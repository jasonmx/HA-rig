# Rig authorisation: two levels (unrestricted / restricted)

Save to the repo as `docs/plans/2026-11-06-rig-authorisation-levels.md` on the implementing branch.

## Context

Today's guardrails mix two jobs in one variable: `DEVICE_SIDE_RIG_APPROVED=1` means both "skip the y prompt" and
"Jason said yes", and the blocker sensor gates by *who started the run* rather than by *what the work disturbs*. So a
probe needs no approval at all while the sensor is off, a human's override silently turns off the mid-run watcher,
`--recover` and the pre-gate replay move lights with no check, and the only thing behind the env var is a CLAUDE.md
line.

The new model classifies the **work**, not the caller:

- **Unrestricted** — runs at any time, sensor on or off:
  - test runs and `--recover` on a profile with `pre_authorized: true` (the flag covers everything that profile's run
    does: pauses, DeviceSide reloads/disables, roaming, HA restarts);
  - `--deploy-only` (HA restarts are always allowed);
  - probes declared `--visual none`, or `--visual profile` on a pre-authorised profile.
- **Restricted** — everything else. Goes when **either** the sensor is off **or** Jason has explicitly approved it.
  Not both.
- The y/N prompt is a **readiness check**, shown only at a terminal; agents (no tty) never see it and need no variable
  for unrestricted work.

Decisions settled in the interview:

| Question | Decision |
|---|---|
| Explicit approval | New env var `DEVICE_SIDE_RIG_RESTRICTED=1`, backed by a Claude Code `ask` permission rule so the harness prompts Jason, not just CLAUDE.md |
| Terminal y while sensor on | Counts as approval, with a distinct prompt (`Restricted: <sensor> is on. Proceed anyway?`) |
| HA restart | Allowed any time |
| Probe classification | Declared by flag, trusted; undeclared = restricted |
| `pre_authorized` scope | Covers the whole run |
| Mid-run abort | On the sensor **turning on** (off → on edge), for every restricted run, approved or not |
| Restoring orphans | Restricted by the profile that left them |
| Old `DEVICE_SIDE_RIG_APPROVED` | Refused with a message pointing at the new variable (fails loudly, never silently means something else) |
| Sensor name | `binary_sensor.rig_restricted`; key `RIG_RESTRICTED_ENTITY`, `RIG_BLOCKER_ENTITY` still read as a fallback |

## Design

### One decision function — new `ha_rig/authorise.py`

Pure, table-tested, no I/O:

```python
@dataclass(frozen=True)
class Decision:
    go: bool
    prompt: str | None    # readiness / restricted question to ask at a tty, None = don't ask
    watch: bool           # arm the edge watcher
    reason: str           # printed in the plan

def authorise(*, restricted: bool, sensor: tuple[bool, str] | None,  # rig_clear's answer; None = no sensor configured
              approved: bool, tty: bool) -> Decision
```

Rules:
- `restricted=False` → go; prompt "Proceed?" only if `tty`; watch False.
- `restricted=True`, sensor clear (or no sensor configured) → go; readiness prompt if `tty`; watch True (if sensor).
  - **No sensor configured and restricted:** decide here — recommended: treat as *not clear* (fail safe), so restricted
    work needs approval. Today an unset key gates nothing; this flips that. Flag it in the plan/PR.
- `restricted=True`, sensor not clear (on / unknown / unreadable / restoring) →
  - `approved` → go, no prompt, watch True;
  - `tty` → prompt `Restricted: <reason>. Proceed anyway?`, a y = approval, watch True;
  - else refuse: `Restricted, and <reason>. Needs DEVICE_SIDE_RIG_RESTRICTED=1 (Jason's yes) or the sensor off.`

`tty` = `sys.stdin.isatty()`; keep `ask()`'s EOF-is-no as a backstop.

### Classifying the work

- `Profile.pre_authorized` (`ha_rig/profile.py:142`) already exists; it is now the run's classification.
- Test runs (`run_held`, `scripts/rig.py:932`): `restricted = not profile.pre_authorized`.
- `--deploy-only` (`deploy_only`, `:745`): never restricted; no prompt needed (it moves no device). Unchanged apart
  from docs.
- Probes (`hold_command`, `:1099`): new `--visual {none,profile}`.
  - `none` → unrestricted.
  - `profile` → unrestricted only if the chosen profile is `pre_authorized`; else error out
    (`--visual profile needs a pre-authorised profile`) rather than quietly downgrade.
  - omitted → restricted.
  - Replace `probe_approved` (`:1133`) with `authorise`. Keep the second check after the lock wait for restricted
    probes that went on the sensor path.
- `--recover` (`recover_all`, `:1166`) and the pre-gate `replay` (`:1320`): classify **per orphan journal** by its
  `profile` field (`orphan.get("profile")`, already used in `claim` at `:925`). Unknown profile or legacy journal →
  restricted.
  - Authorised orphans are restored as today.
  - Unauthorised ones are left, and named: `left <path> (profile <name>, restricted): sensor off or
    DEVICE_SIDE_RIG_RESTRICTED=1 restores it`.
  - `--recover` exits nonzero while any orphan is left (already its contract).
  - **Side effect to guard:** a run whose locks cover a restricted orphan it can't restore would test against stale
    state on that integration. Refuse such a run before its gate, naming the orphan, instead of running on top of it.
  - Replay currently runs *before* the gate prompt; it must now use the run's own authorisation decision, so move the
    `authorise` call ahead of `replay` in `run_held` (the plan printing can stay after).

### The watcher — edge-triggered

`watching()` (`:453`): today it aborts on two reads in a row of a `BLOCKED` state. Change to abort on two reads in a row
of `BLOCKED` **after at least one read of `off` since the run started**. So:
- a run started on the clear path aborts when the sensor turns on (as today);
- an approved run started while the sensor is on keeps going, and aborts only if the sensor goes off and then on again;
- failed reads still neither count nor reset (HA restarts mid-deploy).

Arm it for every restricted run and probe (`Decision.watch`), regardless of approval. Fixes today's gap where a terminal
override disarmed it.

### Approval signal and harness enforcement

- `DEVICE_SIDE_RIG_RESTRICTED=1` is read in `authorise`'s caller only. `DEVICE_SIDE_RIG_APPROVED` set → `SystemExit`
  with the migration message.
- Harness rule: add to `~/.claude/settings.json` (user scope, so it covers deviceside worktrees, where the rig is run
  from, and ha-rig) an `ask` permission for commands carrying the variable. **Verify the pattern before relying on
  it:** prefix rules match the command start, so test each of `DEVICE_SIDE_RIG_RESTRICTED=1 ../ha-rig/scripts/rig.py`,
  `env DEVICE_SIDE_RIG_RESTRICTED=1 …` and `export DEVICE_SIDE_RIG_RESTRICTED=1; …` and add a pattern (or a `deny`
  for the `export`/`env` forms) for any that slip past. Record in the PR which forms the rule catches. Use the
  `update-config` skill for the edit.

### Rename

- `.env.rig` key `RIG_RESTRICTED_ENTITY`; `gate_sensor` (`:419`) reads it, falling back to `RIG_BLOCKER_ENTITY` with a
  one-line deprecation note.
- Renaming the HA entity itself is Jason's (it lives in the household HA and the gitignored `.env.rig`). The code never
  names it.
- Rename functions to match: `gate_sensor` → `restricted_sensor`, `BLOCKED` stays as the state set.

## Files

- `ha_rig/authorise.py` (new): `Decision`, `authorise`.
- `scripts/rig.py`: `gate`/`probe_approved` replaced; `run_held`, `hold_command`, `recover_all`, `replay`,
  `watching`, `gate_sensor`, the argparse block (`--visual`), the module docstring (lines 2–39).
- `ha_rig/profile.py`: no logic change; docstring for `pre_authorized` says it covers the whole run.
- `ha_rig/env.example`, `ha_rig/profile.example.yaml`, `docs/rig.md` ("The blocker gate" → "Authorisation",
  `--deploy-only`, probes at line 110, `--recover`).
- `CLAUDE.md` (ha-rig): the gate sentence becomes "never set `DEVICE_SIDE_RIG_RESTRICTED=1` without Jason's yes for
  that run"; drop the `--deploy-only` aside (now covered by the level rule).
- Tests: `tests/test_authorise.py` (new, the decision table); update `tests/test_rig.py`, `test_rig_locks.py`,
  `test_rig_roam_run.py` where they set `DEVICE_SIDE_RIG_APPROVED` or test `gate`/`probe_approved`/`watching`.
- deviceside: none (no change to what a record proves). Confirm with `rg` in the deviceside checkout.

Note: worktree `../ha-rig-wt-run-findings` touches the same files (`scripts/rig.py`, tests, docs). Coordinate or merge
after it lands.

## Verification

1. `.venv/bin/pytest` and `pre-commit run --all-files`.
2. Decision table tests cover every row: unrestricted × {tty, no tty}; restricted × sensor {off, on, unreadable,
   unset} × {approved, tty, neither}.
3. Watcher tests with a fake sensor sequence: `off,on,on` aborts; `on,on,on` (approved) doesn't; `on,off,on,on`
   aborts; `off,fail,on,on` aborts; `off,on,fail,on` aborts (fail doesn't reset).
4. Replay/recover tests: orphans from a pre-authorised profile restored with sensor on; a restricted profile's left
   and named; a run whose claim covers a left restricted orphan refuses before its gate.
5. `DEVICE_SIDE_RIG_APPROVED=1` exits with the migration message.
6. Live, with Jason's yes, `--dry-run` only: print the decision for a pre-authorised profile, a restricted one, and a
   `--visual none` probe (`--hold KEY --visual none -- true`), with the sensor on and off.
7. Harness: confirm the `ask` rule prompts for each command form listed above.
