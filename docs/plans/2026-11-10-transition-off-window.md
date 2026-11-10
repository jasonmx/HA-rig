# turn_on_for bulb_transition_then_off: off window ends 2 s early

Diagnosed in deviceside's v0.1.0 choreography (Phase 0 step 3) with a `--debug-logs` lab run on 2026-11-10: on
Z2M the bulb turns off on time, but the case's off window closes 7 s after its on check, which passes at once, so
it ends just before DeviceSide's state read at on_time + 1 s. Every sibling case's window ends duration + tol after
its on check (9 s on Z2M).

## Coordination

Intent: widen bulb_transition_then_off's off window by the 2 s its wait subtracts, so it ends duration + tol after the
on check like every other turn_on_for case.
Touches: ha_rig/steps/turn_on_for.yaml
