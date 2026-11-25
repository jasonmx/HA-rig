"""A role in EACH_ENTITY (switch_plug) runs its cases once per entity the profile lists with it: each plug its own
case, result and record test. Every other role tests its first entity, as before."""

import json
from pathlib import Path

import pytest

from ha_rig import engine
from ha_rig.engine import Outcome, Record, case_family, cases, in_head, narrows
from ha_rig.profile import Profile, RigEntity
from ha_rig.progress import Progress
from ha_rig.target import SCHEMA, deviceside_repo

from .rig_fakes import FakeHA, Roster
from .test_rig import rig
from .test_rig_engine import ON_THEN_OFF, TOLERANCE, outcomes, suite, timer_on

PLUGS = ("switch.plug_a", "switch.plug_b", "switch.plug_c")
WATCHED = """
action: turn_on_for
rig_seconds: 5
tests:
  - name: on_then_off
    roles: [switch_plug]
    setup:
      - set: {state: off}
    steps:
      - call: {duration: 0.2}
      - expect_response: {status: expected, integration: self}
    observe: {see: It clicks., watch: Clicks., ask: 'Did it click?'}
"""
TWO_ROLES = """
action: turn_on_for
rig_seconds: 5
tests:
  - name: called
    roles: [switch_plug, light_ct]
    steps:
      - call: {duration: 0.3}
"""


def plugs(*entity_ids: str, integration: str = "shelly", **extra: RigEntity) -> Profile:
    entities = [RigEntity(entity_id, frozenset({"switch_plug"})) for entity_id in entity_ids]
    return Profile({integration: [*entities, *extra.values()]})


def test_entities_lists_every_one_with_the_role_in_order_and_entity_the_first():
    inv = plugs(*PLUGS)
    assert inv.entities("shelly", "switch_plug") == PLUGS
    assert inv.entity("shelly", "switch_plug") == PLUGS[0]
    assert inv.entities("shelly", "light_ct") == ()
    assert inv.entities("zha", "switch_plug") == ()


def test_an_entity_listed_twice_on_an_integration_is_refused(tmp_path):
    path = tmp_path / "lab.yaml"
    path.write_text("shelly:\n" + f"  - entity_id: {PLUGS[0]}\n    roles: [switch_plug]\n" * 2)
    with pytest.raises(ValueError, match=f"{PLUGS[0]} is listed twice under shelly"):
        Profile.load(path)


def test_each_plug_gets_its_own_case_and_the_first_keeps_the_bare_id(tmp_path):
    planned = cases(suite(tmp_path, ON_THEN_OFF), plugs(*PLUGS))
    assert [(case.id, case.entities) for case in planned] == [
        ("on_then_off[switch_plug]", {"switch_plug": PLUGS[0]}),
        ("on_then_off[switch_plug-2]", {"switch_plug": PLUGS[1]}),
        ("on_then_off[switch_plug-3]", {"switch_plug": PLUGS[2]}),
    ]
    assert {case.role for case in planned} == {"switch_plug"}


def test_one_plug_plans_what_it_always_did(tmp_path):
    [case] = cases(suite(tmp_path, ON_THEN_OFF), plugs(PLUGS[0]))
    assert (case.id, case.entities, case.index) == ("on_then_off[switch_plug]", {"switch_plug": PLUGS[0]}, 1)


def test_no_plug_is_one_absent_case(tmp_path):
    [case] = cases(suite(tmp_path, ON_THEN_OFF), Profile({"shelly": []}))
    assert (case.id, case.skip, case.absent) == (
        "on_then_off[switch_plug]",
        "no switch_plug entity in the profile",
        True,
    )


def test_a_light_role_with_two_entities_still_tests_the_first(tmp_path):
    lights = [RigEntity("light.a", frozenset({"light_ct"})), RigEntity("light.b", frozenset({"light_ct"}))]
    planned = cases(
        suite(tmp_path, TWO_ROLES), Profile({"shelly": [RigEntity(PLUGS[0], frozenset({"switch_plug"})), *lights]})
    )
    assert [(case.id, case.entities) for case in planned] == [
        ("called[switch_plug]", {"switch_plug": PLUGS[0]}),
        ("called[light_ct]", {"light_ct": "light.a"}),
    ]


def test_an_observed_case_pairs_with_its_own_plugs_automated_case(tmp_path):
    planned = cases(suite(tmp_path, WATCHED), plugs(*PLUGS[:2]))
    assert [(case.id, case.twin, case.entities["switch_plug"]) for case in planned] == [
        ("on_then_off[switch_plug]", "on_then_off[switch_plug] observed", PLUGS[0]),
        ("on_then_off[switch_plug] observed", "on_then_off[switch_plug]", PLUGS[0]),
        ("on_then_off[switch_plug-2]", "on_then_off[switch_plug-2] observed", PLUGS[1]),
        ("on_then_off[switch_plug-2] observed", "on_then_off[switch_plug-2]", PLUGS[1]),
    ]


def test_a_keyword_picks_one_plugs_case(tmp_path):
    [case] = cases(suite(tmp_path, ON_THEN_OFF), plugs(*PLUGS), "switch_plug-2")
    assert case.entities == {"switch_plug": PLUGS[1]}


@pytest.mark.parametrize(
    ("case_id", "family"),
    [
        ("t[switch_plug]", ("t", "switch_plug", False)),
        ("t[switch_plug-3]", ("t", "switch_plug", False)),
        ("t[switch_plug-2] observed", ("t", "switch_plug", True)),
        ("t[light_ct] observed", ("t", "light_ct", True)),
    ],
)
def test_a_case_family_drops_the_index(case_id, family):
    assert case_family(case_id) == family


async def test_each_plugs_watched_case_has_its_own_banner(tmp_path):
    ha, printed = FakeHA({PLUGS[0]: {}, PLUGS[1]: {}}, timer_on), []
    registry = Roster({"shelly": {"turn_on_for", "get_attributes"}})
    steps = suite(tmp_path, WATCHED)
    await engine.run(
        [steps], plugs(*PLUGS[:2]), registry, ha, tolerance=TOLERANCE, out=printed.append, ask=lambda _: "y"
    )
    assert [line for line in printed if line.startswith("---")] == [
        "--- turn_on_for / on_then_off[switch_plug] observed (1 of 2) ---",
        "--- turn_on_for / on_then_off[switch_plug-2] observed (2 of 2) ---",
    ]


async def test_a_run_drives_and_restores_each_plug_and_reports_each(tmp_path):
    ha = FakeHA({PLUGS[0]: {"state": "on"}, PLUGS[1]: {"state": "off"}}, timer_on)
    result = await engine.run(
        [suite(tmp_path, ON_THEN_OFF)],
        plugs(*PLUGS[:2]),
        Roster({"shelly": {"turn_on_for", "get_attributes"}}),
        ha,
        tolerance=TOLERANCE,
        out=lambda _: None,
    )
    assert outcomes(result) == {
        "on_then_off[switch_plug]": (Outcome.PASS, ""),
        "on_then_off[switch_plug-2]": (Outcome.PASS, ""),
    }
    called = [data["entity_id"] for domain, _, data in ha.calls if domain == "deviceside"]
    assert called == list(PLUGS[:2])
    assert ha.states[PLUGS[0]]["state"] == "on" and ha.states[PLUGS[1]]["state"] == "off"  # each put back
    assert not result.failed


def passed(*case_ids: str) -> Record:
    return Record(Path("t.json"), "abc1234", "2026-11-15", frozenset(case_ids), frozenset(case_ids))


def test_a_run_with_fewer_plugs_narrows_the_record_and_one_with_as_many_does_not(tmp_path):
    record = passed("on_then_off[switch_plug]", "on_then_off[switch_plug-2]", "on_then_off[switch_plug-3]")
    steps = suite(tmp_path, ON_THEN_OFF)
    assert narrows(cases(steps, plugs(*PLUGS[:2])), record) == ["on_then_off[switch_plug-3]"]
    assert narrows(cases(steps, plugs(*PLUGS)), record) == []
    assert narrows(cases(steps, Profile({"shelly": []})), record) == sorted(record.passed)
    # A test the step file no longer has is no narrowing: its family isn't planned.
    assert narrows(cases(steps, plugs(*PLUGS)), passed("gone[switch_plug-2]")) == []


def ledger(tmp_path: Path, steps, saved_on: Profile, resumed_on: Profile, saved: int) -> set[str]:
    """The case ids a resume on `resumed_on` keeps, after the first `saved` cases on `saved_on` passed and were
    saved."""
    progress = Progress.load(tmp_path, "lab")
    progress.track(steps, "c1")
    progress.check_ha("2026.10.1", lambda _: None)
    for case in cases(steps, saved_on)[:saved]:
        progress.record(
            steps, engine.CaseResult(case.id, case.role, "switch", Outcome.PASS, "", tuple(case.entities.values()))
        )
    again = Progress.load(tmp_path, "lab")
    again.check_ha("2026.10.1", lambda _: None)
    again.resume(steps, "c1", cases(steps, resumed_on))
    return set(again.saved(steps))


def test_a_resume_keeps_the_saved_plugs_and_runs_the_rest(tmp_path):
    steps = suite(tmp_path, ON_THEN_OFF)
    kept = ledger(tmp_path, steps, plugs(*PLUGS), plugs(*PLUGS), saved=2)
    assert kept == {"on_then_off[switch_plug]", "on_then_off[switch_plug-2]"}


def test_a_resume_after_the_plugs_are_reordered_reruns_them(tmp_path):
    steps = suite(tmp_path, ON_THEN_OFF)
    assert ledger(tmp_path, steps, plugs(*PLUGS), plugs(PLUGS[1], PLUGS[0], PLUGS[2]), saved=2) == set()


def test_a_resume_on_one_plug_keeps_only_the_first(tmp_path):
    steps = suite(tmp_path, ON_THEN_OFF)
    assert ledger(tmp_path, steps, plugs(*PLUGS), plugs(PLUGS[0]), saved=2) == {"on_then_off[switch_plug]"}


async def test_a_run_with_two_plugs_keeps_a_record_that_passed_three(tmp_path):
    steps = suite(tmp_path, ON_THEN_OFF)
    old = tmp_path / "records" / "shelly" / "turn_on_for.md"
    old.parent.mkdir(parents=True)
    rows = "".join(
        f"| `{case_id}` | `switch` (switch_plug) | pass |\n"
        for case_id in ("on_then_off[switch_plug]", "on_then_off[switch_plug-2]", "on_then_off[switch_plug-3]")
    )
    old.write_text(f"# Shelly · turn_on_for\n\n## Tests\n\n| Test | Target | Result |\n|---|---|---|\n{rows}")
    assert engine.Record.read(old).passed == {
        "on_then_off[switch_plug]",
        "on_then_off[switch_plug-2]",
        "on_then_off[switch_plug-3]",
    }
    ha = FakeHA({PLUGS[0]: {"state": "off"}, PLUGS[1]: {"state": "off"}}, timer_on)
    registry = Roster({"shelly": {"turn_on_for", "get_attributes"}})
    result = await engine.run([steps], plugs(*PLUGS[:2]), registry, ha, tolerance=TOLERANCE, out=lambda _: None)
    assert not result.failed
    written, kept = await engine.write_records(
        result, registry, lambda integration: tmp_path / "records" / integration, "2026-11-15", "rig run"
    )
    assert (written, kept) == (
        [],
        [(old, ["on_then_off[switch_plug-3]"], "the record isn't JSON format 3")],
    )


def test_an_offline_second_plug_skips_only_its_own_case(tmp_path):
    inv = plugs(*PLUGS[:2])
    inv.offline = frozenset({PLUGS[1]})
    assert [(case.id, case.skip) for case in cases(suite(tmp_path, ON_THEN_OFF), inv)] == [
        ("on_then_off[switch_plug]", None),
        ("on_then_off[switch_plug-2]", f"{PLUGS[1]} unreachable"),
    ]


def test_a_powered_second_plug_leaves_the_lanes_and_the_first_stays(tmp_path):
    lamp = RigEntity("light.lamp", frozenset({"light_onoff"}), powered_by=PLUGS[1])
    inv = plugs(*PLUGS[:2], lamp=lamp)
    assert [in_head(case, inv) for case in cases(suite(tmp_path, ON_THEN_OFF), inv)] == [True, False]


def test_the_plan_the_check_and_the_platforms_cover_every_plug(tmp_path):
    steps = suite(tmp_path, ON_THEN_OFF)
    inv = plugs(*PLUGS)
    registry = Roster({"shelly": {"turn_on_for"}})
    assert rig.plan_lines([steps], inv, registry) == [
        f"shelly / turn_on_for -> {', '.join(PLUGS)}",
        f"    on_then_off[switch_plug]: {PLUGS[0]}",
        f"    on_then_off[switch_plug-2]: {PLUGS[1]}",
        f"    on_then_off[switch_plug-3]: {PLUGS[2]}",
    ]
    assert rig.targets([steps], inv, registry, None) == sorted(PLUGS)
    assert set(rig.platforms([steps], inv, registry, None)) == set(PLUGS)


# --- The format 2 record: a test per plug, each with its own device --------------------------------------------

PAIRED = deviceside_repo()
needs_schema = pytest.mark.skipif(not (PAIRED / SCHEMA).is_file(), reason=f"the paired checkout has no {SCHEMA}")


@needs_schema
async def test_the_record_holds_a_test_per_plug_each_naming_its_own_maker_string(tmp_path, monkeypatch):
    from . import test_rig_records_json as records_json

    makers = {PLUGS[0]: "_TZ3000_0zfrhq4i", PLUGS[1]: "_TZ3000_typdpbpg"}
    ieees = {PLUGS[0]: "0xa4c1380000000011", PLUGS[1]: "0xa4c1380000000012"}
    for entity_id, ieee in ieees.items():
        device = entity_id.partition(".")[2]
        monkeypatch.setitem(
            records_json.DEVICES,
            device,
            {
                "id": device,
                "identifiers": [["mqtt", f"zigbee2mqtt_{ieee}"]],
                "model_id": "TS011F_plug_1",
                "sw_version": "1.0",
            },
        )
        monkeypatch.setitem(records_json.ENTITIES, entity_id, device)
    bridge = [{"ieee_address": ieees[e], "model_id": "TS011F", "manufacturer": makers[e]} for e in PLUGS[:2]]
    ha = records_json.RegistryHA(
        {PLUGS[0]: {}, PLUGS[1]: {}},
        records_json.answer({e: {e: {"status": "sent", "capability": "countdown"}} for e in PLUGS[:2]}),
        bridge,
    )
    steps = suite(tmp_path, records_json.ONE_CALL.format(roles="switch_plug"), "zigbee2mqtt")
    result = await records_json.run_suite(ha, steps, plugs(*PLUGS[:2], integration="zigbee2mqtt"))
    [path] = await records_json.write(result, tmp_path / "records")
    tests = records_json.held(path)["tests"]
    assert tests == [
        {
            "id": case_id,
            "domain": "switch",
            "role": "switch_plug",
            "result": "pass",
            "devices": [{"model": "TS011F", "manufacturer": maker, "firmware": "1.0", "class": "countdown"}],
        }
        for case_id, maker in zip(("called[switch_plug]", "called[switch_plug-2]"), makers.values(), strict=True)
    ]
    assert json.loads(path.read_text())["result"] == "pass"
