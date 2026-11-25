"""A case calling an action DeviceSide doesn't build on its target's declared class (the profile's `class:`) skips
as not applying, decided at plan time from the checkout's scope (engine.unbuilt); a response naming another class fails
the case, and a record needs the declared class confirmed by a pass on the same entity (SuiteResult.recordable)."""

import dataclasses
import json
from pathlib import Path

import pytest

from ha_rig import engine, progress
from ha_rig.engine import CaseResult, Outcome, Suite, SuiteResult, load_step_file, run
from ha_rig.profile import Profile, RigEntity
from tests.rig_fakes import FakeHA, Roster, planned_cases

PLUG = "switch.plug"
BASE = Roster({"shelly": {"turn_on_for", "get_attributes"}}, classes=frozenset({"shelly"}))

# turn_on_for, which Shelly builds on Switch, beside a get_attributes call the tests make unbuilt there.
TEXT = """
action: turn_on_for
rig_seconds: 5
tests:
  - name: plain
    roles: [switch_plug]
    steps:
      - call: {duration: 1}
  - name: reads
    roles: [switch_plug]
    steps:
      - call: {duration: 1}
      - call: {action: get_attributes}
  - name: on_a_light
    roles: [light_dimmable]
    steps:
      - call: {duration: 1}
"""


class _Unbuilt:
    """An adapter whose scope leaves out some classes, as one with no entry for them has: not built there."""

    def __init__(self, adapter, without: set[str]) -> None:
        capabilities = {k: v for k, v in adapter.scope.capabilities.items() if k not in without}
        self.scope = dataclasses.replace(adapter.scope, capabilities=capabilities)
        self._adapter = adapter

    def __getattr__(self, name: str):
        return getattr(self._adapter, name)


class Registry:
    """BASE, with `action` not built on the classes `without` names."""

    def __init__(self, action: str = "get_attributes", without: frozenset[str] = frozenset({"Switch"})) -> None:
        self.action, self.without = action, without

    def adapter(self, action: str, integration: str):
        found = BASE.adapter(action, integration)
        return _Unbuilt(found, self.without) if found is not None and action == self.action else found

    def __getattr__(self, name: str):
        return getattr(BASE, name)


def suite(tmp_path: Path, text: str = TEXT) -> Suite:
    (path := tmp_path / "steps.yaml").write_text(text)
    return Suite(load_step_file(path), "shelly")


def declared(name: str | None = "Switch") -> Profile:
    return Profile({"shelly": [RigEntity(PLUG, frozenset({"switch_plug"}), declared_class=name)]}, name="house")


def planned(tmp_path, text: str = TEXT, inv: Profile | None = None, registry=None) -> dict[str, tuple]:
    steps = suite(tmp_path, text)
    registry = registry or Registry()
    found = planned_cases(steps, inv or declared(), None, engine.adapters_for(steps, registry), registry)
    return {case.id: (case.skip, case.inapplicable) for case in found}


def test_a_case_calling_an_action_not_built_on_the_declared_class_skips_as_not_applying(tmp_path):
    assert planned(tmp_path) == {
        "plain[switch_plug]": (None, False),
        "reads[switch_plug]": ("get_attributes is not built on Switch", True),
        "on_a_light[light_dimmable]": ("no light_dimmable entity in the profile", True),
    }


def test_no_declared_class_or_one_the_scope_serves_plans_every_case(tmp_path):
    for inv in (declared(None), declared("RGBCCT")):  # Shelly serves get_attributes on RGBCCT
        found = planned(tmp_path, inv=inv)
        assert found["plain[switch_plug]"] == found["reads[switch_plug]"] == (None, False)


def test_the_suites_own_action_counts(tmp_path):
    found = planned(tmp_path, registry=Registry("turn_on_for"))
    assert found["plain[switch_plug]"] == ("turn_on_for is not built on Switch", True)


@pytest.mark.parametrize(
    "call",
    [
        "{call: {action: get_attributes}, only: [zha]}",  # never runs on Shelly
        "{call: {action: get_attributes}, except_classes: {shelly: [Swi]}}",  # left out on the declared class
        "call: {action: get_attributes, target: light_color}",  # another role's target, not the case's own
        "{call: {action: get_attributes}, classes: {zha: [Swi]}}",  # a classes selector naming another integration
    ],
)
def test_a_call_that_wont_run_on_the_target_is_not_judged(tmp_path, call):
    text = TEXT.replace("      - call: {action: get_attributes}", f"      - {call}")
    inv = Profile(
        {
            "shelly": [
                RigEntity(PLUG, frozenset({"switch_plug"}), declared_class="Switch"),
                RigEntity("light.bulb", frozenset({"light_color"})),
            ]
        }
    )
    assert planned(tmp_path, text, inv)["reads[switch_plug]"] == (None, False)


def test_a_class_the_integration_doesnt_name_fails_the_plan(tmp_path):
    with pytest.raises(ValueError, match="'Plug' is not one shelly names"):
        planned(tmp_path, inv=declared("Plug"))


def answering(capability: str):
    def _respond(ha, action, data):
        if action == "turn_on_for":
            ha.set(data["entity_id"], "on")
        return {data["entity_id"]: {"status": "sent", "integration": "shelly", "capability": capability}}

    return _respond


async def run_suite(tmp_path, capability: str):
    ha = FakeHA({PLUG: {}}, answering(capability))
    tolerance = {"shelly": 0.2}
    result = await run([suite(tmp_path)], declared(), Registry(), ha, tolerance=tolerance, out=lambda _: None)
    return ha, result.suites[0]


async def test_a_run_skips_the_unbuilt_case_unsent_and_its_suite_can_record(tmp_path):
    ha, found = await run_suite(tmp_path, "Switch")
    by_id = {case.id: case for case in found.cases}
    assert by_id["plain[switch_plug]"].outcome is Outcome.PASS
    assert by_id["plain[switch_plug]"].declared == (PLUG, "Switch")
    skipped = by_id["reads[switch_plug]"]
    assert (skipped.outcome, skipped.detail, skipped.declared) == (
        Outcome.SKIPPED,
        "get_attributes is not built on Switch",
        (PLUG, "Switch"),
    )
    assert [service for domain, service, _ in ha.calls if domain == "deviceside"] == ["turn_on_for"]
    assert by_id["on_a_light[light_dimmable]"].outcome is Outcome.SKIPPED  # a role the profile lacks needs no pass
    assert found.recordable


async def test_a_response_naming_another_class_fails_the_case(tmp_path):
    _, found = await run_suite(tmp_path, "RGBCCT")
    plain = next(case for case in found.cases if case.id == "plain[switch_plug]")
    assert plain.outcome is Outcome.FAIL
    assert "DeviceSide named the target's class RGBCCT; the profile declares Switch" in plain.detail
    assert not found.recordable


@pytest.mark.parametrize(
    ("passed", "recordable"),
    [
        ((PLUG, "Switch"), True),
        (None, False),  # the pass named no class: the skip's class is unconfirmed
        (("switch.other", "Switch"), False),  # another entity's class confirms nothing here
    ],
)
def test_an_unbuilt_skip_needs_its_class_confirmed_by_a_pass(tmp_path, passed, recordable):
    results = [
        CaseResult("plain[switch_plug]", "switch_plug", "switch", Outcome.PASS, declared=passed),
        CaseResult(
            "reads[switch_plug]", "switch_plug", "switch", Outcome.SKIPPED, inapplicable=True, declared=(PLUG, "Switch")
        ),
    ]
    assert SuiteResult(suite(tmp_path), results, True).recordable is recordable


def test_a_saved_case_keeps_its_declared_class():
    case = CaseResult("reads[switch_plug]", "switch_plug", "switch", Outcome.SKIPPED, declared=(PLUG, "Switch"))
    assert progress._result(json.loads(json.dumps(progress._saved(case)))) == case


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("{entity_id: light.room, roles: [group], class: hue}", "is a group"),
        ("{entity_id: switch.plug, roles: [switch_plug], class: 3}", "class must be a DeviceSide class name"),
    ],
)
def test_a_profile_refuses_a_class_on_a_group_or_not_a_name(tmp_path, entry, message):
    (path := tmp_path / "p.yaml").write_text(f"shelly:\n  - {entry}\n")
    with pytest.raises(ValueError, match=message):
        Profile.load(path)


def test_a_profile_reads_an_entitys_class(tmp_path):
    (path := tmp_path / "p.yaml").write_text(
        "shelly:\n  - {entity_id: switch.plug, roles: [switch_plug], class: Switch}\n"
    )
    assert Profile.load(path).declared_class("shelly", PLUG) == "Switch"


def row(case_id: str, name: str, model: str) -> dict:
    """A record's passing row of the case, on one device of class `name`."""
    device = {"model": model, "manufacturer": "Maker", "firmware": None, "class": name}
    return {"id": case_id, "domain": "switch", "role": "switch_plug", "result": "pass", "devices": [device]}


SKIPPED = CaseResult(
    "reads[switch_plug]",
    "switch_plug",
    "switch",
    Outcome.SKIPPED,
    "get_attributes is not built on Switch",
    inapplicable=True,
    declared=(PLUG, "Switch"),
)


def test_a_carried_row_on_the_class_now_not_built_is_dropped_and_another_class_stays():
    other, same = row("reads[switch_plug]", "hue", "LCA001"), row("reads[switch_plug]", "Switch", "S3")
    kept, dropped = engine.unbuilt_rows([other, same, row("plain[switch_plug]", "Switch", "S3")], [SKIPPED])
    assert [(r["id"], r["devices"][0]["class"]) for r in kept] == [
        ("reads[switch_plug]", "hue"),
        ("plain[switch_plug]", "Switch"),
    ]
    assert dropped == [("reads[switch_plug]", "Switch")]


def test_a_run_says_which_row_it_dropped_and_carries_the_other_class(tmp_path):
    rows = [row("reads[switch_plug]", "hue", "LCA001"), row("reads[switch_plug]", "Switch", "S3")]
    path = tmp_path / "turn_on_for.json"
    path.write_text(json.dumps({"format": engine.CALLS_FORMAT, "calls": "b" * 40, "tests": rows}))
    passed = CaseResult("plain[switch_plug]", "switch_plug", "switch", Outcome.PASS, declared=(PLUG, "Switch"))
    said: list[str] = []
    old, carried, why = engine._prior(
        SuiteResult(suite(tmp_path), [passed, SKIPPED], True), path, object(), "b" * 40, said.append
    )
    assert why is None and carried == ["reads[switch_plug]"]
    assert [r["devices"][0]["class"] for r in old if r["id"] == "reads[switch_plug]"] == ["hue"]
    assert (
        "shelly / turn_on_for: reads[switch_plug]: passed on Switch in the record, now not built: row dropped" in said
    )
