"""ha_rig/coverage.py: a suite's fingerprint and whether its record is fresh, as deviceside's release gate judges."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from custom_components.deviceside.core.registry import AdapterRegistry

from ha_rig import coverage
from ha_rig.engine import CaseResult, Device, Outcome, Suite, load_step_file, load_step_files
from ha_rig.observed import Device as ObservedDevice
from ha_rig.observed import Observation, Trace, sequence
from ha_rig.profile import Profile
from ha_rig.progress import Progress, _saved
from ha_rig.target import deviceside_repo

FILES = {st.action: st for st in load_step_files() if not st.options and st.name is None}
SUITE = Suite(FILES["turn_on_for"], "shelly")
OTHER = Suite(FILES["turn_on_for"], "hue")
PKG = "custom_components/deviceside/integrations"
IDS = ["on_then_off[switch_plug]", "on_then_off[light_onoff]"]


def plan(ids=IDS, absent=()) -> list:
    """The cases a profile plans: those in `absent` skipped for lacking the role, as engine.cases marks them."""
    return [
        SimpleNamespace(id=i, skip="no role entity in the profile" if i in absent else None, absent=i in absent)
        for i in ids
    ]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str = "change") -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def record(suite: Suite, cited: str, ids=IDS, date: str = "2026-11-01", skipped=()) -> str:
    rows = "\n".join(
        f"| `{case_id}` | `switch` (switch_plug) | {'skipped' if case_id in skipped else 'pass'} |" for case_id in ids
    )
    return (
        f"# Shelly · {suite.name}\n\n| | |\n|---|---|\n| Result | pass |\n| Date | {date} |\n"
        f"| Source | rig run (`scripts/rig.py`), commit `{cited[:7]}` |\n\n## Tests\n\n"
        f"| Test | Target | Result |\n|---|---|---|\n{rows}\n"
    )


@pytest.fixture
def repo(tmp_path) -> Path:
    """A deviceside-shaped repo: two integration packages and core/."""
    path = tmp_path / "deviceside"
    path.mkdir()
    git(path, "init", "-q")
    commit(
        path,
        {
            f"{PKG}/shelly/__init__.py": "",
            f"{PKG}/shelly/turn_on_for.py": "x = 1\n",
            f"{PKG}/hue/__init__.py": "",
            f"{PKG}/hue/turn_on_for.py": "x = 1\n",
            "custom_components/deviceside/core/engine.py": "y = 1\n",
        },
        "start",
    )
    return path


def shelly(_repo: Path) -> list[str]:
    return [f"{PKG}/shelly/__init__.py", f"{PKG}/shelly/turn_on_for.py"]


def hue(_repo: Path) -> list[str]:
    return [f"{PKG}/hue/__init__.py", f"{PKG}/hue/turn_on_for.py"]


def test_code_is_the_gates_adapter_code_with_the_called_actions():
    registry, repo = AdapterRegistry.discover(), deviceside_repo()
    own = coverage.code(SUITE, registry, repo)
    assert f"{PKG}/shelly/turn_on_for.py" in own and f"{PKG}/shelly/__init__.py" in own
    assert not any("/core/" in path or "/actions/" in path for path in own)
    calling = coverage.code(Suite(FILES["set_auto_off"], "shelly"), registry, repo)  # calls turn_on_for
    assert {f"{PKG}/shelly/set_auto_off.py", *own} <= set(calling)


def test_the_fingerprint_follows_the_suites_code_and_step_file(repo, tmp_path):
    step = tmp_path / "turn_on_for.yaml"
    step.write_text("tests: []\n")
    first = git(repo, "rev-parse", "HEAD")
    before = coverage.fingerprint(repo, first, shelly(repo), step)
    core = commit(repo, {"custom_components/deviceside/core/engine.py": "y = 2\n"})
    assert coverage.fingerprint(repo, core, shelly(repo), step) == before  # core/ isn't the suite's code
    hue_change = commit(repo, {f"{PKG}/hue/turn_on_for.py": "x = 2\n"})
    assert coverage.fingerprint(repo, hue_change, shelly(repo), step) == before
    assert coverage.fingerprint(repo, hue_change, hue(repo), step) != coverage.fingerprint(repo, first, hue(repo), step)
    step.write_text("tests: [changed]\n")
    assert coverage.fingerprint(repo, hue_change, shelly(repo), step) != before


def test_a_record_stays_fresh_until_its_adapters_code_changes(repo):
    cited = git(repo, "rev-parse", "HEAD")
    commit(repo, {f"docs/test-records/shelly/{SUITE.name}.md": record(SUITE, cited)})
    commit(repo, {f"docs/test-records/hue/{OTHER.name}.md": record(OTHER, cited)})
    core = commit(repo, {"custom_components/deviceside/core/engine.py": "y = 2\n"})
    fresh = coverage.freshness(repo, SUITE, core, shelly(repo), plan())
    assert fresh.fresh  # a core/ change stales nothing, as the gate decides
    assert str(fresh) == f"record 2026-11-01, commit {cited[:7]}"
    changed = commit(repo, {f"{PKG}/hue/turn_on_for.py": "x = 2\n"})
    assert coverage.freshness(repo, SUITE, changed, shelly(repo), plan()).fresh
    stale = coverage.freshness(repo, OTHER, changed, hue(repo), plan())
    assert (stale.fresh, str(stale)) == (False, "stale: hue/turn_on_for.py")


def test_a_record_whose_cases_differ_is_stale(repo):
    cited = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {f"docs/test-records/shelly/{SUITE.name}.md": record(SUITE, cited)})
    stale = coverage.freshness(repo, SUITE, head, shelly(repo), plan([IDS[0], "new[switch_plug]"]))
    assert str(stale) == f"stale: cases changed: new new[switch_plug]; gone {IDS[1]}"


def test_no_record_an_unknown_commit_or_none_cited(repo):
    head = git(repo, "rev-parse", "HEAD")
    assert str(coverage.freshness(repo, SUITE, head, shelly(repo), plan())) == "no record"
    head = commit(repo, {f"docs/test-records/shelly/{SUITE.name}.md": record(SUITE, "abcdef1234")})
    assert "which git can't find" in str(coverage.freshness(repo, SUITE, head, shelly(repo), plan()))
    head = commit(repo, {f"docs/test-records/shelly/{SUITE.name}.md": "# no source\n"})
    assert str(coverage.freshness(repo, SUITE, head, shelly(repo), plan())) == "stale: cites no commit"


PROFILE_TEXT = (
    "hue:\n  - entity_id: light.a\n    roles: [light_dimmable]\n"
    "shelly:\n  - entity_id: light.b\n    roles: [light_dimmable]\n"
)


def test_coverage_omits_a_suite_whose_options_the_adapter_lacks(repo, tmp_path, monkeypatch):
    profile_path = tmp_path / "house.yaml"
    profile_path.write_text(
        "hue:\n  - entity_id: light.a\n    roles: [light_dimmable]\n"
        "shelly:\n  - entity_id: light.b\n    roles: [light_dimmable]\n"
    )
    monkeypatch.setattr(coverage, "code", lambda suite, registry, repo: ())
    registry = AdapterRegistry.discover()
    stop = [st for st in load_step_files() if st.action == "stop" and st.options == {"predicted_state"}]
    suites = [Suite(stop[0], "hue"), Suite(stop[0], "shelly")]
    plan = coverage.of(suites, Profile.load(profile_path), registry, repo, git(repo, "rev-parse", "HEAD").strip(), None)
    assert [item.suite.integration for item in plan] == ["hue"]


def test_a_firmware_fact_covers_its_case_across_a_code_change_and_fresh_reruns_it(repo, tmp_path, monkeypatch):
    step = tmp_path / "turn_on_for.yaml"
    step.write_text(
        "action: turn_on_for\nrig_seconds: 5\ntests:\n"
        "  - name: plain\n    roles: [switch_plug]\n    steps:\n      - wait: 0\n"
        "  - name: wins\n    roles: [switch_plug]\n    firmware_fact: true\n    steps:\n      - wait: 0\n"
    )
    suite = Suite(load_step_file(step), "shelly")
    profile_path = tmp_path / "lab.yaml"
    profile_path.write_text("shelly:\n  - entity_id: switch.plug\n    roles: [switch_plug]\n")
    lab, registry = Profile.load(profile_path), AdapterRegistry.discover()
    monkeypatch.setattr(coverage, "code", lambda suite, registry, repo: shelly(repo))
    first = git(repo, "rev-parse", "HEAD")
    ledger = Progress.load(tmp_path, "lab")
    coverage.of([suite], lab, registry, repo, first, ledger)
    ledger.record(suite, CaseResult("wins[switch_plug]", "switch_plug", "switch", Outcome.PASS, "", ("switch.plug",)))
    ledger.facts(suite, "2026.10.1", [Device("Plug", "P1", "1.0")])

    changed = commit(repo, {f"{PKG}/shelly/turn_on_for.py": "x = 2\n"})
    [now] = coverage.of([suite], lab, registry, repo, changed, Progress.load(tmp_path, "lab"))
    assert (set(now.saved), now.by_firmware) == ({"wins[switch_plug]"}, {"wins[switch_plug]"})
    assert [case.id for case in now.missing] == ["plain[switch_plug]"]
    assert str(now) == "partial 1/2, 1 by firmware"
    [rerun] = coverage.of([suite], lab, registry, repo, changed, Progress.load(tmp_path, "lab"), fresh=True)
    assert not rerun.saved and len(rerun.missing) == 2


def test_a_narrower_profiles_record_does_not_cover_a_broader_one(repo):
    cited = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {f"docs/test-records/shelly/{SUITE.name}.md": record(SUITE, cited, skipped=[IDS[0]])})
    bulb = coverage.freshness(repo, SUITE, head, shelly(repo), plan(absent=[IDS[0]]))
    assert bulb.fresh
    full = coverage.freshness(repo, SUITE, head, shelly(repo), plan())
    assert str(full) == f"stale: this profile runs what the record skipped: {IDS[0]}"


def suite_coverage(repo: Path, head: str, planned: list, saved=None) -> coverage.SuiteCoverage:
    fresh = coverage.freshness(repo, SUITE, head, shelly(repo), planned)
    return coverage.SuiteCoverage(SUITE, "fp", fresh, planned, saved or {})


def test_a_broader_record_covers_the_narrower_profile_and_a_stale_one_warns_it_will_not_be_narrowed(repo):
    cited = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {f"docs/test-records/shelly/{SUITE.name}.md": record(SUITE, cited)})
    bulb = suite_coverage(repo, head, plan(absent=[IDS[0]]))
    assert bulb.covered and bulb.narrows == [IDS[0]]  # fresh: covered, nothing to warn of
    assert str(bulb) == f"covered (record 2026-11-01, commit {cited[:7]})"

    changed = commit(repo, {f"{PKG}/shelly/turn_on_for.py": "x = 2\n"})
    stale = suite_coverage(repo, changed, plan(absent=[IDS[0]]))
    warning = f"; won't write a record: this profile skips {IDS[0]}, which the record passed"
    assert not stale.covered and stale.narrows == [IDS[0]]
    assert str(stale).endswith(warning) and str(stale).startswith("uncovered (stale: ")

    saved = {case.id: object() for case in stale.planned}
    assert (
        str(suite_coverage(repo, changed, plan(absent=[IDS[0]]), saved)) == "covered (saved; no record yet)" + warning
    )

    full = suite_coverage(repo, changed, plan())
    assert full.narrows == [] and "won't write" not in str(full)


def test_a_missing_observed_case_hints_its_lend_when_its_twins_last_calls_are_covered(tmp_path):
    seq, legend = sequence([("target", ("fake", "turn_on_for", {"duration": 1}))])
    acme, newer = ObservedDevice("Acme", "S3", "2.0"), ObservedDevice("Acme", "S3", "3.0")
    twin, watched = "t[switch_plug]", "t[switch_plug] observed"
    planned = [
        SimpleNamespace(id=watched, observed=True, twin=twin),
        SimpleNamespace(id="u", observed=False, twin=None),
    ]
    covered = SimpleNamespace(suite=SUITE, missing=planned)
    traced = CaseResult(twin, "switch_plug", "switch", Outcome.PASS, trace=Trace("s0", seq, legend, (acme,)))
    entry = {"integration": "shelly", "suite": SUITE.name, "fingerprint": "old", "touched": "2026-11-01T00:00:00"}
    progress = Progress(tmp_path / "ledger.json", {("shelly", SUITE.name, "old"): entry | {"cases": [_saved(traced)]}})
    row = Observation(newer, "s0", seq, twin, "2026-11-02", "pass")

    hints = coverage.observed_hints([covered], progress, {"shelly": [row]})

    assert hints == {
        ("shelly", SUITE.name, watched): "observed 2026-11-02, if the calls are unchanged; seen on S3, firmware 3.0"
    }
    assert coverage.observed_hints([covered], progress, {"shelly": []}) == {}
