"""The ledger: each suite's saved cases at the commit planned at, kept on disk for an interrupted run to resume."""

import datetime
import json
from pathlib import Path

from ha_rig import progress as progress_module
from ha_rig.engine import Case, CaseResult, Device, Outcome, Suite, load_step_file, load_step_files, merge
from ha_rig.progress import Progress, holds, path_for
from tests.rig_fakes import STEPS, Roster, profile

SUITE = Suite(next(f for f in load_step_files(STEPS) if f.action == "turn_on_for" and not f.options), "shelly")
HUE = Suite(SUITE.file, "hue")
PASSED = CaseResult("a[switch_plug]", "switch_plug", "switch", Outcome.PASS, entity_ids=("switch.plug",))
FAILED = CaseResult("b[switch_plug]", "switch_plug", "switch", Outcome.FAIL, "too slow", ("switch.plug",))


def loaded(tmp_path: Path, commit: str = "fp1", version: str = "2026.10.1", *suites: Suite) -> Progress:
    progress = Progress.load(tmp_path, "lab")
    for suite in suites or (SUITE,):
        progress.track(suite, commit)
    progress.check_ha(version, lambda _: None)
    return progress


def started(tmp_path: Path) -> Progress:
    progress = loaded(tmp_path)
    progress.record(SUITE, PASSED)
    progress.record(SUITE, FAILED)
    progress.facts(SUITE, "2026.10.1", [Device("Plug", "P1", "1.0")])
    return progress


def test_the_ledger_round_trips_a_suites_cases_and_facts(tmp_path):
    started(tmp_path)
    again = loaded(tmp_path)
    done = again.done()
    assert done[("shelly", SUITE.name, PASSED.id)] == PASSED
    assert done[("shelly", SUITE.name, FAILED.id)] == FAILED
    [part] = again.parts([SUITE, HUE])  # nothing saved for hue
    assert (part.cases, part.ha_version, part.devices) == ([PASSED, FAILED], "2026.10.1", [Device("Plug", "P1", "1.0")])
    assert not part.complete
    assert path_for(tmp_path, "lab").name == "lab.json"  # one file per profile, whatever the branch


def test_a_case_recorded_again_replaces_its_result(tmp_path):
    progress = started(tmp_path)
    progress.record(SUITE, PASSED)
    assert [case["id"] for case in loaded(tmp_path).entry(SUITE)["cases"]] == [FAILED.id, PASSED.id]


def test_facts_join_across_parts(tmp_path):
    progress = started(tmp_path)
    progress.facts(SUITE, None, [Device("Plug", "P1", "1.0"), Device("Bulb", None, "2")])
    [part] = loaded(tmp_path).parts([SUITE])
    assert part.devices == [Device("Plug", "P1", "1.0"), Device("Bulb", None, "2")]


def test_parts_rebuild_results_merge_joins(tmp_path):
    started(tmp_path)
    [joined] = merge(loaded(tmp_path).parts([SUITE]), profile(switch_plug="switch.plug"), Roster({})).suites
    assert joined.cases[0].id in {PASSED.id, FAILED.id}


def test_another_commit_sees_none_of_the_saved_cases_and_keeps_them(tmp_path):
    started(tmp_path)
    other = loaded(tmp_path, "fp2")
    assert not other and other.parts([SUITE]) == []
    other.record(SUITE, PASSED)
    assert loaded(tmp_path, "fp1").done().keys() == {
        ("shelly", SUITE.name, PASSED.id),
        ("shelly", SUITE.name, FAILED.id),
    }
    assert set(loaded(tmp_path, "fp2").done()) == {("shelly", SUITE.name, PASSED.id)}


def test_another_ha_version_drops_the_entries_it_ran_on(tmp_path):
    started(tmp_path)
    said = []
    same = Progress.load(tmp_path, "lab")
    same.track(SUITE, "fp1")
    same.check_ha("2026.10.1", said.append)
    assert same and said == []
    newer = Progress.load(tmp_path, "lab")
    newer.track(SUITE, "fp1")
    newer.check_ha("2026.11.0", said.append)
    assert not newer and "HA 2026.10.1, and HA is 2026.11.0" in said[0]
    assert loaded(tmp_path)  # dropped from the file only by a save
    newer.record(SUITE, PASSED)
    assert set(loaded(tmp_path, version="2026.11.0").done()) == {("shelly", SUITE.name, PASSED.id)}


def test_an_unreachable_skip_is_never_saved(tmp_path):
    progress = loaded(tmp_path)
    progress.record(
        SUITE, CaseResult("c[light]", "light", None, Outcome.SKIPPED, "light.x unreachable", unreachable=True)
    )
    assert not progress
    assert not path_for(tmp_path, "lab").exists()


def test_a_failed_suite_is_dropped_at_the_runs_end_and_others_stay(tmp_path):
    progress = started(tmp_path)
    progress.track(HUE, "fp1")
    progress.record(HUE, PASSED)
    assert progress.drop_failed() == [("shelly", SUITE.name)]
    again = loaded(tmp_path, "fp1", "2026.10.1", SUITE, HUE)
    assert set(again.done()) == {("hue", SUITE.name, PASSED.id)}


def test_a_written_record_drops_its_suite(tmp_path):
    progress = started(tmp_path)
    progress.drop(SUITE)
    progress.save()
    assert not path_for(tmp_path, "lab").exists()


def test_a_save_merges_another_runs_saves(tmp_path):
    shelly_run = loaded(tmp_path)
    hue_run = loaded(tmp_path, "fp1", "2026.10.1", HUE)
    shelly_run.record(SUITE, PASSED)
    hue_run.record(HUE, PASSED)  # read before shelly's save; must not erase it
    shelly_run.record(SUITE, FAILED)
    both = loaded(tmp_path, "fp1", "2026.10.1", SUITE, HUE)
    assert set(both.done()) == {
        ("shelly", SUITE.name, PASSED.id),
        ("shelly", SUITE.name, FAILED.id),
        ("hue", SUITE.name, PASSED.id),
    }


def test_entries_untouched_for_a_month_are_pruned(tmp_path, monkeypatch):
    started(tmp_path)
    loaded(tmp_path, "fp1", "2026.10.1", HUE).record(HUE, PASSED)
    path = path_for(tmp_path, "lab")
    saved = json.loads(path.read_text())
    old = datetime.datetime.now() - datetime.timedelta(days=progress_module.PRUNE_DAYS + 1)
    saved["suites"][0]["touched"] = old.isoformat(timespec="seconds")
    path.write_text(json.dumps(saved))
    progress = loaded(tmp_path, "fp1", "2026.10.1", SUITE, HUE)
    assert set(progress.done()) == {("hue", SUITE.name, PASSED.id)}
    progress.save()
    assert [entry["integration"] for entry in json.loads(path.read_text())["suites"]] == ["hue"]


def test_forget_drops_cases_that_no_longer_hold(tmp_path):
    progress = started(tmp_path)
    progress.forget(SUITE, [FAILED.id])
    progress.save()
    assert set(loaded(tmp_path).done()) == {("shelly", SUITE.name, PASSED.id)}


def test_an_unreadable_file_is_an_empty_ledger(tmp_path):
    path = path_for(tmp_path, "lab")
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert not loaded(tmp_path)


def test_a_saved_case_with_no_absent_key_still_loads(tmp_path):
    started(tmp_path)
    path = path_for(tmp_path, "lab")
    data = json.loads(path.read_text())
    for entry in data["suites"]:
        for case in entry["cases"]:
            assert case.pop("absent") is False
    path.write_text(json.dumps(data))
    assert loaded(tmp_path).done()[("shelly", SUITE.name, PASSED.id)] == PASSED


def test_an_absent_skip_round_trips(tmp_path):
    absent = CaseResult("c[switch_plug]", "switch_plug", None, Outcome.SKIPPED, "no switch_plug entity", absent=True)
    progress = loaded(tmp_path)
    progress.record(SUITE, absent)
    assert loaded(tmp_path).done()[("shelly", SUITE.name, absent.id)].absent


def test_an_entry_from_before_commit_keys_is_left_out_and_said(tmp_path, capsys):
    path = path_for(tmp_path, "lab")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"suites": [{"integration": "shelly", "suite": SUITE.name, "fingerprint": "f"}]}))
    assert not loaded(tmp_path)
    assert "unreadable or from an older rig" in capsys.readouterr().out


FIRST, SECOND = SUITE.file.tests[:2]


def planned(test=FIRST, entities=("switch.plug",), skip=None) -> Case:
    return Case(SUITE, test, "switch_plug", {"switch_plug": entities[0]}, skip)


def ran(case: Case, outcome: Outcome = Outcome.PASS, detail: str = "") -> CaseResult:
    return CaseResult(case.id, case.role, "switch", outcome, detail, tuple(case.entities.values()))


def test_resume_keeps_the_saved_results_that_hold_and_forgets_the_rest(tmp_path):
    progress = loaded(tmp_path)
    for case in (planned(FIRST), planned(SECOND)):
        progress.record(SUITE, ran(case))
    again = Progress.load(tmp_path, "lab")
    again.resume(SUITE, "fp1", [planned(FIRST), planned(SECOND, ("switch.other",))])
    assert set(again.saved(SUITE)) == {planned(FIRST).id}  # the second now moves another entity


def test_resume_at_another_commit_starts_afresh(tmp_path):
    started(tmp_path)
    again = Progress.load(tmp_path, "lab")
    again.resume(SUITE, "fp2", [])
    assert not again


def test_resume_drops_a_suite_with_a_saved_failure_or_when_fresh(tmp_path):
    started(tmp_path)  # holds FAILED
    again = Progress.load(tmp_path, "lab")
    again.resume(SUITE, "fp1", [])
    assert not again
    loaded(tmp_path).record(SUITE, ran(planned()))
    fresh = Progress.load(tmp_path, "lab")
    fresh.resume(SUITE, "fp1", [planned()], fresh=True)
    assert not fresh


def test_a_saved_skip_holds_only_for_the_same_reason_and_not_observed_only_unattended():
    why = "fact test: runs with --facts"
    skipped = ran(planned(), Outcome.SKIPPED, why)
    assert holds(planned(skip=why), skipped, False)
    assert not holds(planned(), skipped, False)  # asked for now: it runs
    unwatched = ran(planned(), Outcome.NOT_OBSERVED)
    assert holds(planned(), unwatched, True) and not holds(planned(), unwatched, False)


FACT_FILE = """
action: turn_on_for
rig_seconds: 5
tests:
  - name: plain
    roles: [switch_plug]
    steps:
      - wait: 0
  - name: wins
    roles: [switch_plug]
    firmware_fact: true
    steps:
      - wait: 0
"""


def test_a_fact_tests_failure_never_drops_the_suite(tmp_path):
    (path := tmp_path / "steps.yaml").write_text(FACT_FILE)
    suite = Suite(load_step_file(path), "shelly", facts=True)
    progress = loaded(tmp_path, "c1", "2026.10.1", suite)
    passed = CaseResult("plain[switch_plug]", "switch_plug", "switch", Outcome.PASS, entity_ids=("switch.plug",))
    progress.record(suite, passed)
    progress.record(
        suite, CaseResult("wins[switch_plug]", "switch_plug", "switch", Outcome.FAIL, "x", ("switch.plug",))
    )
    assert progress.drop_failed() == []
    again = Progress.load(tmp_path, "lab")
    planned = [Case(suite, test, "switch_plug", {"switch_plug": "switch.plug"}) for test in suite.file.tests]
    again.resume(suite, "c1", planned)
    assert set(again.saved(suite)) == {"plain[switch_plug]", "wins[switch_plug]"}  # resumed, the fact's fail kept
    progress.record(suite, replace_failed(passed))
    assert progress.drop_failed() == [("shelly", "turn_on_for")]  # a default case's failure does


def replace_failed(result: CaseResult) -> CaseResult:
    return CaseResult(result.id, result.role, result.domain, Outcome.FAIL, "x", result.entity_ids)
