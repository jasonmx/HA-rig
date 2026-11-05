# Plan: rig steps for Shelly persistent auto-off / auto-on

## Context

DeviceSide PR #253 (merged into `release/v0.1.0`) added `set_auto_off`, `set_auto_on`, `clear_auto_off`,
`clear_auto_on` and `auto_off`/`auto_on` rows in Shelly's `get_attributes`, all `experimental=True`. Step 8 of
`../deviceside/notes/plans/2026-11-05-persistent-auto-off.md` hands the live proof to this repo: the records that
clear the flags, the interplay with `turn_on_for`, the real `auto_*_delay` bounds (`AUTO_DELAY_LIMITS` in
`integrations/shelly/rpc.py` is a guess: 0..604800), and the ~60 s Shelly entry reload after every `SetConfig`.

Two things the rig can't do today:
- **Put back device config.** `Snapshot` (`ha_rig/engine.py`) restores on/off and light attributes only. A case that
  dies with auto-off set leaves a relay that switches itself off after every press.
- **Wait out a reload it didn't cause.** Core's Shelly coordinator reloads the entry 60 s after the first
  `config_changed` (`Debouncer(cooldown=ENTRY_RELOAD_COOLDOWN, immediate=False)`: later writes in the window coalesce,
  they don't push it back). Entities go unavailable and return; a call landing then fails.

Interview decisions:
- **Cleanup:** journal each target's auto rows and restore them as found (restore and `--recover`), before on/off.
- **Reload wait:** engine-tracked. A write marks the entity reload-pending; the next `call`/`set` on it and its
  restore wait it out. Checks don't wait, so a countdown can still be watched.
- **Discovery:** assert the likeliest outcome; the first run's failures correct the YAML and give the bounds.
- **No-reload proof:** every one of the four files proves an `unchanged` call is followed by no reload (~75 s each).

Proof follows the call: `switch_plug` covers the Switch component (the `light_onoff`/`light_wrapped` roles ride its
record), `light_color` covers RGBCCT. Every new test is `only: [shelly]`, roles `[switch_plug, light_color]`.

## Steps

Worktree `wt/rig-auto-off` off `main` here, one PR. Commit the plan into it first as
`docs/plans/2026-11-05-rig-persistent-auto-off.md`.

1. **Reload tracking** (`ha_rig/profile.py`, `ha_rig/engine.py`)
   - `ENTRY_RELOADS = {"shelly": EntryReload(actions=frozenset({set_/clear_auto_off/_on}), cooldown_s=60,
     deadline_s=100)}` beside `SETTLE_S`, with a comment citing core's `ENTRY_RELOAD_COOLDOWN`.
   - After a `call` of one of those actions whose row is `confirmed` or `sent` (a write; `unchanged` and `rejected`
     sent no SetConfig), start a watcher task for that entity *at once*: `ha.wait_for` the state to go
     `unavailable`, then to leave it, within `deadline_s`. Starting it at the write means a quick reload can't
     slip past while checks run.
   - `_call` and `_set` await any pending watcher on their entity first; so does the case's restore, and the case
     end (so the next case never lands mid-reload). A watcher that times out fails the step with "no entry reload
     within 100 s", which is itself a finding (the documented side effect didn't happen).
   - Mirror the `busy`/`mark_busy` shape already there rather than inventing a second mechanism.

2. **`expect_no_reload: {for: <expr>}`** step: `wait_for` the entity going `unavailable` over `for` seconds; seeing
   it fails, the timeout passes. Add it to `CHECKS`, so an observed run turns it into a wait. Document it in
   `docs/rig.md`'s step table.

3. **Auto config in the snapshot** (`Snapshot` in `ha_rig/engine.py`, `recover_case` in `scripts/rig.py`)
   - `take`: when the case's file action or `test.calls` includes an `ENTRY_RELOADS` action, read
     `deviceside.get_attributes` for the entity (whatever its state) and keep `auto_off`/`auto_on` in a new
     `auto` dict, journaled in the existing `cases[key]` entry beside `taken`/`stored`/`power`. Other cases pay nothing.
   - `restore`: config first, then on/off (else a lingering auto-on undoes the restore's turn-off). For each row,
     `None` calls `clear_auto_<x>`, `{"duration": d}` calls `set_auto_<x>` with `d`. Both are idempotent, so an
     untouched device sends nothing. A write starts a watcher (step 1) and restore awaits it before returning.
     A failure joins `keep_unrestored`, as now.
   - `recover_case` passes `auto=entry.get("auto", {})` and prints what it puts back.
   - Devices start disabled, so they end disabled; restore as found is what leaves them that way.

4. **Step files** (`ha_rig/steps/`), each `only: [shelly]`, roles `[switch_plug, light_color]`:
   - `set_auto_off.yaml`
     - `applies_to_any_turn_on`: `set: off`; `call: {duration: 6}` → `status: expected`; `call: {duration: 6}`
       (waits the reload) → `status: unchanged, detail: already_set`; `expect_no_reload: {for: 75}`;
       `set: {state: on}` (plain HA: core's Set has no `toggle_after`, so only the device's own setting can turn
       it off); `wait: duration - tol`; `expect_state: on`; `expect_state: {state: off, within: 2 * tol}`.
     - `takes_the_bounds`: `call: {duration: 0.05}` → expected; `call: {duration: 604801}` →
       `clamped_to: 604800`, expected; `call: {action: get_attributes}` → `auto_off.duration: 604800`. Device
       stays off throughout. A SetConfig error here is how run 1 finds the real bounds.
     - `turn_on_for_shorter_wins_per_call` and `turn_on_for_longer_wins_per_call` (guess: `toggle_after` replaces
       the persistent delay for that turn-on): auto-off 20 + `turn_on_for` 6 → off at ~6; auto-off 6 +
       `turn_on_for` 20 → on at 6 + 2·tol, off at ~20. Together they tell per-call / persistent / shortest apart.
   - `set_auto_on.yaml`: `applies_to_any_turn_off` (mirror: `set: on`, write, repeat unchanged, no reload,
     `set: off`, on after the delay) and `takes_the_bounds` (device on throughout).
   - `clear_auto_off.yaml`
     - `clears`: `call: {action: set_auto_off, duration: 6}`; `call: {}` (waits the reload) → expected;
       `set: {state: on}` (waits the second reload); `wait: duration + 2 * tol`; `expect_state: on`.
     - `unchanged_when_clear`: `call: {}` → `unchanged, already_clear`; `expect_no_reload: {for: 75}`.
     - `countdown_survives_a_clear` (guess: it does): `set_auto_off` 60; `call: {action: turn_on_for, duration:
       20}` (waits the reload); `wait: 3`; `call: {}` → expected; `expect_state: on`; `expect_state: {state: off,
       within: 17 + 2 * tol}`. All inside the clear's 60 s debounce, so no reload interrupts it.
   - `clear_auto_on.yaml`: `clears` and `unchanged_when_clear`, mirrored.
   - `get_attributes.yaml`: `reads_auto_off_and_on`: read → `auto_off: null, auto_on: null`; `set_auto_off` 30;
     `set_auto_on` 40; read → `auto_off.duration: 30, auto_on.duration: 40`. Check `_by_modes`/`per_light` passes
     non-color rows through on `light_color`.
   - Set each `rig_seconds` from the reload count (~75 s a write, restore included); the plan adds roughly an hour
     to a full Shelly suite, mostly reload waits. Print it in the PR body.

5. **Tests** (`tests/`, with `rig_fakes.py`): the watcher starts on a write and not on `unchanged`; `call`/`set`
   and restore wait for it; it fails on no reload by the deadline; `expect_no_reload` passes and fails; snapshot
   takes auto rows only for auto cases, journals them, restores config before on/off, and `recover_case` puts them
   back. Update `test_the_step_files_keep_their_roles` for the four new files.

6. **Docs:** `docs/rig.md` step table (`expect_no_reload`), a line under Writing a step file on the engine's reload
   wait, and Shared state's Journal bullet (Shelly auto config).

7. Pre-push: `.venv/bin/pytest`, `pre-commit run --all-files` (household check), `/audit-code` on the diff. Open
   the PR; merging into `main` here is Jason's.

8. **Discovery runs: on the Shelly color bulb only** (Jason's rule: no discovery on the relays). Each needs Jason's
   yes at the gate. From the deviceside checkout on `release/v0.1.0`, in the background:
   `../ha-rig/scripts/rig.py --integration shelly --action set_auto_off -k light_color` (then the other three and
   `get_attributes`). Where a guess fails, read the report, correct the YAML to what the bulb did, and note the
   finding for deviceside (precedence line in `docs/integrations.md`, `AUTO_DELAY_LIMITS`). Rerun until green on
   `light_color`. Afterwards confirm its `get_attributes` reads `auto_off: null, auto_on: null`.

9. **Relay run** (`switch_plug`), only once the bulb is green, with its own yes at the gate: the same suites,
   expectations settled. The Switch component may disagree with RGBCCT on precedence or bounds; if a relay case
   fails, stop and bring the report to Jason rather than iterating on the relays. Confirm the relays read
   `auto_off: null, auto_on: null` afterwards.

## Out of this PR (deviceside follow-up)

Fix `AUTO_DELAY_LIMITS` from run 1's error text, commit the records (`docs/test-records/shelly/{set,clear}_auto_*.md`,
refreshed `get_attributes.md`), clear the five `experimental` flags, fill in the precedence line. If the bounds
change the adapter's code, the records need rerunning against that commit (`check_release.py --records`).

## Verification

- `.venv/bin/pytest` green, `pre-commit run --all-files` clean.
- `scripts/rig.py --dry-run --integration shelly` lists the four new suites and the new `get_attributes` case on
  both roles, with no skips.
- A live Shelly run passes for all five suites and writes their records into the deviceside checkout; the run log
  shows one reload per write and none after an `unchanged`.
- Kill a run mid-case after a `set_auto_off` write: `--recover` clears it, and `get_attributes` reads it null.
