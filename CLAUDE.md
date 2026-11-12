# ha-rig

The private test rig for DeviceSide (`DeviceSide/DeviceSide`, public). It tests a deviceside checkout and writes test
records into it: see `docs/rig.md`, Setup. What a record must prove, when an adapter's `experimental` flag clears, and
how releases gate on records are deviceside's rules (its `CLAUDE.md`); a change here that alters what a record
proves needs a matching change there.

**No household details in commits**, though this repo is private: entity IDs, device names, MACs, hostnames and
tokens stay in the gitignored `profiles/`, `.env.rig`, `.private-terms` and `CLAUDE.local.md`. The pre-commit
household check (`scripts/check_private.py`) enforces it.

**The rig moves the household's real lights and restarts its HA.** Drive it only through `scripts/rig.py`, behind its
authorization gate (`docs/rig.md`, Authorization); never set `DEVICE_SIDE_RIG_RESTRICTED=1` without Jason's yes for that run.

## Layout

- `scripts/rig.py`: the runner; `scripts/deploy.sh`: its deploy, through `.env.rig`'s commands
- `ha_rig/`: the engine, HA and Hue clients, locks, journal, baseline, roaming, run logs, profiles, the target
  checkout (`target.py`) and the household terms for deviceside's check (`terms.py`)
- `tests/`: the rig's own tests, with fakes and fixture step files (`tests/steps/`); they import deviceside's registry
  and actions and read its step files from the target checkout, the step files only to check they parse
  (`tests/test_rig_steps.py`); the engine tests take adapters from a declared roster over that registry
  (`tests/rig_fakes.py`, `Roster`)

## Changes

- Plans live in `docs/plans/`. The planning session commits and pushes the plan on the `wt/` branch the work will
  use, before handing off, so a fresh worktree has it: an untracked plan in the main checkout is invisible there
  and blocks its fast-forward on merge.
- Setup: `uv sync`. Keep the `dev` group's pins in step with deviceside's `pyproject.toml`.
- Before each push: `.venv/bin/pytest` and `pre-commit run --all-files`.
- Before opening the PR: `/audit-code` on the diff, then fix its findings and carry on to the PR; stop for Jason
  only on a big judgment call. Judge each finding by the codebase's long-term health, not the PR's convenience:
  fix it now unless it is genuinely out of scope, and then say where it is tracked.
- A PR that changes `scripts/rig.py`'s options regenerates README's `--help` block (`COLUMNS=100 scripts/rig.py
  --help`); `tests/test_rig.py` fails until it does.
- The step files are deviceside's (`tests/rig/steps/` there), landed in the deviceside PR for the action they test;
  a rig PR is needed only for an engine change. When one pairs with a deviceside PR (a step file using a new step
  kind, say), the rig PR's plan names it in Coordination, `Tests-with: DeviceSide/DeviceSide#N`, so CI tests the
  pair together (`scripts/ci_deviceside.sh`); merge the rig PR first, as the rig can't run a step kind its engine
  lacks. Nothing reruns it when that PR moves or merges, and `ci-green` counts every run at the head: before merging,
  `gh run rerun` the PR's Tests runs, which read both afresh.
- Async throughout; American English, matching HA.
