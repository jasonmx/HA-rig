# Fix #104: Zigbee2MQTT's own version recorded as a device's firmware

## Context

Zigbee2MQTT records in DeviceSide (`docs/integrations/zigbee2mqtt/test-records/turn_on_for.json`, `get_attributes.json`)
say `"firmware": "Zigbee2MQTT 2.14.3"` on the Tuya TS011F plugs, and the compatibility page (DeviceSide/DeviceSide#564)
shows that as their firmware. The rig copies HA's device-registry `sw_version` as written
(`ha_rig/engine.py:3061`, `Reached` in `reached_devices`; the same read feeds the Markdown Device row in `rig_facts`,
`engine.py:2965`).

Checked against Zigbee2MQTT's discovery code (`lib/extension/homeassistant.ts`, `getDevicePayload`): the payload starts
with `sw_version: \`Zigbee2MQTT ${this.zigbee2MQTTVersion}\`` and replaces it with `entity.zh.softwareBuildID` only
when that is not null. Groups keep the fallback; the bridge device gets the bare version (no case reaches it).
So the fallback is exactly `Zigbee2MQTT <package version>` (the version may carry a `-dev` suffix), on any device or
group under the `mqtt` domain with a `zigbee2mqtt_…` identifier.

Outcome: `firmware` is `null` ("it reports none") when the value is Zigbee2MQTT's fallback. The DeviceSide records are
not hand-edited; the next rig run rewrites them. Nothing changes what a record proves, so no deviceside change pairs with it.

## Coordination

Intent: Read Zigbee2MQTT's discovery fallback `sw_version` ("Zigbee2MQTT <version>") as no firmware, in records and the
Device row.
Touches: ha_rig/engine.py, tests/test_rig_records_json.py, tests/test_rig_engine.py, docs/rig.md

## Change

Worktree `wt/z2m-firmware` off `main`; `pr-draft` first. Plan file committed on that branch (repo CLAUDE.md).

1. **`ha_rig/engine.py`**: add one helper beside `record_model`/`record_manufacturer`:
   ```python
   Z2M_FALLBACK_FIRMWARE = re.compile(r"Zigbee2MQTT \d")  # Z2M's discovery sw_version until it reads a build ID

   def record_firmware(device: dict) -> str | None:
       """A device-registry entry's firmware: its `sw_version`, None where it reports none: empty, or on a
       Zigbee2MQTT device or group, Z2M's own `Zigbee2MQTT <version>`, which its discovery sends until it reads the
       device's build ID."""
   ```
   The Z2M test is "an `mqtt` identifier starting `zigbee2mqtt_`" (covers groups, which `device_ieee` skips), with
   `Z2M_FALLBACK_FIRMWARE.match(value)`. Use it at both reads: `reached_devices` (`Reached(model, record_firmware(device), …)`)
   and `rig_facts` (`Device(…, record_firmware(device))`), so the JSON record and the Markdown Device row agree.
2. **`tests/test_rig_records_json.py`**: add a device beside `z2m_b` (`"sw_version": ""  # Z2M has read none`) with
   `"sw_version": "Zigbee2MQTT 2.14.3"` (and its bridge/devices entry and entity in the fake), and a test, following
   the `reached_devices(ha, ["light.z2m_a", "light.z2m_b"], {})` one (~line 592), asserting its `firmware` is `None`.
   Also assert a non-Z2M device whose `sw_version` happens to start "Zigbee2MQTT" keeps it (the guard is Z2M-only).
3. **`tests/test_rig_engine.py`**: one `rig_facts` case (next to line 1372) where a Z2M device's fallback reads as
   firmware `unknown` in `str(Device)`.
4. **`docs/rig.md`** (~line 472, the record paragraph): one clause that firmware is null where the integration
   reports none, Zigbee2MQTT's own `Zigbee2MQTT <version>` included.

## Verification

- `.venv/bin/pytest` (new tests fail before the change, pass after); `pre-commit run --all-files`.
- No rig run: the fakes cover it, and the next real Z2M run rewrites deviceside's records.
- Before ready: the audit gate (`audit-gate brief pr` → agent → `audit-gate record`), then
  `merge-green <n> --squash --delete-branch`, backgrounded.

## Implementing session

Model `sonnet`: one helper, two call sites and tests; the record tests catch a wrong call.
