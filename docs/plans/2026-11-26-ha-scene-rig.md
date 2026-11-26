# Rig support for set_scene on Home Assistant scenes (v0.3.0)

## Context

DeviceSide's `wt/set-scene-ha-scenes` (plan `docs/plans/2026-11-26-set-scene-ha-scenes.md` there) adds a core route:
`set_scene` on a `scene.homeassistant` entity writes each light through `set_attributes` and turns off, through
core's `light.turn_off`, the lights the scene holds as off. It has no adapter of its own: `SetScene` rides
`set_attributes` (an `Action` class attribute, deviceside step 4), so every integration with `set_attributes` serves it.
Its Decision 5 leaves the rig case to this plan. The rig today knows only the Hue bridge scene (the `scene` role, the
roam group's rig-made Hue scene, `ha_rig/profile.py` `RoamGroup.scene`), and it runs a suite only where the action
has an adapter (`engine.applies`, `adapters_for`), so a `set_scene` suite never runs off Hue. Outcome: a
`set_scene` run on each integration with `set_attributes` tests a rig-made HA scene and writes that integration's
`set_scene.json`. That run decides the v0.3.0 release. No merge waits on it.

## Decisions (Jason, 2026-11-26)

1. **Role and making:** a new role `ha_scene`, made per case with `scene.create` by a setup step, from the case's own
   lights. It is never a profile entry.
2. **Record:** `docs/integrations/<x>/test-records/set_scene.json` on each integration it ran on, Hue's together with
   its bridge-scene cases. `check_release.py --records` validates it but doesn't demand one where `set_scene` only
   rides `set_attributes` (that gate stays on `set_attributes`). gen_compat's derived set-scene cell links it
   when it exists.
3. **Per-integration scenes only.** No scene spans two integrations. Splitting a scene across integrations is
   unit-tested in deviceside step 3. A spanning scene would need two locks, run outside the lanes, and have a record
   that belongs to neither integration.

## Design

**The role.** `ha_scene` joins `ROLES` (`ha_rig/profile.py`), but no profile lists it: `fixed_entity` refuses it
("made by the ha_scene step"). Its entity is fixed per integration, `scene.ds_rig_ha_<integration>` (`scene_id`
`ds_rig_ha_<integration>`), so lanes never share one. A case needing the role gets that ID from
`case_entities` (no profile lookup). It is never in `made_entities` and never "hidden" (`hidden_devices`): it is made
of the case's own fixed lights, which the rig knows, so the case stays in its integration's lane (`in_head`). If its
lights are roaming bulbs, `in_head` already takes the case out of the lane.

**The step.** A new step kind, `ha_scene`, valid in `setup` only:
```yaml
- ha_scene: {light_ct: {state: on, brightness_pct: 40, color_temp_kelvin: 2732}, light_dimmable: {state: off}}
```
Keys are roles, which the case needs (`roles_needed`). Values are light states in the rig's units. The engine turns
them into one `scene.create` (`entities:` with `brightness` 0-255, `color_temp_kelvin` and `color_mode: color_temp`,
so the route's color-mode pick is exercised as a real scene stores it). It then waits for the scene entity to appear
in HA. If two roles resolve to the same entity, the case skips as not applying (`ha_scene: light_ct and light_dimmable
are one light`), decided in `_planned`. Before making the scene the engine journals it under the case's journal key.
The case's end (pass or fail, after `restore`) calls `scene.delete`, and recovery (`scripts/rig.py` `recover_case`)
deletes one a crashed run left. An HA restart drops it anyway. The lights are snapshotted as usual, since they are
case entities.

**Where the suite runs.** `applies`, `adapters_for` and `unbuilt` read a ridden action. When the integration has no
adapter for the action and the deviceside action class names one it rides (`getattr(action_cls, "rides", None)`,
read through the registry the way `check_options` reads it), the ridden action's adapter stands in. The suite then
applies, and `unbuilt` judges the declared class against `set_attributes`' scope, as the dispatcher gates each light.
`check_options` is unchanged: `set_scene` files set no options. The existing `[scene]` cases skip off Hue as before
(`no scene entity in the profile`, inapplicable), so they never narrow a record. `tests/rig_fakes.py` `Roster` gains
a way to declare a riding action.

**What a record holds.** `reached_entities` skips `ha_scene` as it skips `scene`, and the lights reached are the
case's light roles. `reached` takes the lights as `behind` the scene, and `_test_json` sets `group: true` for
`ha_scene`, as for `scene`. Each light's class comes from the scene's response entry: deviceside step 3 must
fold per-light classes into it the way a native group does (`member_capabilities`, read by `_behind_class`) and
name the lights' integration when they share one. **This is a coordination item for deviceside step 3** (below). The
record name is `set_scene.json` (`record_name`, unchanged).

## Cases (in deviceside's `tests/rig/steps/set_scene.yaml`, paired PR)

Every case runs on `ha_scene` with the setup step above (light_ct on at 40 %/2732 K, light_dimmable off). Setups move
the lights far from the scene's values, at 100 % and 6500 K.

| Case | Steps | What it catches that no unit test or existing case does |
|---|---|---|
| `sets_while_off` | both lights off; call; expect `sent`, both still off after 1 s; turn light_ct on; expect 40 % ±, 2732 K ± | The **live `scene_config` read**: unit tests fake `hass.data["homeassistant_scene"]`, and only a real `scene.create` entity proves core keeps it there with `scene_config.states` in that shape. Also that the route's indirect resolve (`via_group` = the scene, `named=False`) writes stored attributes on a real off light of each integration. The `set_attributes` cases name the light directly, and Hue's `[scene]` goes through the bridge adapter. |
| `applies_while_on` | light_ct on at 100 %/6500 K; call; expect 40 % ±, 2732 K ± within 5 s | That the 0-255 to percent conversion and the `color_temp_kelvin` pick by `color_mode` land on a device as core stored them, and that an on light changes straight away on each integration. The unit tests assert the request, not the light. |
| `turns_off_held_off` | light_dimmable on; call; expect light_dimmable off within 5 s, light_ct still off and holding the scene's values when turned on | The **`light.turn_off` path**: no `set_attributes` case turns a light off, and Hue's bridge scene sends `on: false` to the bridge, not to core. This is the only check that the blocking turn-off with the call's context reaches each integration's light, and that turning off one light doesn't stop the write to the other. |

An HA scene edit applying (re-read per call) is unit-tested (`tests/core/test_ha_scene.py`), so it is not a case.

## Steps

0. Save this plan as `docs/plans/2026-11-26-ha-scene-rig.md` on `wt/ha-scene-rig` (worktree
   `~/projects/ha/ha-rig-wt/ha-scene-rig`), commit, push, `pr-draft` it.
1. **Role and step kind** (`ha_rig/profile.py`, `ha_rig/engine.py` step parsing and `Run`): `ha_scene` in `ROLES`,
   refused by `fixed_entity`; `case_entities` and `_planned` (fixed ID, one-light skip); the `ha_scene` step
   (`scene.create`, journal, wait for the entity, `scene.delete` at the case's end and in `recover_case`). Tests in
   `tests/test_rig_engine.py` against the fake HA: made, deleted on pass and on fail, recovered from a journal,
   one-light skip, refused in a profile.
2. **Riding actions** (`applies`, `adapters_for`, `unbuilt`, `tests/rig_fakes.py` `Roster`): a suite whose action
   rides another runs where the ridden adapter is, and its declared-class skips follow the ridden scope. Tests: a
   riding suite applies on an integration with only `set_attributes`, and not on one without; `[scene]` skips there
   without narrowing; `unbuilt` on a class `set_attributes` refuses.
3. **Records** (`reached_entities`, `reached`, `_test_json`): the `ha_scene` record test lists the scene's lights
   with their classes from `member_capabilities`, `group: true`. Test with a fake response in that shape.
4. **Docs:** `docs/rig.md` (the `ha_scene` step in the step table, the role in Profiles, the lanes note, recovery),
   `ha_rig/profile.example.yaml`'s role list ("ha_scene: made by the step, never listed"). README's `--help` block
   only if options change (none expected).
5. **Paired deviceside PR** (branch `wt/set-scene-ha-scene-rig-steps` off `wt/set-scene-ha-scenes`, `pr-draft`): the
   three cases above in `tests/rig/steps/set_scene.yaml`, the header comment updated, `rig_seconds` raised; the
   `check_release.py --records` test that a `set_scene.json` on a riding integration is validated and not demanded
   (if deviceside step 4 hasn't pinned it already); gen_compat linking the record in the derived cell (Decision 2).
   Name it here as `Tests-with` and rerun `pr-draft`. **Merge order:** this rig PR first (`merge-green --squash
   --delete-branch`), as the rig must know the step kind before deviceside's step file uses it. The step-file PR
   follows `wt/set-scene-ha-scenes`, after v0.2.0 is tagged.
6. **Audit** `/audit-code` on the rig PR's diff, fix, mark ready, merge.

## Reuse

`case_entities`, `_planned`, `hidden_devices`, `in_head`, `reached_entities`, `_behind_class`, `_test_json`
(`ha_rig/engine.py`); `Snapshot` and the journal (`ha_rig/journal.py`) for the made scene; `recover_case`
(`scripts/rig.py`); `rig_name`'s `ds-rig` naming idea (`ha_rig/profile.py`); `Roster` (`tests/rig_fakes.py`);
roam.py's `SCENE_BRIGHTNESS`/`SCENE_MIREK` values for the scene, so both scene kinds use the same target.

## Verification

- `.venv/bin/pytest` green, with `scripts/ci_deviceside.sh` pairing the step-file PR (`Tests-with`), so
  `tests/test_rig_steps.py` parses the new cases against the new step kind.
- `scripts/rig.py --plan --action set_scene` (no lock, no move) from the deviceside step-file worktree lists
  `[ha_scene]` cases on hue, zha, zigbee2mqtt (and every other profile integration with `set_attributes`), with
  `[scene]` skipped off Hue.
- The live run is Jason's, on the Mac, behind the authorization gate:
  `../ha-rig/scripts/rig.py --action set_scene` from the step-file worktree. It writes each integration's
  `set_scene.json` and decides v0.3.0. No merge waits on it.

## Coordination

Intent: the rig tests set_scene on Home Assistant scenes: an `ha_scene` role made per case with scene.create, suites
for actions that ride another, and per-integration set_scene records.
Touches: ha_rig/profile.py, ha_rig/engine.py, ha_rig/profile.example.yaml, scripts/rig.py, tests/test_rig_engine.py, tests/rig_fakes.py, docs/rig.md, docs/plans/2026-11-26-ha-scene-rig.md
Depends-on: none to merge. Needs from deviceside `wt/set-scene-ha-scenes` step 3: an HA scene's response entry
carries `member_capabilities` and the lights' `integration`. Step 4: `SetScene` names `rides = "set_attributes"`.
Message that worktree's owner (`agent-find --branch wt/set-scene-ha-scenes`) at step 0.
Tests-with: DeviceSide/DeviceSide#<step-file PR, step 5>

## Implementing session

Model: `opus`. Records are public and a change to what one proves needs a matching deviceside change. The
response-shape coordination with an in-flight deviceside branch is also a judgment call that CI won't catch.

```
cd ~/projects/ha/ha-rig && claude --model opus 'Implement docs/plans/2026-11-26-ha-scene-rig.md, starting at step 0.'
```
