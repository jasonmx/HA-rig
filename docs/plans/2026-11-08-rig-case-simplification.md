# Rig case simplification: cut functional duplicates, bias to Documented

## Context

A fresh full rig run on the house profile is estimated at 10,578 s (~2.9 h). Four case-to-code maps (one per
integration, in the planning session's scratchpad; their findings are folded in below) showed many cases exercise
no adapter branch, wire call or firmware behavior another case doesn't. The biggest cost is Shelly persistent
auto-off/on: ~24 cases at ~150 s each (each write and each restore pays a ~60 s entry reload; eight 75 s no-reload
waits). Jason chose: Proposal A (in-rules cuts), Proposal B (firmware-fact tier, Shelly restructure, one option
pass), the minimal Shelly auto design, two tenets that bias future work to Documented, and two bug fixes. Target: a
full run ~1.5 h shorter.

First step of the implementing session: copy this plan to `ha-rig/docs/plans/2026-11-08-rig-case-simplification.md`
(rig work) and run `pr-draft` there; the deviceside part gets its own branch and PR.

## Coordination

Intent: Cut duplicate rig cases, add a firmware-version tier for firmware-fact cases, codify "default to Documented".
Touches: ha_rig/engine.py, ha_rig/coverage.py, ha_rig/progress.py, ha_rig/steps/*.yaml, tests/test_rig_steps.py,
tests/test_rig_engine.py, tests/test_progress.py, tests/test_coverage.py, tests/test_rig.py, docs/rig.md, CLAUDE.md
Tests-with: jasonmx/deviceside#301

## Design decisions (settled)

1. **Fold prefix cases into the case that contains them; fold toward the action under test** so its record keeps
   the case. Carry over every distinct check (ranges, `expect_state: on`, the +3 s re-read, `observe:` blocks).
   - `stop.halts_a_raise` + `raise_brightness.raise_stop_raise` → one case in `stop.yaml`; remove from raise file.
   - `stop.halts_a_lower_to_off` + `lower_brightness.lower_stop_lower` → one case in `stop.yaml` (keep 47–82 range,
     `expect_state on`, Z2M observe).
   - `raise_hue.cycles_up_the_wheel` and `stop.hue_cycle_stop` → into `raise_hue.hue_stop_raise` (add the +3 s
     re-read and the observe). `stop.yaml` keeps a hue stop via the folded case only if the stop record needs it:
     keep `hue_cycle_stop` in `stop.yaml` and drop `cycles_up_the_wheel` if moving it would leave stop's hue arm
     unrecorded.
   - `stop.saturation_down_stop` → into `halts_brightness_and_saturation` (re-read, observe on light_color).
   - `raise_brightness.raise_from_off` → covered by `raise_after_soft_off`; drop.
   - Keep `lower_to_off`: the only case using the default sweep time.
2. **Relay fan-out:** `on_then_off`, `off_then_on`, `reads_a_running_timer` run on `switch_plug` only; the
   `drops_brightness_*` cases keep light_onoff and light_wrapped covered.
3. **Group copies only where the group branch differs.** Drop `[group]` from `min_brightness_raises`,
   `min_brightness_leaves_brighter`, `max_brightness_lowers`, `stop_when_idle`, `get_attributes.reads_while_off`.
   Keep groups on ramps and `set_attributes.sets_*` (prediction means, ZHA `via_group`, Sengled drops-while-off,
   Z2M group native-control). Per-integration `only:` where a group branch exists on one integration alone.
4. **Smaller cuts:** `bulb_brightness_then_off` limited to `only: [zha]` (transition case covers brightness on
   Shelly/Z2M); drop `announced_with_both_color_modes` (implied by `keeps_and_reads_hs_while_off`); trim
   `reads_a_group_timer` to its first read; make the `wait: 2  # ZHA ignores…` steps ZHA-only (a step-level
   `only:` in the engine, or split the step).
5. **Never cut a field case** (fields are never Documented): keep `max_brightness_lowers[light_ct]`,
   `sets_xy_while_off`, Z2M `bulb_rgb_then_off`, rgb/hs/xy on Shelly.
6. **lifecycle:** one integration (Hue: it has a `call_resolver`), one role (`light_ct`). Top-level `only: [hue]`.
7. **Firmware-fact tier (B).** New per-test key `firmware_fact: true`. Such a case is covered while a saved result
   or record exists for the same device model + firmware (from the record's Device row / the ledger's facts),
   regardless of adapter code changes. Coverage (`ha_rig/coverage.py`) and the ledger (`ha_rig/progress.py`) key
   these cases on firmware, not code; `--fresh` still reruns them. Records list them with the firmware they ran at.
   Mark: Shelly precedence (`turn_on_for_*_wins_per_call`), `countdown_survives_a_clear`, `takes_the_bounds`,
   `lower_brightness.soft_off_keeps_level`, `raise_brightness.late_stop_report`,
   `raise_brightness-predicted_state.bridge_scene_recall_mid_fade_shows_at_once`. deviceside's
   `check_release.py --records` must accept a record whose stale-by-code cases are all firmware facts at unchanged
   firmware; if that needs records to carry per-case freshness, the rig writes it and check_release reads it.
8. **Shelly auto, minimal.** Plug (`Switch`) only; bulb's `RGBCCT` moves to Documented in
   `shelly/addressing.py` for the auto adapters (keep RGBCCT Verified for the other actions).
   - `set_auto_off.yaml`: one regression case: set 6 → repeat (expect `unchanged`, no `expect_no_reload`) →
     `get_attributes` reads `auto_off.duration` → plain turn on → off after delay. Plus the firmware-fact cases.
   - `clear_auto_off.yaml`: one case: set → clear → turn on → stays on; `covers: [set_auto_off]` not needed if
     four files keep one case each. Mirror for auto_on. Drop `unchanged_when_clear` (unit-tested, `test_auto.py:45,75`)
     and `get_attributes.reads_auto_off_and_on` (folded into the set cases).
   - Optional engine key `covers: [<action>]` letting one case count toward a second record: implement only if
     it stays small; otherwise four cases (~9 min).
9. **One options pass.** The engine pins an option once for all of an integration's option suites in a run
   (`*-predicted_state`), instead of a pin/unpin reload per file.
10. **Bug: `set_scene.applies_while_on`** must be able to fail: setup at 6500 K / a level far from the scene, then
    `expect_state` on `light_ct` (a scene member) at the scene's level or color.
11. **Bug: ZHA `group_color` never runs** (ZHA makes no group under 2 members; the rig has one color bulb). Take
    ZHA's color groupcast out of the stable scope (experimental or refused, per the release-flag rules) and add a
    row to `docs/hardware-the-rig-lacks.md` "Testers needed": built, awaiting a tester with two color bulbs on ZHA.
    Leave the doc's pending `[@Claude: merge…]` note alone unless it is trivial alongside.

## Tenets (exact text)

deviceside `CLAUDE.md`, first lines of **Documented**:

> **Default to Documented.** A rig case earns its place only by catching what nothing else can: a call shape, a
> branch the adapter takes on a live reply, or a device behavior that DeviceSide or its docs rely on. One Verified
> case per call shape per adapter; every sibling that meets the test below rides it. A firmware fact (precedence,
> bounds, timing) is proven once per firmware version, not per code change. Confirming a stored setting counts as
> confirming the call.

Also amend the Documented criterion "never `confirmed`" so the last sentence above doesn't contradict it.

ha-rig `CLAUDE.md`, under **Changes**:

> Before adding a case, name what it catches that no existing case or unit test does: a different call shape, a
> live-reply branch, or a firmware fact. No name, no case.

## Steps

1. ha-rig worktree `wt/case-simplification`; copy plan; `pr-draft`.
2. Engine: `firmware_fact`, step-level `only:`, one options pass, optional `covers:`; unit tests in `tests/`
   (`test_rig_steps.py`, `test_coverage.py`, `test_progress.py`, `test_rig_engine.py`).
3. Step files: decisions 1–8, 10. `docs/rig.md` "Writing a step file" documents the new keys.
4. deviceside worktree `wt/documented-tenet`: tenet, Documented clause, Shelly auto RGBCCT → Documented, ZHA
   group_color scope + hardware doc row, `integrations.md` if any claim changes, `check_release.py` for decision 7;
   regenerate compat/README tables (`scripts/gen_compat.py`, `gen_features.py`); unit tests.
5. ha-rig `CLAUDE.md` tenet. `/audit-code` on both diffs, fix, open PRs.

## Verification

- `.venv/bin/pytest` and `pre-commit run --all-files` in both repos; rig CI with `Tests-with`.
- `scripts/rig.py --profile house --dry-run --fresh`: compare case count and estimate with the 10,578 s baseline;
  check no suite lost its last case and every remaining field case is present.
- `scripts/rig.py --coverage` at HEAD: firmware-fact cases show covered by firmware.
- `scripts/check_release.py --records` passes on deviceside.
- A rig run of the changed suites (Jason's authorization; Shelly needs the bulb powered — the lab Hue lamp shares
  its fixture) writes fresh records; `set_scene.applies_while_on` fails when the call is stubbed out (probe once).

## Implementation decisions

- **Decision 11 dropped** (Jason, 2026-11-08): deviceside's "Proof follows the call" rule (PR #204) already covers
  ZHA's color groupcast through the brightness group cases and the single-light color cases. No scope change and no
  hardware row; ZHA's `group_color` cases skip for want of the role.
- **`covers:` not built**: four auto files with one regression case each.
- **Firmware facts** (decision 7): the ledger keeps a firmware fact's pass in a `firmware` section keyed by suite
  and case, with the devices its part ran on and a digest of the test as written. Coverage lends it to the suite's
  entry at the fingerprint now (devices joined), so the record carries it, marked `(firmware fact)`, with the
  firmware under Device. It goes when it fails or reruns, its test changes, its entities change, or a later part
  shows a device of its model at another firmware. Never pruned for age; HA's version doesn't drop it.
- **`check_release.py` unchanged**: a record carrying lent facts cites the commit of the run that wrote it, so the
  gate's code comparison still holds; the deviceside tenet ("proven once per firmware version") is the rule change.
- **One options pass** (decision 9): `engine.by_options` groups a run's suites by the options they pin, each set in
  the order it first appears, so each set is saved once per run (`run()`), not once per file.
