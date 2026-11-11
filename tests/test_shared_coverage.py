"""A run's `core/` coverage in its records (ha_rig/shared_coverage.py, rig.measured): the section deviceside's gate
reads, and what stands in for it when the helper or the run lets it down."""

import types
from pathlib import Path

import pytest

import scripts.rig as rig
from ha_rig import engine, shared_coverage, target
from ha_rig.client import HAError
from ha_rig.engine import CaseResult, Outcome, RunResult, SuiteResult
from tests import rig_fakes as fakes
from tests.test_rig import target_suite

CORE = "/config/custom_components/deviceside/core"


def test_rows_map_core_paths_to_modules_with_collapsed_ranges_sorted():
    executed = {
        f"{CORE}/dispatcher.py": [55, 12, 13, 14, 40],
        f"{CORE}/base/util.py": [3, 5, 6],
        f"{CORE}/empty.py": [],
        "/config/custom_components/deviceside/integrations/hue/light.py": [1, 2],
        "/usr/lib/other.py": [9],
    }
    assert shared_coverage.rows(executed) == [("core/base/util.py", "3, 5-6"), ("core/dispatcher.py", "12-14, 40, 55")]


def test_no_rows_render_no_section():
    assert shared_coverage.section([]) == []
    assert shared_coverage.rows({}) == []


def passed_suite(tmp_path: Path) -> SuiteResult:
    suite = target_suite(tmp_path)[0]
    return SuiteResult(suite, [CaseResult("t[light_ct]", "light_ct", "light", Outcome.PASS)], True)


def test_a_rendered_record_is_read_back_by_deviceside_gate(tmp_path, monkeypatch):
    """What the gate's own parser takes from the section is what the run executed, with no error."""
    gate = target.check_release(target.deviceside_repo())
    monkeypatch.setattr(gate, "REPO", tmp_path)
    shared = shared_coverage.rows({f"{CORE}/dispatcher.py": [12, 13, 14, 40, 55], f"{CORE}/b.py": [7]})
    text = engine.render_record(passed_suite(tmp_path), "Shelly", "2026.10.1", [], "2026-11-11", "t", shared=shared)
    (path := tmp_path / "record.md").write_text(text)

    found, errors = gate.shared_rows(path, "c1")

    assert errors == []
    assert {module: row.executed for module, row in found} == {
        "custom_components/deviceside/core/dispatcher.py": frozenset({12, 13, 14, 40, 55}),
        "custom_components/deviceside/core/b.py": frozenset({7}),
    }
    assert "## Shared coverage" not in engine.render_record(
        passed_suite(tmp_path), "Shelly", "2026.10.1", [], "2026-11-11", "t"
    )


class Helper(fakes.FakeAdmin):
    """FakeAdmin plus the rig_coverage services: `executed` is what `stop` returns, `broken` what raises."""

    def __init__(self, executed=None, broken: str | None = None):
        super().__init__({})
        self.executed, self.broken = executed or {}, broken

    async def call_service(self, domain, service, data=None, return_response=False):
        if domain != "rig_coverage":
            return await super().call_service(domain, service, data, return_response)
        self.calls.append((domain, service, dict(data or {})))
        if self.broken == service:
            raise HAError({"code": "service_not_found", "message": f"{domain}.{service}"})
        return self.executed if return_response else None


async def test_the_helper_is_started_and_its_lines_taken_on_stop():
    helper = Helper({f"{CORE}/a.py": [1, 2]})
    assert await shared_coverage.start(helper) is None
    assert await shared_coverage.stop(helper) == ([("core/a.py", "1-2")], None)
    assert [call[1] for call in helper.calls] == ["start", "stop"]


@pytest.mark.parametrize(
    ("helper", "why"),
    [
        (Helper(broken="stop"), "rig_coverage.stop failed"),
        (Helper({}), "no core/ lines"),
        (Helper({"/elsewhere.py": [1]}), "no core/ lines"),
    ],
)
async def test_a_stop_that_fails_or_returns_nothing_gives_no_rows(helper, why):
    rows, reason = await shared_coverage.stop(helper)
    assert rows == [] and why in reason


async def test_measured_brackets_the_run_and_stops_it_when_it_aborts():
    helper = Helper({f"{CORE}/a.py": [4]})
    rows: shared_coverage.Rows = []

    async def aborted():
        raise RuntimeError("signal")

    with pytest.raises(RuntimeError):
        await rig.measured(helper, rows, aborted())
    assert [call[1] for call in helper.calls] == ["start", "stop"]
    assert rows == [("core/a.py", "4")]


async def test_a_missing_helper_costs_the_section_not_the_run(capsys):
    helper = Helper(broken="start")
    rows: shared_coverage.Rows = []

    async def run():
        return "ran"

    assert await rig.measured(helper, rows, run()) == "ran"
    assert rows == [] and [call[1] for call in helper.calls] == ["start"]  # never stopped what never started
    assert "No shared coverage: rig_coverage.start failed" in capsys.readouterr().out


async def test_an_unmeasured_run_never_calls_the_helper():
    async def run():
        return "ran"

    assert await rig.measured(None, [], run()) == "ran"


async def report_with(tmp_path, monkeypatch, outcome: Outcome) -> list:
    """What `shared` reaches write_records when the run's one suite ends `outcome`."""
    got = []

    async def write_records(result, registry, records, date, source, into=None, untraced=None, shared=()):
        got.append(shared)
        return [], []

    monkeypatch.setattr(rig, "write_records", write_records)
    monkeypatch.setattr(rig, "write_report", lambda result, directory, title: tmp_path / "report.md")
    monkeypatch.setattr(rig, "git", lambda *args, check=True: "c1")
    monkeypatch.setattr(rig, "merge", lambda suites, profile, registry: RunResult(suites))
    suite = passed_suite(tmp_path)
    suite.cases.append(CaseResult("u[light_ct]", "light_ct", "light", outcome))
    staging = types.SimpleNamespace(test_records_dir=lambda integration: tmp_path)
    shared = [("core/a.py", "1-2")]
    await rig.report(RunResult([suite]), None, "c1", fakes.profile("shelly"), staging, "s", shared=shared)
    return got


async def test_every_record_gets_the_run_lines_when_nothing_failed(tmp_path, monkeypatch):
    assert await report_with(tmp_path, monkeypatch, Outcome.PASS) == [[("core/a.py", "1-2")]]


async def test_a_failed_suite_takes_the_section_from_every_record(tmp_path, monkeypatch, capsys):
    assert await report_with(tmp_path, monkeypatch, Outcome.FAIL) == [()]
    assert "No shared coverage in any record" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--no-deploy", "--check", "--recover", "--coverage", "--deploy-only"])
def test_the_flag_is_refused_where_no_record_is_written(flag, monkeypatch, capsys):
    monkeypatch.setattr(rig.sys, "argv", ["rig.py"])
    with pytest.raises(SystemExit) as exit_:
        rig.main(["--shared-coverage", flag])
    assert exit_.value.code == 2
    assert "--shared-coverage" in (err := capsys.readouterr().err) or "--deploy-only" in err


def test_the_flag_takes_ha_exclusively():
    setup = rig.Setup({}, fakes.profile("shelly"), fakes.OPTION_REGISTRY, [], False, False)
    on = types.SimpleNamespace(no_deploy=True, roam_check=False, shared_coverage=True)
    off = types.SimpleNamespace(no_deploy=True, roam_check=False, shared_coverage=False)
    assert rig.claim(on, setup).ha == "exclusive"
    assert rig.claim(off, setup).ha == "shared"


def test_the_flag_needs_the_admin_token():
    on = types.SimpleNamespace(shared_coverage=True)
    with pytest.raises(SystemExit, match="HA_ADMIN_TOKEN"):
        rig.refuse_unmeasurable(on, {"HA_TOKEN": "t"})
    rig.refuse_unmeasurable(on, {"HA_ADMIN_TOKEN": "a"})
    rig.refuse_unmeasurable(types.SimpleNamespace(shared_coverage=False), {})
