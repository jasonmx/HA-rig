"""JSON format 2 test records and `_facts.json`: what the rig writes for a checkout with deviceside's record schema,
each file round-tripped through the paired checkout's own schema."""

import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ha_rig import engine, progress, schema
from ha_rig.engine import (
    CaseResult,
    Outcome,
    Reached,
    RunResult,
    SuiteResult,
    load_step_file,
    reached_devices,
    record_manufacturer,
    record_model,
    run,
    write_fact,
    write_records,
)
from ha_rig.facts import FactsFile
from ha_rig.profile import Profile, RigEntity, normalize_ieee
from ha_rig.schema import InvalidRecord, RecordSchema
from ha_rig.target import SCHEMA, deviceside_repo

from .rig_fakes import FakeHA, Roster, profile
from .test_rig_engine import TOLERANCE, outcomes, suite, under

PAIRED = deviceside_repo()
if not (PAIRED / SCHEMA).is_file():
    pytest.skip(f"the paired deviceside checkout has no {SCHEMA}: its records are markdown", allow_module_level=True)

CONTRACT = Draft202012Validator(json.loads((PAIRED / SCHEMA).read_text()))  # deviceside's, read apart from the rig's
SCHEMA_OF = RecordSchema.load(PAIRED)
# Z2M and Shelly name classes, as on deviceside's device-classes branches; ZHA and Hue don't yet.
REGISTRY = Roster(
    {
        "shelly": {"turn_on_for", "get_attributes"},
        "zha": {"turn_on_for", "get_attributes", "set_attributes"},
        "zigbee2mqtt": {"turn_on_for", "get_attributes", "set_attributes"},
        "hue": {"set_scene", "get_attributes", "set_attributes"},
    },
    classes=frozenset({"shelly", "zigbee2mqtt"}),
)
PACKAGE = "0123456789ab"  # a package's git tree, abbreviated as the runner does
HUE_BULB = "0x0017880100000001"
TUYA_PLUG = "0xa4c1380000000001"
OTHER_BULB = "0x0017880100000002"
# HA's device registry as each integration fills it: Z2M's model_id is its product code, ZHA's model the bulb's own.
DEVICES = {
    "plug": {
        "id": "plug",
        "model": "Shelly 1 Mini Gen3",
        "model_id": "S3SW-001X8EU",
        "sw_version": "1.7.4",
        "manufacturer": "Shelly",
    },
    "z2m_a": {
        "id": "z2m_a",
        "identifiers": [["mqtt", f"zigbee2mqtt_{HUE_BULB}"]],
        "model": "Hue White A60 B22",
        "model_id": "9290018217",
        "manufacturer": "Philips",  # the definition's vendor, never written
        "sw_version": "1.116.3",
    },
    "z2m_b": {
        "id": "z2m_b",
        "identifiers": [["mqtt", f"zigbee2mqtt_{OTHER_BULB}"]],
        "model": "Hue Essential White and Color Ambiance A60",
        "model_id": "8720169392182",
        "sw_version": "",  # Z2M has read none
    },
    "z2m_group": {"id": "z2m_group", "identifiers": [["mqtt", "zigbee2mqtt_1"]], "model": "Group", "sw_version": "2.1"},
    "zha_bulb": {
        "id": "zha_bulb",
        "identifiers": [["zha", "00:17:88:01:00:00:00:03"]],
        "model": "LWA012",
        "manufacturer": "Signify Netherlands B.V.",
    },
    "hue_bulb": {
        "id": "hue_bulb",
        "identifiers": [["hue", "abc"]],
        "model": "Hue white lamp",
        "model_id": "LWA012",
        "manufacturer": "Signify Netherlands B.V.",
        "sw_version": "1.122.2",
    },
}
ENTITIES = {
    "switch.plug": "plug",
    "light.z2m_a": "z2m_a",
    "light.z2m_b": "z2m_b",
    "light.z2m_room": "z2m_group",
    "light.zha_bulb": "zha_bulb",
    "light.hue_bulb": "hue_bulb",
}
# Z2M's bridge/devices: each device's Zigbee model ID, the Basic cluster's.
BRIDGE_DEVICES = [
    {"ieee_address": HUE_BULB, "model_id": "LWA012", "manufacturer": "Signify Netherlands B.V."},
    {"ieee_address": OTHER_BULB, "model_id": "LCA016"},  # Z2M reports no maker string for it
]


class RegistryHA(FakeHA):
    """A FakeHA with a device registry holding DEVICES, and Z2M's bridge/devices."""

    def __init__(self, states: dict[str, dict], respond=None, bridge_devices: list[dict] | None = None):
        super().__init__(states, respond)
        self.devices = list(DEVICES.values())
        self.bridge_devices = BRIDGE_DEVICES if bridge_devices is None else bridge_devices

    async def command(self, payload: dict):
        if payload["type"] == "config/entity_registry/list_for_display":
            return {"entities": [{"ei": entity_id, "di": device_id} for entity_id, device_id in ENTITIES.items()]}
        return await super().command(payload)

    async def z2m_device_list(self) -> list[dict]:
        return self.bridge_devices


def answer(respond_keys: dict[str, dict]):
    """DeviceSide's turn_on_for: the light on, then off, and the response `respond_keys` gives for the target."""

    def respond(ha, action, data):
        if action == "get_attributes":
            return {}
        entity_id = data["entity_id"]
        ha.set(entity_id, "on")
        ha.later(data["duration"]["seconds"], entity_id, "off")
        return respond_keys[entity_id]

    return respond


ONE_CALL = """
action: turn_on_for
rig_seconds: 5
tests:
  - name: called
    roles: [{roles}]
    steps:
      - call: {{duration: 0.3}}
"""
KINDS = """
action: turn_on_for
rig_seconds: 5
tests:
  - name: called
    roles: [switch_plug, light_ct]
    steps:
      - call: {duration: 0.3}
  - name: wins
    roles: [switch_plug]
    firmware_fact: true
    steps:
      - wait: 0
"""


async def run_suite(ha, steps, inv: Profile) -> RunResult:
    return await run([steps], inv, REGISTRY, ha, admin=ha, tolerance=TOLERANCE, out=lambda _: None, json_records=True)


async def write(result: RunResult, records: Path, printed: list[str] | None = None, **kwargs) -> list[Path]:
    """write_records in format 2, from a run at commit abc1234 whose packages are each tree PACKAGE."""
    written, _ = await write_records(
        result,
        REGISTRY,
        under(records),
        "2026-11-14",
        "rig run",
        package=lambda key: PACKAGE,
        schema=SCHEMA_OF,
        commit="abc1234",
        out=(printed if printed is not None else []).append,
        **kwargs,
    )
    return written


def held(path: Path) -> dict:
    """The file at `path` as deviceside reads it: it holds to deviceside's schema, as indent 2 with a final newline."""
    text = path.read_text()
    data = json.loads(text)
    assert list(CONTRACT.iter_errors(data)) == []
    assert text == json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return data


async def test_a_record_lists_each_case_kind_and_holds_to_deviceside_schema(tmp_path):
    ha = RegistryHA(
        {"switch.plug": {}}, answer({"switch.plug": {"switch.plug": {"status": "sent", "capability": "Switch"}}})
    )
    steps = suite(tmp_path, KINDS, facts=False)
    result = await run_suite(ha, steps, profile(switch_plug="switch.plug"))
    [path] = await write(result, tmp_path / "records")
    assert path == tmp_path / "records" / "shelly" / "turn_on_for.json"
    data = held(path)
    assert {key: value for key, value in data.items() if key != "tests"} == {
        "format": 2,
        "integration": "shelly",
        "action": "turn_on_for",
        "result": "pass",
        "date": "2026-11-14",
        "source": {"kind": "rig", "commit": "abc1234"},
        "package": PACKAGE,
        "home_assistant": "2026.10.1",
    }
    assert list(data) == [
        "format",
        "integration",
        "action",
        "result",
        "date",
        "source",
        "package",
        "home_assistant",
        "tests",
    ]
    assert data["tests"] == [
        {
            "id": "called[switch_plug]",
            "domain": "switch",
            "role": "switch_plug",
            "result": "pass",
            "devices": [{"model": "S3SW-001X8EU", "manufacturer": "Shelly", "firmware": "1.7.4", "class": "Switch"}],
        },
        {
            "id": "called[light_ct]",
            "role": "light_ct",
            "result": "skipped",
            "reason": "no light_ct entity in the profile",
        },
        {"id": "wins[switch_plug]", "role": "switch_plug", "result": "skipped", "fact": True},
    ]
    assert not (path.parent / "turn_on_for.md").exists()
    assert engine.Record.read(path) == engine.Record(
        path,
        "abc1234",
        "2026-11-14",
        frozenset({"called[switch_plug]", "called[light_ct]", "wins[switch_plug]"}),
        frozenset({"called[switch_plug]"}),
        frozenset({"called[light_ct]", "wins[switch_plug]"}),
    )


def z2m_group(members_answer: dict) -> tuple[RegistryHA, Profile]:
    ha = RegistryHA(
        {"light.z2m_room": {}, "light.z2m_a": {}, "light.z2m_b": {}}, answer({"light.z2m_room": members_answer})
    )
    room = RigEntity("light.z2m_room", frozenset({"group"}), members=("light.z2m_a", "light.z2m_b"))
    return ha, Profile({"zigbee2mqtt": [room]}, {})


async def group_test(tmp_path, members_answer: dict) -> dict:
    ha, inv = z2m_group(members_answer)
    result = await run_suite(ha, suite(tmp_path, ONE_CALL.format(roles="group"), "zigbee2mqtt"), inv)
    assert outcomes(result) == {"called[group]": (Outcome.PASS, "")}
    [path] = await write(result, tmp_path / "records")
    [test] = held(path)["tests"]
    return test


async def test_a_native_groups_test_lists_its_members_with_their_zigbee_model_ids_and_class(tmp_path):
    test = await group_test(tmp_path, {"light.z2m_room": {"status": "sent", "capabilities": ["hue"]}})
    assert test == {
        "id": "called[group]",
        "domain": "light",
        "role": "group",
        "result": "pass",
        "group": True,
        # Z2M's bridge/devices model IDs, never the registry's product codes, and never the group's own device.
        "devices": [
            {"model": "LWA012", "manufacturer": "Signify Netherlands B.V.", "firmware": "1.116.3", "class": "hue"},
            {"model": "LCA016", "manufacturer": None, "firmware": None, "class": "hue"},
        ],
    }


async def test_a_group_listing_several_classes_names_none_for_a_member(tmp_path):
    test = await group_test(tmp_path, {"light.z2m_room": {"status": "sent", "capabilities": ["hue", "other"]}})
    assert [device["class"] for device in test["devices"]] == [None, None]


async def test_a_group_answered_member_by_member_gives_each_its_own_class(tmp_path):
    response = {"light.z2m_a": {"status": "sent", "capability": "hue"}, "light.z2m_b": {"status": "sent"}}
    test = await group_test(tmp_path, response)  # light.z2m_b isn't placed yet: its entry names no class
    assert [device["class"] for device in test["devices"]] == ["hue", None]


@pytest.mark.parametrize(
    ("integration", "entity_id", "model", "firmware"),
    [("zha", "light.zha_bulb", "LWA012", None), ("hue", "light.hue_bulb", "LWA012", "1.122.2")],
)
async def test_zha_and_hue_name_the_zigbee_model_id_and_no_class(tmp_path, integration, entity_id, model, firmware):
    """ZHA's registry `model` and Hue's `model_id` (the bridge's product_data.model_id) are the Zigbee model ID; neither
    integration names classes, so the class is null whatever the response carries."""
    response = {entity_id: {entity_id: {"status": "sent", "capability": "hue"}}}
    ha = RegistryHA({entity_id: {}}, answer(response))
    text = ONE_CALL.format(roles="light_ct")
    if integration == "hue":
        text = text.replace("turn_on_for", "set_scene")  # Hue has no turn_on_for adapter
    steps = suite(tmp_path, text, integration)
    result = await run_suite(ha, steps, profile(integration, light_ct=entity_id))
    [path] = await write(result, tmp_path / "records")
    [test] = held(path)["tests"]
    maker = "Signify Netherlands B.V."
    assert test["devices"] == [{"model": model, "manufacturer": maker, "firmware": firmware, "class": None}]


def test_the_model_id_follows_the_device_not_the_suite():
    z2m = {normalize_ieee(HUE_BULB): {"model_id": "LWA012"}}
    assert record_model(DEVICES["z2m_a"], z2m) == "LWA012"
    assert record_model(DEVICES["z2m_b"], z2m) is None  # not in bridge/devices: the record can't name it
    assert record_model(DEVICES["zha_bulb"], z2m) == "LWA012"
    assert record_model(DEVICES["hue_bulb"], z2m) == "LWA012"
    assert record_model(DEVICES["plug"], z2m) == "S3SW-001X8EU"


def test_the_maker_string_is_the_integrations_never_a_device_name():
    z2m = {normalize_ieee(TUYA_PLUG): {"model_id": "TS011F", "manufacturer": "_TZ3000_okaz9tjs"}}
    plug = {"id": "tuya", "identifiers": [["mqtt", f"zigbee2mqtt_{TUYA_PLUG}"]], "manufacturer": "Tuya", "name": "Lamp"}
    assert (record_model(plug, z2m), record_manufacturer(plug, z2m)) == ("TS011F", "_TZ3000_okaz9tjs")
    assert record_manufacturer(DEVICES["z2m_b"], z2m) is None  # not in bridge/devices
    assert record_manufacturer(DEVICES["zha_bulb"], z2m) == "Signify Netherlands B.V."
    assert record_manufacturer(DEVICES["hue_bulb"], z2m) == "Signify Netherlands B.V."
    assert record_manufacturer({"id": "x", "manufacturer": ""}, z2m) is None


async def test_reached_devices_reads_bridge_devices_only_for_a_z2m_device():
    class Counting(RegistryHA):
        reads = 0

        async def z2m_device_list(self):
            self.reads += 1
            return await super().z2m_device_list()

    ha = Counting({})
    assert await reached_devices(ha, ["switch.plug"], {"switch.plug": "Switch"}) == (
        Reached("S3SW-001X8EU", "1.7.4", "Switch", "Shelly"),
    )
    assert ha.reads == 0
    found = await reached_devices(ha, ["light.z2m_a", "light.z2m_b"], {})
    signify = "Signify Netherlands B.V."
    assert found == (Reached("LWA012", "1.116.3", None, signify), Reached("LCA016", None)) and ha.reads == 1
    with pytest.raises(LookupError, match="light.unknown"):
        await reached_devices(ha, ["light.unknown"], {})


async def test_a_record_naming_a_device_with_no_model_id_is_not_written(tmp_path):
    ha, inv = z2m_group({"light.z2m_room": {"status": "sent", "capabilities": ["hue"]}})
    ha.bridge_devices = BRIDGE_DEVICES[:1]  # bridge/devices lacks light.z2m_b's bulb
    said_then: list[str] = []
    steps = suite(tmp_path, ONE_CALL.format(roles="group"), "zigbee2mqtt")
    result = await run(
        [steps], inv, REGISTRY, ha, admin=ha, tolerance=TOLERANCE, out=said_then.append, json_records=True
    )
    assert "zigbee2mqtt / called[group]: a device it reached has no model ID in HA's registry" in "\n".join(said_then)
    printed: list[str] = []
    assert await write(result, tmp_path / "records", printed) == []
    assert not (tmp_path / "records" / "zigbee2mqtt" / "turn_on_for.json").exists()
    [said] = printed
    assert said.startswith("zigbee2mqtt / turn_on_for: turn_on_for.json doesn't hold to deviceside's ")
    assert "tests/0/devices/1/model" in said


async def test_a_case_that_ran_with_no_devices_noted_writes_no_record(tmp_path):
    ha = FakeHA({"light.bulb": {}}, answer({"light.bulb": {"light.bulb": {"status": "sent"}}}))  # no registry entry
    result = await run_suite(ha, suite(tmp_path, ONE_CALL.format(roles="light_ct")), profile(light_ct="light.bulb"))
    assert result.suites[0].recordable
    printed: list[str] = []
    assert await write(result, tmp_path / "records", printed) == []
    assert printed == [
        "shelly / turn_on_for: no record, as no devices were noted for called[light_ct] (the registry lookup failed, "
        "or an older rig saved them): a run at this commit reruns those cases"
    ]


def canned(text: str, cases: list[CaseResult], tmp_path, integration: str = "shelly") -> RunResult:
    path = tmp_path / "steps.yaml"
    path.write_text(text)
    steps = engine.Suite(load_step_file(path), integration)
    return RunResult([SuiteResult(steps, cases, complete=True, ha_version="2026.10.4")])


OPTIONS = """
action: stop
options: [predicted_state]
rig_seconds: 5
tests:
  - name: halts
    roles: [light_ct]
    steps:
      - wait: 0
"""


async def test_an_option_files_record_is_named_for_it_and_lists_its_options(tmp_path):
    halts = CaseResult(
        "halts[light_ct]", "light_ct", "light", Outcome.PASS, reached=(Reached("LTA009", "1.163.1", "hue"),)
    )
    [path] = await write(canned(OPTIONS, [halts], tmp_path, "zigbee2mqtt"), tmp_path / "records")
    assert path.name == "stop-predicted_state.json"
    data = held(path)
    assert (data["action"], data["options"]) == ("stop", ["predicted_state"])


async def test_a_record_never_narrows_a_json_record(tmp_path):
    """A converted format 1 record that passed the switch case keeps it from a profile without the role."""
    old = tmp_path / "records" / "shelly" / "turn_on_for.json"
    old.parent.mkdir(parents=True)
    old.write_text(
        json.dumps(
            {
                "format": 1,
                "source": {"kind": "rig", "commit": "cbc2156"},
                "date": "2026-11-07",
                "tests": [{"id": "lit[switch_plug]", "role": "switch_plug", "domain": "switch", "result": "pass"}],
            }
        )
    )
    skipped = CaseResult(
        "lit[switch_plug]", "switch_plug", None, Outcome.SKIPPED, "no switch_plug entity in the profile"
    )
    skipped.inapplicable = skipped.absent = True
    passed = CaseResult("lit[light_ct]", "light_ct", "light", Outcome.PASS, reached=(Reached("S3BL", "2.0", "RGBCCT"),))
    text = ONE_CALL.format(roles="switch_plug, light_ct").replace("called", "lit")
    written, kept = await write_records(
        canned(text, [skipped, passed], tmp_path),
        REGISTRY,
        under(tmp_path / "records"),
        "2026-11-14",
        "rig run",
        schema=SCHEMA_OF,
        commit="abc1234",
    )
    assert (written, kept) == ([], [(old, ["lit[switch_plug]"])])


def test_a_json_record_the_rig_cant_read_holds_nothing(tmp_path):
    path = tmp_path / "stop.json"
    path.write_text("not json")
    assert engine.Record.read(path) == engine.Record(path, None, None, frozenset())


FACT = """
action: turn_on_for
rig_seconds: 5
tests:
  - name: wins
    roles: [switch_plug]
    firmware_fact: true
    steps:
      - wait: 0
"""


def a_fact(tmp_path) -> tuple[engine.Suite, CaseResult]:
    path = tmp_path / "steps.yaml"
    path.write_text(FACT)
    steps = engine.Suite(load_step_file(path), "shelly", facts=True)
    case = CaseResult(
        "wins[switch_plug]",
        "switch_plug",
        "switch",
        Outcome.FAIL,
        reached=(Reached("S3SW-001X8EU", "1.7.4", "Switch"),),
    )
    return steps, case


def test_a_fact_starts_facts_json_and_appends_leaving_every_row_as_it_was(tmp_path):
    steps, case = a_fact(tmp_path)
    records = under(tmp_path / "records")
    converted = {
        "date": "2026-11-13",
        "suite": "stop",
        "test": "halts[light_ct] observed",
        "kind": "observed",
        "result": "pass",
        "devices": [{"model": "LTA009", "name": "Hue white ambiance", "firmware": "1.163.1"}],
    }
    path = write_fact(steps, case, REGISTRY, records, "2026-11-14", SCHEMA_OF)
    assert path == tmp_path / "records" / "shelly" / "_facts.json"
    data = held(path)
    data["facts"].insert(0, converted)
    path.write_text(json.dumps(data))
    assert write_fact(steps, case, REGISTRY, records, "2026-11-15", SCHEMA_OF) == path
    row = {
        "date": "2026-11-14",
        "suite": "turn_on_for",
        "test": "wins[switch_plug]",
        "kind": "automated",
        "result": "fail",
        "devices": [{"model": "S3SW-001X8EU", "manufacturer": None, "firmware": "1.7.4", "class": "Switch"}],
    }
    assert held(path) == {
        "format": 2,
        "integration": "shelly",
        "facts": [converted, row, row | {"date": "2026-11-15"}],
    }
    assert not (path.parent / "_facts.md").exists()
    rows = FactsFile.read(path).rows
    assert [(fact.suite, fact.test, fact.result) for fact in rows][-1] == ("turn_on_for", "wins[switch_plug]", "fail")
    assert str(rows[0]) == "last on file 2026-11-13, on `LTA009`, firmware `1.163.1`: pass"


def test_a_facts_file_the_rig_cant_read_is_left_alone_and_the_row_said(tmp_path):
    steps, case = a_fact(tmp_path)
    path = tmp_path / "records" / "shelly" / "_facts.json"
    path.parent.mkdir(parents=True)
    path.write_text("{oops")
    printed: list[str] = []
    assert (
        write_fact(steps, case, REGISTRY, under(tmp_path / "records"), "2026-11-14", SCHEMA_OF, printed.append) is None
    )
    assert path.read_text() == "{oops"
    [said] = printed
    assert said.startswith("Not written to _facts.json (") and '"test": "wins[switch_plug]"' in said
    assert FactsFile.read(path).rows == ()


def test_a_fact_with_no_devices_noted_or_a_row_the_schema_refuses_is_not_written(tmp_path):
    steps, case = a_fact(tmp_path)
    records = under(tmp_path / "records")
    case.reached = ()
    printed: list[str] = []
    assert write_fact(steps, case, REGISTRY, records, "2026-11-14", SCHEMA_OF, printed.append) is None
    assert printed == [
        "Not written to _facts.json: turn_on_for / wins[switch_plug] found fail, but its devices couldn't be read"
    ]
    case.reached = (Reached(None, "1.7.4", "Switch"),)
    assert write_fact(steps, case, REGISTRY, records, "2026-11-14", SCHEMA_OF, printed.append) is None
    assert not (tmp_path / "records" / "shelly" / "_facts.json").exists()
    assert "facts/0/devices/0/model" in printed[1]


def test_the_ledger_saves_what_each_case_reached(tmp_path):
    path = tmp_path / "steps.yaml"
    path.write_text(FACT)
    steps = engine.Suite(load_step_file(path), "shelly")
    saved = progress.Progress.load(tmp_path, "lab")
    saved.track(steps, "c1")
    case = CaseResult("wins[switch_plug]", "switch_plug", "switch", Outcome.PASS, entity_ids=("switch.plug",))
    case.reached = (Reached("S3SW-001X8EU", None, "Switch"),)
    saved.record(steps, case)
    again = progress.Progress.load(tmp_path, "lab")
    again.track(steps, "c1")
    assert again.saved(steps)["wins[switch_plug]"].reached == case.reached


def test_a_resume_for_json_records_reruns_a_case_saved_with_no_devices(tmp_path):
    path = tmp_path / "steps.yaml"
    path.write_text(FACT.replace("    firmware_fact: true\n", ""))
    steps = engine.Suite(load_step_file(path), "shelly")
    [case] = engine.cases(steps, profile(switch_plug="switch.plug"))
    blind = CaseResult(case.id, "switch_plug", "switch", Outcome.PASS, entity_ids=("switch.plug",))
    assert progress.holds(case, blind, unattended=False)  # markdown records name no case's devices
    assert not progress.holds(case, blind, unattended=False, devices=True)
    blind.reached = (Reached("S3SW-001X8EU", None, "Switch"),)
    assert progress.holds(case, blind, unattended=False, devices=True)


async def test_a_markdown_run_reads_no_case_devices(tmp_path):
    ha = RegistryHA({"switch.plug": {}}, answer({"switch.plug": {"switch.plug": {"status": "sent"}}}))
    steps = suite(tmp_path, ONE_CALL.format(roles="switch_plug"))
    result = await run([steps], profile(switch_plug="switch.plug"), REGISTRY, ha, tolerance=TOLERANCE, out=print)
    assert [case.reached for case in result.suites[0].cases] == [()]


async def test_json_records_need_the_runs_commit(tmp_path):
    with pytest.raises(ValueError, match="commit"):
        await write_records(RunResult([]), REGISTRY, under(tmp_path), "d", "rig run", schema=SCHEMA_OF)


def test_a_checkout_without_the_schema_writes_markdown(tmp_path):
    assert RecordSchema.load(tmp_path) is None


def test_a_schema_without_jsonschema_or_unreadable_stops_the_run(tmp_path, monkeypatch):
    (tmp_path / SCHEMA).parent.mkdir(parents=True)
    (tmp_path / SCHEMA).write_text('{"type": 5}')
    with pytest.raises(SystemExit, match="Could not load"):
        RecordSchema.load(tmp_path)
    monkeypatch.setitem(sys.modules, "jsonschema", None)
    with pytest.raises(SystemExit, match="uv sync"):
        RecordSchema.load(tmp_path)


def test_write_refuses_a_document_the_schema_refuses(tmp_path):
    with pytest.raises(InvalidRecord, match="at the top level"):
        SCHEMA_OF.write(tmp_path / "x.json", {"format": 2})
    assert not (tmp_path / "x.json").exists()
    assert schema.dumps({"model": "é"}) == '{\n  "model": "é"\n}\n'
