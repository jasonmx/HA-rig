# A group's setup `set` retries once when a member is left stale

## Coordination

Intent: in a case's setup, a `set` on a group that leaves a member short of the set value sends the same call once
more before failing. Fixes `soft_off_stays_on_until_its_end[group]`'s setup (room set to 60 %, a member last
reported 47.43 %).
Touches: ha_rig/engine.py, tests/test_rig_engine.py, docs/rig.md

## Background

- deviceside `notes/plans/2026-11-11-hue-per-attribute-forget.md`, Not in this PR, first Rig bullet (Jason agreed).
- deviceside `docs/integration-internals.md`, Hue, Tested behavior: "A room member can be left stale": a room's
  member can last report a mid-fade value with nothing after it, and a plain HA `light.turn_on` of the room can leave
  one too.
- `Runner._set` (`ha_rig/engine.py`) already waits, through `await_state`, for **every** member to reach the state
  and the brightness (±2 of 255) within `tol` + `transition`. The 47.43 % member failed that wait. What's missing is a
  second chance: the bridge never reports again by itself, but a fresh command makes the bulb report.

## Decisions (Jason, 2026-11-11)

- **Setup only.** A `set` in `steps:` runs inside the wire tap, so a retry would add a traced HA call and change the
  case's trace. Steps keep failing on the first timeout.
- **Groups only** (`is_group(entity_id)`). A single light that misses its set still fails at once: nothing has shown
  a lone light doing it.
- **The same group call again**, the identical `light.turn_on`/`turn_off` with the same data, then the same wait
  (`tol` + `transition`) for every member. No member-only call: the setup would then send a call shape it doesn't
  declare.
- **One retry.** If the second wait also times out, the case fails with that wait's `StepFailure`, as now.
- **A run-log warning** on each retry (`_LOGGER.warning`), naming the group, the member and what it showed, e.g.
  `light.x: setup set to {...}; light.y showed brightness 121; setting the group again`. No change to records,
  case results or traces.

## Steps

1. **Know the phase.** `Runner.steps` gains `setup: bool = False`; `run` passes `setup=True` for
   `case.test.setup`, and the observed path's setup does too (it runs through the same call in `run`). Store it as
   `self.in_setup` around the loop (reset in `finally`), or pass it down; the handler signature
   `(args, following)` stays as is for every other step. Don't infer setup from `self.traced is None`: that is also
   None when there's no admin connection.
2. **Retry in `_set`.** Split its tail: the service call (with its `traced` append, which only matters outside
   setup) and the `await_state` wait. When `self.in_setup and self.is_group(entity_id)` and the wait raises
   `StepFailure`, log the warning (the failure's message already names the member and `_describe`s its state; reuse
   it), call the service again, and wait once more; a second `StepFailure` propagates. Scenes return before the wait
   as now, so they never retry.
3. **Tests** (`tests/test_rig_engine.py`, beside `test_set_waits_for_every_member_to_reach_the_brightness`):
   - A group setup whose member ignores the first `turn_on` and takes the second: the case passes, the fake saw two
     identical group calls, and the warning is in `caplog`.
   - A member that ignores both: the case fails on `setup step 1 (set)` after exactly two calls.
   - The same stale member with the `set` in `steps:` (not setup): fails after one call, so no retry outside setup.
   - A single light (not a group) in setup whose first set is lost: fails after one call.
   Each names what it catches: the retry, its bound, its phase limit and its target limit. Use a `FakeHA` subclass
   whose `call_service` drops a member's update for the first N group calls; keep `TOL` small as the neighbours do.
4. **Docs.** `docs/rig.md`, the step table's `set` row: add that in a setup, a group whose member misses the value
   is set once more before the case fails (the Hue fact in deviceside's integration-internals, Tested behavior).

## Not in this PR

- `lower_to_a_limit[group]` staying bridge-truth (#56, `wt/limit-fade-end`): Jason's call, separate PR. #56 also
  touches `ha_rig/engine.py`; check `pr-overlaps` and merge main in if it lands first.
- No rig run in this PR: the next Hue rerun (`--profile lab --integration hue`, from deviceside #335's worktree, with
  Jason's yes) exercises it, per the release choreography plan.

## Done when

- `.venv/bin/pytest` and `pre-commit run --all-files` pass; the four tests above are in.
- PR merged with `merge-green <n> --squash --delete-branch`, after the audit.
- Tell Jason so the deviceside plan's Rig bullet can be ticked (deviceside's notes aren't this repo's to edit).
