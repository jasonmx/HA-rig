# turn_on_for: Hue native control is put back after the timer's turn_on

Deviceside #302 makes each Z2M action set `hue_native_control` the way it needs for its own publish, then restore the
user's value. `turn_on_for` with a color no longer leaves the option on, so the four bulb cases that turn it off
first now expect it off again after the timer. The color landing (`color_temp_kelvin` in range, `color_mode: xy`)
still proves the adapter turned it on for the `light.turn_on`.

## Coordination

Intent: turn_on_for's Z2M bulb cases expect hue_native_control restored (false), not left on, matching deviceside #302.
Touches: ha_rig/steps/turn_on_for.yaml
Tests-with: jasonmx/deviceside#302

## Steps

1. The four `expect_z2m_option: {hue_native_control: true}` become `false`; the case comment says why.
2. Rig tests, pre-commit, audit (step-file-only: none), PR. Merge once deviceside #302 is ready to merge.
