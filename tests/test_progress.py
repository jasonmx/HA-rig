"""The ledger: each suite's saved cases at its fingerprint, kept on disk for later runs to build coverage on."""

import datetime
import json
from dataclasses import asdict
from pathlib import Path

from ha_rig import progress as progress_module
from ha_rig.engine import CaseResult, Device, Outcome, Suite, load_step_file, load_step_files, merge
from ha_rig.progress import Progress, path_for
from tests.rig_fakes import Roster, profile

SUITE = Suite(next(f for f in load_step_files() if f.action == "turn_on_for" and not f.options), "shelly")
HUE = Suite(SUITE.file, "hue")
PASSED = CaseResult("a[switch_plug]", "switch_plug", "switch", Outcome.PASS, entity_ids=("switch.plug",))
FAILED = CaseResult("b[switch_plug]", "switch_plug", "switch", Outcome.FAIL, "too slow", ("switch.plug",))


def loaded(tmp_path: Path, fingerprint: str = "fp1", version: str = "2026.10.1", *suites: Suite) -> Progress:
    progress = Progress.load(tmp_path, "lab")
    for suite in suites or (SUITE,):
        progress.track(suite, fingerprint)
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


def test_another_fingerprint_sees_none_of_the_saved_cases_and_keeps_them(tmp_path):
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


FACTS_YAML = """
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
WINS = CaseResult("wins[switch_plug]", "switch_plug", "switch", Outcome.PASS, entity_ids=("switch.plug",))
PLUG = Device("Plug", "P1", "1.0")


def facts_suite(tmp_path: Path, text: str = FACTS_YAML) -> Suite:
    path = tmp_path / "turn_on_for.yaml"
    path.write_text(text)
    return Suite(load_step_file(path), "shelly")


def ran_a_fact(tmp_path: Path, suite: Suite, devices=(PLUG,)) -> Progress:
    progress = loaded(tmp_path, "fp1", "2026.10.1", suite)
    progress.record(suite, WINS)
    progress.facts(suite, "2026.10.1", list(devices))
    return progress


def test_a_firmware_facts_pass_outlives_its_fingerprint_and_ha_version(tmp_path):
    suite = facts_suite(tmp_path)
    ran_a_fact(tmp_path, suite).drop(suite)  # its record is written
    later = loaded(tmp_path, "fp2", "2026.11.0", suite)
    assert not later  # the new code's entry has nothing
    assert later.fact_results(suite) == {WINS.id: (WINS, [PLUG])}
    later.lend(suite, later.fact_results(suite).values())
    [part] = later.parts([suite])
    assert (part.cases, part.devices) == ([WINS], [PLUG])  # its record carries the fact and its firmware


def test_a_firmware_fact_lends_nothing_until_its_devices_are_known(tmp_path):
    suite = facts_suite(tmp_path)
    progress = loaded(tmp_path, "fp1", "2026.10.1", suite)
    progress.record(suite, WINS)  # the run ended before the part's facts
    assert loaded(tmp_path, "fp2", "2026.10.1", suite).fact_results(suite) == {}


def test_a_firmware_fact_goes_when_its_model_shows_another_firmware(tmp_path):
    suite = facts_suite(tmp_path)
    ran_a_fact(tmp_path, suite)
    other = loaded(tmp_path, "fp2", "2026.10.1", suite)
    other.record(suite, CaseResult("plain[switch_plug]", "switch_plug", "switch", Outcome.PASS))
    other.facts(suite, "2026.10.1", [Device("Bulb", None, "9")])  # another model: the fact stands
    assert WINS.id in loaded(tmp_path, "fp2", "2026.10.1", suite).fact_results(suite)
    other.facts(suite, "2026.10.1", [Device("Plug", "P1", "1.1")])
    assert loaded(tmp_path, "fp2", "2026.10.1", suite).fact_results(suite) == {}


def test_a_firmware_fact_goes_when_it_fails_or_its_test_changes(tmp_path):
    suite = facts_suite(tmp_path)
    ran_a_fact(tmp_path, suite)
    edited = facts_suite(tmp_path, FACTS_YAML.replace("      - wait: 0\n", "      - wait: 1\n"))
    assert loaded(tmp_path, "fp2", "2026.10.1", edited).fact_results(edited) == {}
    assert WINS.id in loaded(tmp_path, "fp2", "2026.10.1", suite).fact_results(suite)
    failing = loaded(tmp_path, "fp2", "2026.10.1", suite)
    failing.record(suite, CaseResult(WINS.id, "switch_plug", "switch", Outcome.FAIL, "lost", ("switch.plug",)))
    assert loaded(tmp_path, "fp2", "2026.10.1", suite).fact_results(suite) == {}


def test_a_plain_case_is_never_a_firmware_fact(tmp_path):
    suite = facts_suite(tmp_path)
    progress = loaded(tmp_path, "fp1", "2026.10.1", suite)
    progress.record(suite, CaseResult("plain[switch_plug]", "switch_plug", "switch", Outcome.PASS))
    progress.facts(suite, "2026.10.1", [PLUG])
    assert progress.fact_results(suite) == {}
    assert "firmware" not in json.loads(path_for(tmp_path, "lab").read_text())


def test_a_lent_fact_whose_firmware_moved_leaves_the_entry_to_rerun(tmp_path):
    suite = facts_suite(tmp_path)
    ran_a_fact(tmp_path, suite).drop(suite)
    later = loaded(tmp_path, "fp2", "2026.10.1", suite)
    later.lend(suite, later.fact_results(suite).values())
    later.facts(suite, "2026.10.1", [Device("Plug", "P1", "1.1")])  # a part of the new run reads newer firmware
    assert later.parts([suite]) == []  # nothing left to carry: the fact reruns
    assert later.entry(suite)["devices"] == [asdict(Device("Plug", "P1", "1.1"))]  # the old firmware is gone too
    assert later.fact_results(suite) == {}


def test_a_failed_device_lookup_leaves_the_fact_pending(tmp_path):
    suite = facts_suite(tmp_path)
    ran_a_fact(tmp_path, suite, devices=())  # rig_facts failed: no devices
    assert loaded(tmp_path, "fp2", "2026.10.1", suite).fact_results(suite) == {}


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
