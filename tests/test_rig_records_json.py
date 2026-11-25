"""JSON test records and `_facts.json`: what the rig writes for a checkout with deviceside's record schema, each file
round-tripped through the paired checkout's own schema. A record is format 3 (bound to the action's call transcript)
against a schema that lists it, format 2 (bound to the package tree) against one that doesn't, and where the action has
no transcript. The paired schema is patched to be each, so the tests hold whichever deviceside it is paired with."""

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
from tests.rig_fakes import planned_cases

from .rig_fakes import FakeHA, Roster, profile
from .test_rig_engine import TOLERANCE, outcomes, suite, under

PAIRED = deviceside_repo()
if not (PAIRED / SCHEMA).is_file():
    pytest.skip(f"the paired deviceside checkout has no {SCHEMA}: its records are markdown", allow_module_level=True)

CONTRACT = Draft202012Validator(json.loads((PAIRED / SCHEMA).read_text()))  # deviceside's, read apart from the rig's
PAIRED_DOCUMENT = json.loads((PAIRED / SCHEMA).read_text())
SCHEMA_OF = RecordSchema.load(PAIRED)


def schema_listing(formats: list[int]) -> RecordSchema:
    """The paired schema with its record's formats narrowed to `formats`: an older deviceside's takes [1, 2]."""
    document = json.loads(json.dumps(PAIRED_DOCUMENT))
    document["$defs"]["record"]["properties"]["format"]["enum"] = formats
    return RecordSchema.parse(PAIRED / SCHEMA, json.dumps(document))


SCHEMA_2 = schema_listing([1, 2])  # deviceside from before format 3
needs_format_3 = pytest.mark.skipif(not SCHEMA_OF.binds_calls, reason="the paired deviceside schema has no format 3")
BLOB = "0123456789abcdef0123456789abcdef01234567"  # a call transcript's git blob id, as git rev-parse prints it
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
FALLBACK_BULB = "0x0017880100000003"
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
    "z2m_fallback": {
        "id": "z2m_fallback",
        "identifiers": [["mqtt", f"zigbee2mqtt_{FALLBACK_BULB}"]],
        "model": "Hue Essential White and Color Ambiance A60",
        "model_id": "8720169392182",
        "sw_version": "Zigbee2MQTT 2.14.3",  # Z2M's discovery fallback, before it reads a build ID
    },
    "other_zigbee2mqtt": {
        "id": "other_zigbee2mqtt",
        "identifiers": [["hue", "xyz"]],
        "model": "Hue white lamp",
        "model_id": "LWA012",
        "sw_version": "Zigbee2MQTT 2.14.3",  # not a Z2M device: its firmware is its own
    },
    "z2m_group": {"id": "z2m_group", "identifiers": [["mqtt", "zigbee2mqtt_1"]], "model": "Group", "sw_version": "2.1"},
    "zha_bulb": {
        "id": "zha_bulb",
        "identifiers": [["zha", "00:17:88:01:00:00:00:03"]],
        "primary_config_entry": "zha_entry",
        "model": "LWA012",
        "manufacturer": "Signify Netherlands B.V.",
    },
    # A quirks v2 device: zha hands HA's registry its friendly name, never the Basic cluster's strings.
    "zha_plug": {
        "id": "zha_plug",
        "identifiers": [["zha", "a4:c1:38:00:00:00:00:02"]],
        "config_entries": ["zha_entry"],
        "model": "Smart plug",
        "manufacturer": "Tuya",
    },
    "zha_unread": {
        "id": "zha_unread",
        "identifiers": [["zha", "a4:c1:38:00:00:00:00:03"]],
        "primary_config_entry": "zha_entry",
        "model": "unk_model",
        "manufacturer": "unk_manufacturer",
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
    "light.z2m_fallback": "z2m_fallback",
    "light.other_zigbee2mqtt": "other_zigbee2mqtt",
    "light.z2m_room": "z2m_group",
    "light.zha_bulb": "zha_bulb",
    "light.hue_bulb": "hue_bulb",
    "light.hue_b": "hue_b",
    "light.hue_room": "hue_room",
    "scene.hue_relax": "hue_room",  # a Hue scene's entity sits on its room's device
    "switch.zha_plug": "zha_plug",
    "switch.zha_unread": "zha_unread",
}
DEVICES["hue_b"] = DEVICES["hue_bulb"] | {"id": "hue_b", "identifiers": [["hue", "def"]], "model_id": "LCA016"}
DEVICES["hue_room"] = {"id": "hue_room", "identifiers": [["hue", "room"]], "model": "Room"}
# ZHA's device diagnostics, by device: zigpy's own Basic-cluster strings, beside zha's friendly ones.
DIAGNOSTICS = {
    "zha_bulb": {"manufacturer": "Signify Netherlands B.V.", "model": "LWA012"},
    "zha_plug": {
        "manufacturer": "_TZ3000_okaz9tjs",
        "model": "TS011F",
        "friendly_manufacturer": "Tuya",
        "friendly_model": "Smart plug",
    },
    "zha_unread": {"manufacturer": None, "model": None, "friendly_manufacturer": "unk_manufacturer"},
}
# Z2M's bridge/devices: each device's Zigbee model ID, the Basic cluster's.
BRIDGE_DEVICES = [
    {"ieee_address": HUE_BULB, "model_id": "LWA012", "manufacturer": "Signify Netherlands B.V."},
    {"ieee_address": OTHER_BULB, "model_id": "LCA016"},  # Z2M reports no maker string for it
    {"ieee_address": FALLBACK_BULB, "model_id": "LWA012"},
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

    async def rest(self, method: str, path: str, json: dict | None = None):
        prefix = "/api/diagnostics/config_entry/zha_entry/device/"
        if method == "get" and path.startswith(prefix):
            return {"home_assistant": {}, "data": DIAGNOSTICS[path.removeprefix(prefix)]}
        raise AssertionError(f"unexpected REST call {method} {path}")


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


async def write(
    result: RunResult, records: Path, printed: list[str] | None = None, registry=REGISTRY, **kwargs
) -> list[Path]:
    """write_records against a schema that takes only format 2 (`schema=` another), from a run at commit abc1234
    whose packages are each tree PACKAGE."""
    kwargs = {"schema": SCHEMA_2, **kwargs}
    written, _ = await write_records(
        result,
        registry,
        under(records),
        "2026-11-14",
        "rig run",
        package=lambda key: PACKAGE,
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


async def test_a_checkout_without_format_3_gets_format_2_whatever_the_transcripts(tmp_path):
    ha = RegistryHA({"switch.plug": {}}, answer({"switch.plug": {"switch.plug": {"status": "sent"}}}))
    result = await run_suite(
        ha, suite(tmp_path, ONE_CALL.format(roles="switch_plug")), profile(switch_plug="switch.plug")
    )
    [path] = await write(result, tmp_path / "records", calls=lambda integration, action: BLOB)
    data = held(path)
    assert (data["format"], data["package"], "calls" in data) == (2, PACKAGE, False)


@needs_format_3
async def test_a_checkout_with_format_3_binds_the_record_to_the_actions_transcript(tmp_path):
    ha = RegistryHA({"switch.plug": {}}, answer({"switch.plug": {"switch.plug": {"status": "sent"}}}))
    result = await run_suite(
        ha, suite(tmp_path, ONE_CALL.format(roles="switch_plug")), profile(switch_plug="switch.plug")
    )
    asked = []

    def calls(integration, action):
        asked.append((integration, action))
        return BLOB

    [path] = await write(result, tmp_path / "records", schema=SCHEMA_OF, calls=calls)
    data = held(path)
    assert asked == [("shelly", "turn_on_for")]
    assert (data["format"], data["calls"], "package" in data) == (3, BLOB, False)
    assert list(data)[:8] == ["format", "integration", "action", "result", "date", "source", "calls", "home_assistant"]


ENTRY = """
action: get_attributes
name: config_entry
rig_seconds: 5
tests:
  - name: halts
    roles: [light_ct]
    steps:
      - wait: 0
"""


@needs_format_3
@pytest.mark.parametrize(
    ("text", "record", "options"),
    [
        (
            ENTRY.replace("get_attributes", "stop").replace("name: config_entry", "options: [predicted_state]"),
            "stop-predicted_state",
            ["predicted_state"],
        ),
        (ENTRY, "config_entry", None),
    ],
    ids=["an option suite", "a non-action suite"],
)
async def test_a_suite_named_for_more_than_its_action_binds_to_its_actions_blob(tmp_path, text, record, options):
    passed = CaseResult(
        "halts[light_ct]", "light_ct", "light", Outcome.PASS, reached=(Reached("S3BL", "2.0", "RGBCCT"),)
    )
    asked = []

    def calls(integration, action):
        asked.append((integration, action))
        return BLOB

    written, _ = await write_records(
        canned(text, [passed], tmp_path),
        REGISTRY,
        under(tmp_path / "records"),
        "2026-11-14",
        "rig run",
        calls=calls,
        schema=SCHEMA_OF,
        commit="abc1234",
    )
    [path] = written
    data = held(path)
    action = "stop" if options else "get_attributes"
    assert (path.name, asked, data["action"], data.get("options"), data["calls"]) == (
        f"{record}.json",
        [("shelly", action)],
        action,
        options,
        BLOB,
    )


@needs_format_3
async def test_a_suite_whose_action_has_no_transcript_is_format_2_and_says_why(tmp_path):
    ha = RegistryHA({"switch.plug": {}}, answer({"switch.plug": {"switch.plug": {"status": "sent"}}}))
    result = await run_suite(
        ha, suite(tmp_path, ONE_CALL.format(roles="switch_plug")), profile(switch_plug="switch.plug")
    )
    printed: list[str] = []
    [path] = await write(result, tmp_path / "records", printed, schema=SCHEMA_OF, calls=lambda i, a: None)
    data = held(path)
    assert (data["format"], data["package"], "calls" in data) == (2, PACKAGE, False)
    [line] = printed
    assert "no call transcript of turn_on_for at abc1234" in line and "signs nothing off" in line


def test_the_schema_says_whether_it_takes_format_3():
    assert SCHEMA_2.binds_calls is False
    assert schema_listing([1, 2, 3]).binds_calls is True


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


async def test_an_older_groups_entry_without_the_map_names_a_members_class_only_when_they_share_one(tmp_path):
    """A response from before deviceside named each member's class: one shared class covers every member."""
    test = await group_test(tmp_path, {"light.z2m_room": {"status": "sent", "capabilities": ["hue"]}})
    assert [device["class"] for device in test["devices"]] == ["hue", "hue"]


async def test_a_group_listing_several_classes_names_none_for_a_member(tmp_path):
    test = await group_test(tmp_path, {"light.z2m_room": {"status": "sent", "capabilities": ["hue", "other"]}})
    assert [device["class"] for device in test["devices"]] == [None, None]


async def test_a_mixed_groups_entry_names_each_members_class(tmp_path):
    entry = {"status": "sent", "capabilities": ["hue", "other"]}
    entry["member_capabilities"] = {"light.z2m_a": "hue", "light.z2m_b": "other"}
    test = await group_test(tmp_path, {"light.z2m_room": entry})
    assert [device["class"] for device in test["devices"]] == ["hue", "other"]


async def test_a_member_its_groups_map_leaves_out_has_no_class(tmp_path):
    """A member deviceside couldn't place, or placed provisionally, is left out of the map; `capabilities` goes too."""
    test = await group_test(
        tmp_path, {"light.z2m_room": {"status": "sent", "member_capabilities": {"light.z2m_a": "hue"}}}
    )
    assert [device["class"] for device in test["devices"]] == ["hue", None]


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


async def test_a_zha_device_names_its_own_basic_cluster_strings_not_a_quirks_friendly_name():
    ha = RegistryHA({})
    found = await reached_devices(ha, ["switch.zha_plug", "switch.zha_unread"], {})
    assert found == (Reached("TS011F", None, None, "_TZ3000_okaz9tjs"), Reached(None, None, None, None))


async def test_a_zha_device_with_no_config_entry_or_diagnostics_names_nothing():
    ha = RegistryHA({})
    ha.devices = [device | {"primary_config_entry": None, "config_entries": []} for device in ha.devices]
    with pytest.raises(LookupError, match="no config entry"):
        await reached_devices(ha, ["light.zha_bulb"], {})


SCENE = """
action: set_scene
rig_seconds: 5
tests:
  - name: applies
    roles: [scene]
    steps:
      - call: {}
      - expect_state: {target: group, state: off}
      - expect_state: {target: light_ct, state: off}
"""


async def test_a_scene_case_lists_its_groups_lights_with_the_class_its_entry_reports(tmp_path):
    """A scene has no device of its own: its case reaches its group's lights, a light a step targets once, and the
    class the scene's entry reports covers each. Its test carries `group`, as a native group's does."""
    registry = Roster({"hue": {"set_scene", "get_attributes", "set_attributes"}}, classes=frozenset({"hue"}))
    responses = iter([{"scene.hue_relax": {"status": "sent", "capability": "hue"}}])

    def respond(ha, action, data):
        return {} if action == "get_attributes" else next(responses)

    ha = RegistryHA({"scene.hue_relax": {}, "light.hue_room": {}, "light.hue_bulb": {}, "light.hue_b": {}}, respond)
    room = RigEntity("light.hue_room", frozenset({"group"}), members=("light.hue_bulb", "light.hue_b"))
    inv = Profile(
        {
            "hue": [
                RigEntity("scene.hue_relax", frozenset({"scene"})),
                room,
                RigEntity("light.hue_bulb", frozenset({"light_ct"})),
            ]
        },
        {},
    )
    steps = suite(tmp_path, SCENE, "hue")
    result = await run([steps], inv, registry, ha, tolerance=TOLERANCE, out=lambda _: None, json_records=True)
    assert outcomes(result) == {"applies[scene]": (Outcome.PASS, "")}
    [case] = result.suites[0].cases
    assert [device.as_json() for device in case.reached] == [
        {"model": "LWA012", "manufacturer": "Signify Netherlands B.V.", "firmware": "1.122.2", "class": "hue"},
        {"model": "LCA016", "manufacturer": "Signify Netherlands B.V.", "firmware": "1.122.2", "class": "hue"},
    ]
    [path] = await write(result, tmp_path / "records", registry=registry)
    [test] = held(path)["tests"]
    assert (test["group"], test["domain"]) == (True, "scene")  # a scene's test lists its lights, as a group's


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


async def test_zigbee2mqtts_fallback_sw_version_is_no_firmware():
    ha = RegistryHA({})
    [fallback] = await reached_devices(ha, ["light.z2m_fallback"], {})
    assert fallback.firmware is None  # "Zigbee2MQTT 2.14.3" is Z2M's own version, not the bulb's
    [other] = await reached_devices(ha, ["light.other_zigbee2mqtt"], {})
    assert other.firmware == "Zigbee2MQTT 2.14.3"  # the guard is for Z2M devices only


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
    """A converted format 1 record that passed the switch case keeps it from a profile without the role: its row,
    bound to no transcript, can't be carried into a format 3 record."""
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
        calls=lambda integration, action: BLOB,
    )
    assert (written, kept) == ([], [(old, ["lit[switch_plug]"], "the record isn't JSON format 3")])


GROUP_AND_BULB = """
action: set_attributes
rig_seconds: 5
tests:
  - name: sets
    roles: [light_ct, group]
    steps:
      - wait: 0
"""
HUE = {"model": "LTA009", "manufacturer": "Signify Netherlands B.V.", "firmware": "1.163.1", "class": "hue"}
GROUP_ROW = {
    "id": "sets[group]",
    "domain": "light",
    "role": "group",
    "result": "pass",
    "group": True,
    "devices": [HUE, {**HUE, "model": "LWA012"}],
}


def format_3(path: Path, tests: list[dict], calls: str = BLOB) -> dict:
    """A format 3 record of the zigbee2mqtt set_attributes suite at `path`, from an earlier run."""
    document = {
        "format": 3,
        "integration": "zigbee2mqtt",
        "action": "set_attributes",
        "result": "pass",
        "date": "2026-11-18",
        "source": {"kind": "rig", "commit": "c05e616"},
        "calls": calls,
        "home_assistant": "2026.10.4",
        "tests": tests,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n")
    return document


def single_bulb(tmp_path) -> RunResult:
    """A run of GROUP_AND_BULB on a profile with one bulb of another maker and no group."""
    bulb = CaseResult(
        "sets[light_ct]",
        "light_ct",
        "light",
        Outcome.PASS,
        reached=(Reached("lumi.light.agl003", "1.0", None, "Aqara"),),
    )
    group = CaseResult("sets[group]", "group", None, Outcome.SKIPPED, "no group entity in the profile")
    group.inapplicable = group.absent = True
    return canned(GROUP_AND_BULB, [bulb, group], tmp_path, "zigbee2mqtt")


@needs_format_3
async def test_a_run_without_a_role_carries_the_rows_the_record_passed_on_it(tmp_path):
    """A single-bulb profile's run on another maker's bulb adds its row beside the Hue bulb's the record passed on
    another profile, and keeps the group's row it skips, each as it was: the record holds all three, citing this run."""
    old = tmp_path / "records" / "zigbee2mqtt" / "set_attributes.json"
    hue_row = {"id": "sets[light_ct]", "domain": "light", "role": "light_ct", "result": "pass", "devices": [HUE]}
    format_3(old, [hue_row, GROUP_ROW])
    printed: list[str] = []
    written, kept = await write_records(
        single_bulb(tmp_path),
        REGISTRY,
        under(tmp_path / "records"),
        "2026-11-25",
        "rig run",
        schema=SCHEMA_OF,
        commit="abc1234",
        calls=lambda integration, action: BLOB,
        out=printed.append,
    )
    assert (written, kept) == ([old], [])
    data = held(old)
    assert (data["date"], data["source"]["commit"], data["calls"]) == ("2026-11-25", "abc1234", BLOB)
    assert [test["id"] for test in data["tests"]] == ["sets[light_ct]", "sets[light_ct]", "sets[group]"]
    assert data["tests"][0] == hue_row
    assert data["tests"][1]["devices"][0]["model"] == "lumi.light.agl003"
    assert data["tests"][2] == GROUP_ROW
    assert printed == [
        "zigbee2mqtt / set_attributes: kept the record's rows of sets[group], sets[light_ct] from earlier runs: "
        "cases this profile skips as not applying or doesn't plan, or ran on other devices"
    ]


@needs_format_3
async def test_a_rerun_on_the_same_device_replaces_its_row_in_place(tmp_path):
    """A row is its case and its devices: the Aqara bulb's rerun replaces its own row, where it was, and the Hue
    bulb's stays."""
    old = tmp_path / "records" / "zigbee2mqtt" / "set_attributes.json"
    hue_row = {"id": "sets[light_ct]", "domain": "light", "role": "light_ct", "result": "pass", "devices": [HUE]}
    aqara = {"model": "lumi.light.agl003", "manufacturer": "Aqara", "firmware": "0.9", "class": None}
    aqara_row = {"id": "sets[light_ct]", "domain": "light", "role": "light_ct", "result": "pass", "devices": [aqara]}
    format_3(old, [aqara_row, hue_row, GROUP_ROW])
    written, _ = await write_records(
        single_bulb(tmp_path),
        REGISTRY,
        under(tmp_path / "records"),
        "2026-11-25",
        "rig run",
        schema=SCHEMA_OF,
        commit="abc1234",
        calls=lambda integration, action: BLOB,
        out=lambda _: None,
    )
    assert written == [old]
    tests = held(old)["tests"]
    assert [test["devices"][0]["firmware"] for test in tests] == ["1.0", "1.163.1", "1.163.1"]
    assert tests[1:] == [hue_row, GROUP_ROW]


@needs_format_3
async def test_a_row_on_another_device_keeps_the_record_whole_on_another_transcript(tmp_path):
    """Another maker's bulb never takes the Hue bulb's row: where the record cites another transcript, even one whose
    every segment this run's still holds, no row is carried onto it, and the record stays as it was."""
    old = tmp_path / "records" / "zigbee2mqtt" / "set_attributes.json"
    hue_row = {"id": "sets[light_ct]", "domain": "light", "role": "light_ct", "result": "pass", "devices": [HUE]}
    document = format_3(old, [hue_row], calls="f" * 40)
    run = single_bulb(tmp_path)
    run.suites[0].cases.pop()  # no group case planned: nothing narrows, the Hue row is beside the new one alone
    written, kept = await write_records(
        run,
        REGISTRY,
        under(tmp_path / "records"),
        "2026-11-25",
        "rig run",
        schema=SCHEMA_OF,
        commit="abc1234",
        calls=lambda integration, action: BLOB,
        out=lambda _: None,
    )
    why = "the record's call transcript (ffffffff) isn't this run's (01234567)"
    assert (written, kept) == ([], [(old, ["sets[light_ct]"], why)])
    assert json.loads(old.read_text()) == document


PLAIN = """  - name: plain
    roles: [light_ct]
    steps:
      - wait: 0
"""


@needs_format_3
async def test_a_pass_from_before_a_test_became_a_fact_case_is_not_carried(tmp_path):
    old = tmp_path / "records" / "zigbee2mqtt" / "set_attributes.json"
    hue_row = {"id": "sets[light_ct]", "domain": "light", "role": "light_ct", "result": "pass", "devices": [HUE]}
    format_3(old, [hue_row, GROUP_ROW])
    fact = CaseResult(
        "sets[light_ct]", "light_ct", "light", Outcome.PASS, reached=(Reached("LTA009", "1.163.1", "hue"),)
    )
    text = GROUP_AND_BULB.replace("    steps:", "    firmware_fact: true\n    steps:") + PLAIN
    plain = CaseResult("plain[light_ct]", "light_ct", "light", Outcome.PASS, reached=fact.reached)
    written, _ = await write_records(
        canned(text, [fact, *single_bulb(tmp_path).suites[0].cases[1:], plain], tmp_path, "zigbee2mqtt"),
        REGISTRY,
        under(tmp_path / "records"),
        "2026-11-25",
        "rig run",
        schema=SCHEMA_OF,
        commit="abc1234",
        calls=lambda integration, action: BLOB,
        out=lambda _: None,
    )
    assert written == [old]
    assert held(old)["tests"][:2] == [  # the old passes, from before the test became a firmware fact, credit nothing
        {"id": "sets[light_ct]", "role": "light_ct", "result": "skipped", "fact": True},
        {"id": "sets[group]", "role": "group", "result": "skipped", "fact": True},
    ]


@needs_format_3
async def test_a_plug_beyond_the_profiles_keeps_its_row_after_its_siblings(tmp_path):
    old = tmp_path / "records" / "shelly" / "turn_on_for.json"
    plug = {"model": "S3SW-001X8EU", "manufacturer": "Shelly", "firmware": "1.7.4", "class": "shelly"}
    row = {
        "id": "called[switch_plug-3]",
        "domain": "switch",
        "role": "switch_plug",
        "result": "pass",
        "devices": [plug],
    }
    format_3(old, [row])
    ran = [
        CaseResult(
            case_id,
            "switch_plug",
            "switch",
            Outcome.PASS,
            reached=(Reached("S3SW-001X8EU", "1.7.4", "shelly", "Shelly"),),
        )
        for case_id in ("called[switch_plug]", "called[switch_plug-2]")
    ]
    tail = CaseResult(
        "called[light_ct]", "light_ct", "light", Outcome.PASS, reached=(Reached("S3BL", "2.0", "shelly", "Shelly"),)
    )
    text = ONE_CALL.format(roles="switch_plug, light_ct")
    written, _ = await write_records(
        canned(text, [*ran, tail], tmp_path),
        REGISTRY,
        under(tmp_path / "records"),
        "2026-11-25",
        "rig run",
        schema=SCHEMA_OF,
        commit="abc1234",
        calls=lambda integration, action: BLOB,
        out=lambda _: None,
    )
    assert written == [old]
    assert [test["id"] for test in held(old)["tests"]] == [
        "called[switch_plug]",
        "called[switch_plug-2]",
        "called[switch_plug-3]",
        "called[light_ct]",
    ]


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
        "converted": True,  # deviceside's conversion marks the rows it made from _facts.md
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


WATCHED = """
action: turn_on_for
rig_seconds: 5
tests:
  - name: clicks
    roles: [switch_plug]
    setup:
      - set: {state: off}
    steps:
      - call: {duration: 0.2}
    observe: {see: It clicks., watch: A click., ask: 'Did it click?'}
"""


def test_a_watched_case_a_person_answered_is_an_observed_fact_row(tmp_path):
    path = tmp_path / "steps.yaml"
    path.write_text(WATCHED)
    steps = engine.Suite(load_step_file(path), "shelly", facts=True)
    seen = CaseResult(
        "clicks[switch_plug] observed",
        "switch_plug",
        "switch",
        Outcome.PASS,
        reached=(Reached("S3SW-001X8EU", "1.7.4", "Switch", "Shelly"),),
    )
    facts = write_fact(steps, seen, REGISTRY, under(tmp_path / "records"), "2026-11-14", SCHEMA_OF)
    assert held(facts)["facts"] == [
        {
            "date": "2026-11-14",
            "suite": "turn_on_for",
            "test": "clicks[switch_plug] observed",
            "kind": "observed",
            "result": "pass",
            "devices": [{"model": "S3SW-001X8EU", "manufacturer": "Shelly", "firmware": "1.7.4", "class": "Switch"}],
        }
    ]
    nobody = CaseResult(seen.id, "switch_plug", "switch", Outcome.NOT_OBSERVED, reached=seen.reached)
    assert write_fact(steps, nobody, REGISTRY, under(tmp_path / "records"), "2026-11-15", SCHEMA_OF) is None
    assert len(held(facts)["facts"]) == 1  # nobody there is no fact


def test_a_facts_file_that_cant_be_written_is_said_not_raised(tmp_path):
    steps, case = a_fact(tmp_path)
    (tmp_path / "records").write_text("a file where the folder should be")
    printed: list[str] = []
    assert write_fact(steps, case, REGISTRY, under(tmp_path / "records"), "d", SCHEMA_OF, printed.append) is None
    assert printed[0].startswith("Not written to _facts.json (")


async def test_a_record_that_cant_be_written_is_said_and_the_rest_written(tmp_path):
    halts = CaseResult(
        "halts[light_ct]", "light_ct", "light", Outcome.PASS, reached=(Reached("LTA009", "1.163.1", "hue"),)
    )
    blocked = tmp_path / "records" / "zigbee2mqtt"
    blocked.parent.mkdir()
    blocked.write_text("a file where the folder should be")
    printed: list[str] = []
    assert await write(canned(OPTIONS, [halts], tmp_path, "zigbee2mqtt"), tmp_path / "records", printed) == []
    assert printed[0].startswith("zigbee2mqtt / stop-predicted_state: ")


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
    [case] = planned_cases(steps, profile(switch_plug="switch.plug"))
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
