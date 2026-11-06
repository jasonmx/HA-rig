"""The rig's saved progress: a stopped run's finished cases, kept on disk for the rerun that resumes it."""

from pathlib import Path

from custom_components.deviceside.core.registry import AdapterRegistry

from ha_rig.engine import CaseResult, Device, Outcome, Suite, load_step_files, merge
from ha_rig.progress import Key, Progress, path_for
from tests.rig_fakes import profile

KEY = Key("lab", "tree1", ("shelly",), ("turn_on_for",), None, False, True)
SUITE = Suite(next(f for f in load_step_files() if f.action == "turn_on_for" and not f.options), "shelly")
PASSED = CaseResult("a[switch_plug]", "switch_plug", "switch", Outcome.PASS, entity_ids=("switch.plug",))
FAILED = CaseResult("b[switch_plug]", "switch_plug", "switch", Outcome.FAIL, "too slow", ("switch.plug",))


def started(tmp_path: Path, key: Key = KEY) -> Progress:
    progress = Progress.load(tmp_path, "lab", "wt/x", key)
    progress.record(SUITE, PASSED)
    progress.record(SUITE, FAILED)
    progress.facts(SUITE, "2026.10.1", [Device("Plug", "P1", "1.0")])
    return progress


def test_progress_round_trips_its_cases_and_facts(tmp_path):
    started(tmp_path)
    again = Progress.load(tmp_path, "lab", "wt/x", KEY)
    done = again.done()
    assert done[("shelly", SUITE.name, PASSED.id)] == PASSED
    assert done[("shelly", SUITE.name, FAILED.id)] == FAILED  # a finished failure is kept
    assert again.ha_version == "2026.10.1"
    [part] = again.parts([SUITE])
    assert (part.cases, part.ha_version, part.devices) == ([PASSED, FAILED], "2026.10.1", [Device("Plug", "P1", "1.0")])
    assert not part.complete


def test_a_case_recorded_again_replaces_its_result(tmp_path):
    progress = started(tmp_path)
    progress.record(SUITE, PASSED)
    assert [case["id"] for case in Progress.load(tmp_path, "lab", "wt/x", KEY).cases] == [FAILED.id, PASSED.id]


def test_facts_join_across_parts(tmp_path):
    progress = started(tmp_path)
    progress.facts(SUITE, None, [Device("Plug", "P1", "1.0"), Device("Bulb", None, "2")])
    [part] = Progress.load(tmp_path, "lab", "wt/x", KEY).parts([SUITE])
    assert part.ha_version == "2026.10.1"
    assert part.devices == [Device("Plug", "P1", "1.0"), Device("Bulb", None, "2")]


def test_parts_rebuild_results_merge_joins(tmp_path):
    started(tmp_path)
    registry = AdapterRegistry.discover()
    parts = Progress.load(tmp_path, "lab", "wt/x", KEY).parts([SUITE, Suite(SUITE.file, "hue")])
    assert [part.suite.integration for part in parts] == ["shelly"]  # nothing saved for hue
    [joined] = merge(parts, profile(switch_plug="switch.plug"), registry).suites
    assert joined.cases[0].id in {PASSED.id, FAILED.id}


def test_a_changed_tree_or_selection_starts_fresh_saying_why(tmp_path):
    started(tmp_path)
    said = []
    tree = Progress.load(tmp_path, "lab", "wt/x", Key("lab", "tree2", ("shelly",), ("turn_on_for",)), said.append)
    assert not tree and "tree changed" in said[-1]
    selection = Progress.load(tmp_path, "lab", "wt/x", Key("lab", "tree1", ("hue",), ("turn_on_for",)), said.append)
    assert not selection and "selection changed" in said[-1]
    assert path_for(tmp_path, "lab", "wt/x").exists()  # gone only once the new run saves a case


def test_fresh_drops_the_saved_progress(tmp_path):
    started(tmp_path)
    said = []
    assert not Progress.load(tmp_path, "lab", "wt/x", KEY, said.append, fresh=True)
    assert "--fresh" in said[0]


def test_another_ha_version_starts_fresh(tmp_path):
    started(tmp_path)
    said = []
    progress = Progress.load(tmp_path, "lab", "wt/x", KEY)
    progress.check_ha("2026.10.1", said.append)
    assert progress and said == []
    progress.check_ha("2026.11.0", said.append)
    assert not progress and "HA is 2026.11.0" in said[0]
    progress = Progress.load(tmp_path, "lab", "wt/x", KEY)
    progress.check_ha(None, said.append)
    assert not progress and "can't be read" in said[-1]


def test_one_file_per_profile_and_branch_slashes_and_all(tmp_path):
    started(tmp_path)
    path = path_for(tmp_path, "lab", "wt/x")
    assert path.name == "lab--wt_x.json"
    assert not Progress.load(tmp_path, "lab", "main", KEY)
    assert Progress.load(tmp_path, "lab", "wt/x", KEY)


def test_discard_deletes_the_file(tmp_path):
    progress = started(tmp_path)
    progress.discard()
    assert not path_for(tmp_path, "lab", "wt/x").exists()
    assert not Progress.load(tmp_path, "lab", "wt/x", KEY)
