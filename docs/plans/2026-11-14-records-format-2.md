# Test records in JSON format 2

DeviceSide's device-classes step 5b (`claude/device-classes-5b-records`) moves every test record to JSON, with one
schema for all of them, `docs/test-records.schema.json`, the contract's one home. It converts the markdown records to
`"format": 1` once, and its `gen_compat` and `check_release` read JSON only; `check_release --signoffs` accepts only
format 2, which lists on each test the devices it reached, each with its model ID, firmware and class. The rig writes
format 2.

## Coordination

Intent: against a checkout that has the schema, the rig writes each record and `_facts.json` row as JSON format 2,
validated against that schema before it is written; against one without (DeviceSide `main` today), markdown as before.
Touches: ha_rig/engine.py, ha_rig/facts.py, ha_rig/client.py, ha_rig/progress.py, ha_rig/target.py, scripts/rig.py,
docs/rig.md, pyproject.toml, uv.lock, tests/
Tests-with: DeviceSide/DeviceSide#430

This PR merges first: DeviceSide#430's records need a rig that writes format 2.

## Parts

1. **Choosing the format** (`target.py`): the checkout has `docs/test-records.schema.json` → JSON format 2 and no
   markdown; else markdown exactly as today. The runner decides once, before anything moves, and builds the validator
   then: a checkout with the schema but no `jsonschema` stops the run in its preflight, naming the fix (`uv sync` in
   the project the rig runs in, the checkout's when run from there, whose dev group has it), `--check` included,
   rather than finding out after an hour of cases that no record can be written. `jsonschema` is imported only then,
   so a run against `main` needs none. `--promote` copies files and validates nothing, but refuses markdown records
   staged for a checkout that has the schema now.
2. **Files**: `docs/integrations/<x>/test-records/<suite.name>.json`, and `_facts.json` beside it, indent 2 and a
   trailing newline. Staging, promotion and the never-narrow rule work on whichever file the format names.
3. **What each test reached** (`engine.py`): every case that ran (pass, fail, not observed) now notes, as it ends, the
   devices behind its target: a native group's members (`group: true`), else the target's own device and any light a
   step targets (always one of the case's group in today's step files); a scene has no device of its own, so a scene
   case lists its group's members and lights, and carries `group: true` as a native group's does (decided in the pair review). The ledger saves them, so a resumed suite has them; a saved case without
   them (an older rig's, or a lookup that failed) reruns. A run against a markdown checkout reads no case's devices,
and one against a JSON checkout no markdown fact's.
   - `model`: the Zigbee model ID on Zigbee devices: Z2M's `bridge/devices` `model_id` (HA's registry holds Z2M's
     product code there); ZHA's device diagnostics' `model` (admin only, as a ZHA suite runs): HA's registry takes
     zha's `model`, a quirks v2 device's friendly name where one is set, and zha's `unk_model` reads as null. Hue: HA's registry `model_id`, which HA's Hue integration takes from
     the bridge's `product_data.model_id`, so the rig needn't ask the bridge. Elsewhere the registry `model_id`.
     A device is told by its registry identifiers (a Z2M or ZHA IEEE), not the suite's integration.
   - `firmware`: `sw_version`, null when empty or absent.
   - `manufacturer` (a spec change after the PR opened): the maker string as the integration reports it, as a Tuya
     plug's `TS011F` is hundreds of makers': Z2M's `bridge/devices` `manufacturer` (`_TZ3000_…`, not the definition's
     vendor, which HA's registry holds), ZHA's device diagnostics' `manufacturer` (zigpy's own, beside zha's
     `friendly_manufacturer`; `unk_manufacturer` reads as null), else HA's registry `manufacturer` (on Hue the
     bridge's `product_data.manufacturer_name`, which HA copies). Null where it reports none; never a device name.
   - `class`: from each call of the tested action on the case's own target outside setup (as the Capability column
     reads them, #85): the target's `capability`; a member's own entry where the response answered member by member;
     a native group's `member_capabilities`, each member's class by entity ID (DeviceSide#431; decided in the pair
     review), and in a response from before that map its `capabilities` when it lists one class, which every member
     then reached. Null when the integration names no classes, a response leaves the class out (a device not placed
     yet, or a member the map leaves out), the calls disagree, or an older group's entry lists several classes.
4. **The record**: `{format: 2, integration, action, options?, result: pass, date, source: {kind: rig, commit},
   package, home_assistant, tests}`; a test that ran `{id, domain, role, result, group?, devices}`, a skipped one
   `{id, role, result: skipped, reason}` (the skip detail), a fact case `{id, role, result: skipped, fact: true}`. No
   counts, `Ran` row, notes or fact bullets.
5. **`_facts.json`**: `{format: 2, integration, facts: [...]}`; each fact case appends `{date, suite, test, kind,
   result, devices}`, devices as a record's, leaving every existing row as it is (a converted row's `name`, no
   `class`) and rewriting the file. A file the rig can't read as JSON is left alone and the row is printed instead.
6. **Reading** (`Record.read`, `FactsFile.read`): JSON `tests[].id` and `result`, a fact case read as skipped, and
   `source.commit` and `date`; markdown as before, for a checkout without the schema.
7. **Validation**: each file is checked against the target's schema before it is written; a record or facts file
   that fails is not written, and the run says why. `jsonschema>=4.26.0` joins the dev group, in step with
   deviceside's `pyproject.toml`.
8. **Docs**: `docs/rig.md`, Test records and Facts, say which format a checkout gets and what a format 2 file holds,
   pointing to deviceside's schema for the shape.

## Tests

Canned rig output for each case kind (ran, native group, skipped, fact), for a Z2M, a ZHA and a Hue model ID, and a
null class; each written file round-tripped through the schema from the paired DeviceSide branch (the checkout the
tests import). Reading JSON records and facts; the ledger saving and resuming the devices; the runner choosing the
format and refusing a schema without `jsonschema`; a record that fails validation not written.

## Verification

`.venv/bin/pytest`, `pre-commit run --all-files`, `/audit-code` on the diff. No live rig run: Jason runs those.
