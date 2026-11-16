# A step selector on the target's DeviceSide class

## Coordination

Intent: a step can run only on, or skip, targets of some DeviceSide classes in one integration, so a shared step file
can expect a class's designed behavior (Z2M's Tuya countdown plugs report no timer) without leaving the other
integrations' devices untested.
Touches: ha_rig/engine.py (Step, _parse_step, _check_step_scope, own-file scoping, Run.steps), docs/rig.md (Writing a
step file), tests/
Tests-with: DeviceSide/DeviceSide#465

## Why

deviceside's shared `tests/rig/steps/get_attributes.yaml`, `reads_a_running_timer`, expects `timer.remaining_s` from
every `switch_plug`. Z2M's Tuya countdown plugs report no timer by design (deviceside: Z2M `get_attributes.py`
docstring; research `2026-11-15-tuya-countdown-probe.md`, P6), so the case fails on all four lab plugs. ZHA reads the
same plugs' timers and names its class `tuya_countdown` too, so the selector is scoped per integration. Rejected (Jason,
2026-11-16): a Z2M-only step file plus `only:` on the shared case, which would leave a future Z2M plug that does read
back untested.

## Design (settled, Jason 2026-11-16)

1. **Syntax.** A step takes `classes:` or `except_classes:` beside its kind, as it takes `only:`. Each one maps an
   integration key to a non-empty list of class prefixes:
   ```yaml
   - expect_response: {attributes: {timer.remaining_s: {min: 15, max: 20}}}
     except_classes: {zigbee2mqtt: [tuya_countdown]}
   - expect_response: {attributes: {timer: null}}
     classes: {zigbee2mqtt: [tuya_countdown]}
   ```
   A step takes at most one of the two keys. It may still take `only:`, and both must hold.
2. **Matching.** By prefix: `tuya_countdown` matches `tuya_countdown` and `tuya_countdown_tz3000_0zfrhq4i`. A target
   matches when any class it reports matches any prefix.
3. **Where the class comes from.** It is the classes of the case's own target in the most recent DeviceSide call's
   response that had an entry for it (`reported_classes` over `entries(response, target)`, unioned over the entries).
   Setup calls count. The class is read at run time, step by step, because the rig only learns it from a response.
4. **In another integration's suite** a selector that doesn't name the suite's integration is inert: a
   `classes:` step doesn't run there, and an `except_classes:` step runs.
5. **No class known** (no call has answered yet, or the response names no class, or the integration names no classes,
   `names_classes`) fails the case when the selector names the suite's integration, with a message naming the step.
   It never guesses either way.
6. **Load-time checks** (SchemaError/ValueError, as `only:` gets): the value is a mapping of non-empty string keys to
   non-empty lists of non-empty strings; both keys on one step are refused; a selector on a step with no `call` step
   before it in the test (setup then steps) is refused, since no class can be known there; on a test with `only:`, a
   selector key outside it is refused (as `_check_step_scope` does for `only:`); in an integration's own step file, a
   key naming another integration is refused (as `only:` is there).
7. **Observed and relaxed runs** apply the selector the same way.
8. **Docs.** docs/rig.md, Writing a step file, beside the step `only:` paragraph: what the keys do, prefix matching, and
   that the class is the target's as the last DeviceSide response reported it.

## Steps

1. Engine, docs and tests as above (one PR). Checks: `.venv/bin/pytest`, `pre-commit run --all-files`.
2. deviceside #465: the shared step change in `tests/rig/steps/get_attributes.yaml`, `reads_a_running_timer` (both
   `remaining_s` expectations get `except_classes: {zigbee2mqtt: [tuya_countdown]}`, and a `timer: null` expectation
   with `classes:` follows the first), then the Z2M rerun from deviceside's sign-offs handoff.
