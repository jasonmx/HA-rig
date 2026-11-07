# get_attributes: set the lights up with set_attributes, not turn_on (#281)

## Context

#281: on Z2M, `get_attributes` `reads_while_off[group]` read `color_temp_kelvin: 3354` after a 2700 K setup.
The setup step is the rig's `set:` (`../ha-rig/ha_rig/engine.py` `_set`), which is HA core's `light.turn_on` on the
Z2M group, and it waits only for `state: on`. In that run the color bulb logged Z2M `Delivery failed`, so one member
kept its old color temperature, and the read correctly averaged it in (`core/zcl_row.average`, as Hue's room row
does). So the wrong CT came from core's write, which nothing checked.

Fix: the setup writes through DeviceSide's own `set_attributes`. It writes while the light is off, then reads back
every member (a group included) and raises `not_stored` when one didn't take the write. The case then starts from
attributes that have been confirmed set. If a member misses the write, the run fails at the setup step with the true
cause, not later as a wrong read.

## Changes (all in `../ha-rig`, the private rig repo)

`ha_rig/steps/get_attributes.yaml`, `reads_while_off`:

- Add `only: [hue, zha, zigbee2mqtt]`, the integrations with a `set_attributes` adapter
  (`custom_components/deviceside/integrations/*/set_attributes.py`).
- Replace the steps with:
  ```yaml
  - set: {state: off}
  - call: {action: set_attributes, brightness: 40, color_temp_kelvin: 2700}
  - expect_response: {status: expected, integration: self}
  - call: {}
  - expect_response:
      attributes: {brightness: {min: 39, max: 41}, color_temp_kelvin: {min: 2650, max: 2750}}
  ```
  The two `wait: 2` steps and their comments go: the light is already off when written, so there is no fade, and
  `set_attributes` waits out its own write fade before its read-back. Check that `expect_response` after a `call`
  with another `action:` asserts on that call's response, the way `predicts_mid_move` uses `call: {action: …}`. If
  it doesn't, a rejected setup still raises in `_call` (`StepFailure`), so drop that `expect_response` line.
- Update the file's header comment ("On the group role the set gives every member the same values…") to say
  `set_attributes` writes and confirms every member.

Shelly runs `reads_while_off[light_ct]` but has no `set_attributes` adapter. Rig test names must be unique
(`engine.py:546`), so add a sibling test `reads_while_off_after_turn_on`, `roles: [light_ct]`, `only: [shelly]`,
holding today's `set:`-based steps unchanged, with a comment that it folds back into `reads_while_off` once Shelly
has `set_attributes` (issue below). The Shelly record changes the test's name on its next run.

## Shelly set_attributes, later

Shelly has no `set_attributes` adapter only because none was built. File a DeviceSide issue, "Shelly
set_attributes", and add the comment the global rule requires:

- Fix shape: first probe whether `RGBCCT.Set` with brightness/ct/rgb and no `on` keeps the rig's Multicolor Bulb
  Gen3 off and stores the values. Then add an experimental adapter `integrations/shelly/set_attributes.py`, reusing
  `light_params` (`rpc.py:32`) for the Set params and `get_status` / `status_row` for a read-back that confirms or
  rejects `not_stored`. Then fold the rig's `reads_while_off_after_turn_on` back into `reads_while_off`.
- Model: opus, since it's a new adapter whose call behavior rests on a probe.

Rig unit tests: if `../ha-rig/tests` checks the step files (parse, or a case count per integration), update them.
Run the rig's test suite.

## Coordination

Intent: get_attributes' reads_while_off sets its lights up with deviceside.set_attributes (confirmed per member)
in place of core light.turn_on, so a dropped delivery fails the setup, not the read (#281).
Touches: ha_rig/steps/get_attributes.yaml, tests/ (only if a step-file test needs it)

## Verification

1. The rig's unit tests pass.
2. Rig run of `get_attributes` on the lab profile for zigbee2mqtt, zha and hue (the rig's `CLAUDE.local.md` gives the
   run and lock rules). `reads_while_off[light_ct]` and `[group]` pass on each. Commit the refreshed records in a
   deviceside PR.
3. If Z2M `reads_while_off[group]` fails, it should now fail at the `set_attributes` step with `not_stored`, naming
   the group. That would be a real DeviceSide write defect, followed up in DeviceSide's Z2M `set_attributes`.
4. The Shelly issue is filed and has its comment, and its number is in the rig test's comment.
5. Comment on #281 with the cause (core's setup write, not checked, plus a dropped delivery) and the rig PR, then
   close it once step 2 passes.
