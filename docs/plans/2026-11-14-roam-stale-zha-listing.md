# Roam Hue -> ZHA: a stale ZHA listing read as the join (#86)

## Coordination

Intent: stop a roaming move to ZHA or Z2M from counting a bulb's leftover listing on the target as its arrival, and
stop the move where a stale listing can't be removed.
Touches: ha_rig/roam.py, scripts/rig.py (check_roaming), tests/rig_fakes.py, tests/test_rig_roam*.py
No deviceside pairing: no step kind or record change.

## Why

#86: a Hue -> ZHA move printed the bulb's stale ZHA listing removed (three times), then `Moving ... to ZHA.` and
`... on ZHA.` at once, with no permit-join window; the journal said `at: zha` and `arrived`; going home then failed
with `listed on ZHA but live on none`.

The issue's lead (the Hue entity keeping the target entity ID) doesn't hold: `Roamer.joined("zha")` reads the device
registry's `zha` identifiers only (`HAClient.device_by_ieee`, `device_ieee`), which no Hue device carries.

The cause, in the code:

1. `Roamer.purge` calls `zha.remove` for a dead ZHA listing and prints it removed without checking. HA drops a ZHA
   device's registry entry only once zigpy reports the device removed; for a device zigpy doesn't hold, or a remove
   still running, the entry stays. Each `locate` then found the same dead listing and "removed" it again, as did
   `move`'s pre-join purge.
2. `join`'s `all_in` counts a bulb in once `joined(target)` finds any listing, so the leftover entry read as the
   arrival on the first poll: the window opened and closed in the same instant, `arrived` was journaled, and
   `adopt` renamed the stale ZHA light (whose device it found) to the fixed entity ID.

## Fix

- `move` purges the target's stale listings before it releases anything, so one that stays stops the move with
  every bulb still where it was.
- `purge` of a ZHA listing polls until HA's registry no longer lists it, for up to `PURGE_S`; else a RoamError
  saying so, with what to do. Z2M's force remove already waits for its device list (`z2m_force_remove`).
- A bulb counts as arrived on ZHA or Z2M only once it is listed and live there (`Roamer.arrived_on`); Hue keeps its
  bridge listing, which a search makes fresh. `arrived` and `at` follow from that; `missed` names where a bulb is
  live, and names a dead listing as such. The missed-Hue-Leave retry reads "on no network" as live on none, so a
  dead listing doesn't cost a bulb its search.
- `--check` (`check_roaming`) reports a RoamError from `locate` as a problem instead of crashing on it.

## Tests (engine, fakes)

- A ZHA registry entry that survives `zha.remove`: the move stops before any release; the journal and the bridge
  keep the bulb on Hue.
- A dead listing ZHA adds back during the window (a Hue entity still under the fixed ID, the ZHA join never comes):
  a join timeout, not an arrival, and the journal stays off ZHA.
- A dead target listing appearing after `settle` that `zha.remove` can't clear: the move stops before any release.
- A bulb that missed the bridge's Leave while ZHA lists it dead: still searched back to Hue, released alone, joins.
  (FakeHA's `join` now gives a rejoining ZHA device its registry entry and light back, as HA does.)
- A Hue entity keeping the fixed entity ID while no ZHA join comes: a join timeout (a guard for the issue's lead).
- `--check` with a ZHA registry entry that survives `zha.remove`: the problem named.
- A normal Hue -> ZHA move: the Hue delete, the window, `arrived` and `at` journaled.

## Verification

1. `.venv/bin/pytest`, `pre-commit run --all-files`, `/audit-code` on the diff, then the PR (per CLAUDE.md).
2. Live, Jason: the A60 roaming run from a deviceside checkout once its leftover ZHA device is gone from HA.
