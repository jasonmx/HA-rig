# Findings from the first real-rig runs of --for and resume

Follow-ups to `docs/plans/2026-11-06-deploy-only-and-resume.md` (PRs #7, #9). One ha-rig PR for steps 1 and 2, one for
step 3 (separate concerns, separate reverts). Step 4 is a deviceside issue, not work here.

## Decisions taken in the interview

| Finding | Decision |
|---|---|
| A phase moves the bulbs, finds the budget spent, starts nothing, moves home | **A move buys one case, and needs a lead.** Start no move with under `MOVE_LEAD_S = 60` of budget left; once a phase has moved, its first device case starts whatever the budget. |
| `--for` counts from approval, so a deploy restart eats a short budget | **Keep counting from approval.** `--for` stays a wall-clock window that includes the deploy. Fix 1 keeps a short budget from wasting a move; say in the docs and the `Budget:` line that the deploy counts. |
| `--check` passed a ZHA profile whose bulb was paired to Hue | **Check each fixed entity's platform**, in `check_devices`, so both `--check` and the pre-run check catch it. |
| Z2M `cycles_up_the_wheel` hue travel +41° (expected +54° ± 12°), once | **Leave it; watch for a repeat.** No change. |
| Z2M `get_attributes` read "Delivery failed": unexpected DeviceSide WARNING | **Fix in deviceside**: the Z2M `Reader` retries a read once on a delivery failure. The rig stays strict: no `expected_log`. |

## Step 1: a move buys one case (`scripts/rig.py`, `ha_rig/engine.py`)

Cause: `run_phases` checks `budget.stops()` once per phase, before `visit`. The move, `wait_for_targets` and (phase 0)
the pause come between that check and `_Run.automated`'s per-case check, so the budget can run out in between.

- `Budget` gets `left` (seconds to the deadline, floored at 0) and a one-shot `grant()`: the next `stops()` call that
  would stop a **device case** returns False once instead, and clears the grant.
- `_Run.automated`: check the budget only before a case that runs (`run_case`). Skipped, not-observed and
  twin-failed cases are reported as now, whatever the budget: they cost no time and are results. `_Run.observed` the
  same: the check sits before `run_case`. So the grant is spent on the first case that moves a device.
- `run_phases`, before `visit`: when the phase moves the bulbs (a roamer, and the target network isn't where they
  are now; find how `Roamer.go` knows that), stop if `budget.left < MOVE_LEAD_S`, marking `stopped` (so the run
  exits 75). A phase that doesn't move keeps today's `budget.stops()` check. After a move, `budget.grant()`.
- `MOVE_LEAD_S = 60`, next to `ROAM_ESTIMATE_S`, commented as one move's join, rename and HA wait.
- Docs: `docs/rig.md`'s "Stopping and resuming" and the module docstring: the `--for` window counts from approval,
  deploy included; a move starts only with a minute left, and always runs at least its first case. The `Budget:` line
  in `print_plan` says "from approval, deploy included".

Tests: `tests/test_rig_roam_run.py`: budget with under 60 s left before a move: no move, bulbs home, exit 75; budget
spent during the move: the phase's first case runs, no second; a phase with no move and a spent budget starts
nothing. `tests/test_rig_engine.py`: skipped cases are still reported after the budget is spent; the grant goes to
the first device case, not a skipped one; in lanes, one grant total.

## Step 2: (no code) `--for` from approval

Nothing beyond step 1's doc lines. Note it in the PR body as decided.

## Step 3: `check_devices` checks the platform (`scripts/rig.py`)

Cause: `check_devices` only calls `get_state` and `available()`. A bulb moved to another network is renamed back to
the same entity ID after the join, so the profile's entity exists and is available, and the run fails later at
`bulb_hue`'s unique_id parse (`ha_rig/engine.py`, "no ZHA address in its unique_id").

- Read `config/entity_registry/list` once (`HAClient`'s registry list, `ha_rig/client.py`) alongside the states.
- For each fixed entity, the expected platform is its integration key's HA domain: reuse `runlogs.HA_DOMAINS`
  (hue, zha, mqtt, shelly, matter); don't add a third copy next to it and `roam.PLATFORMS`. That needs `targets` (or a
  sibling) to say each entity's integration; a group's `members` count as the group's integration.
- Exception: `light_wrapped` is a `switch_as_x` light over the integration's switch, so its platform is
  `switch_as_x`; check that rather than skip it.
- A mismatch is a problem: `<entity>: a <platform> entity, not <integration>; pair it back or fix profile <name>`.
  An entity missing from the registry (YAML-defined) is a problem too: the run can't trust it.
- Docs: `check_devices`' docstring and the `--check` help/docstring line: "in HA, on its integration, and available".

Tests (`tests/test_rig.py` or `tests/test_rig_network.py`, with the existing HA fake's registry): a ZHA-keyed entity
whose registry platform is hue is reported and `--check` exits non-zero; a matching one passes; `light_wrapped` on
`switch_as_x` passes; Z2M's mqtt platform passes.

## Step 4: deviceside issue (filed, not built here)

Filed as jasonmx/DeviceSide#277: the Z2M `Reader` retries a read once when Z2M logs the read failed with a delivery
failure (an unacknowledged frame), before raising. See the issue for the fix shape.

## Verification

1. `.venv/bin/pytest` and `pre-commit run --all-files` after each step.
2. Rig runs, each with Jason's yes, from the deviceside checkout, in the background:
   - `--check` on a fixed-bulb ZHA profile with its bulb on Hue: reports the platform mismatch, changes nothing.
   - A roaming profile with `--for` set to end inside the first move: the move's phase runs one case, then home, exit
     75; the rerun resumes.
