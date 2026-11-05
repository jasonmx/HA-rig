# ha-rig

The private test rig for DeviceSide (`jasonmx/deviceside`, public). It tests a deviceside checkout and writes test
records into it: see `docs/rig.md`, Setup. What a record must prove, when an adapter's `experimental` flag clears, and
how releases gate on records are deviceside's rules (its `CLAUDE.md`); a change here that alters what a record
proves needs a matching change there.

**No household details in commits**, though this repo is private: entity IDs, device names, MACs, hostnames and
tokens stay in the gitignored `profiles/`, `.env.rig`, `.private-terms` and `CLAUDE.local.md`. The pre-commit
household check (`scripts/check_private.py`) enforces it.

**The rig moves the household's real lights and restarts its HA.** Drive it only through `scripts/rig.py`, behind its
gate; never answer the gate (`DEVICE_SIDE_RIG_APPROVED=1`) without Jason's yes for that run.

## Layout

- `scripts/rig.py`: the runner; `scripts/deploy.sh`: its deploy, through `.env.rig`'s commands
- `ha_rig/`: the engine, HA and Hue clients, locks, journal, baseline, roaming, run logs, profiles, the target
  checkout (`target.py`) and the household terms for deviceside's check (`terms.py`)
- `ha_rig/steps/`: one step file per action, named as deviceside's action modules are
- `tests/`: the rig's own tests, with fakes; they import deviceside's registry and actions from the target checkout

## Changes

- Setup: `uv sync`. Keep the `dev` group's pins in step with deviceside's `pyproject.toml`.
- Before each push: `.venv/bin/pytest` and `pre-commit run --all-files`.
- A new or renamed deviceside action needs its step file here, landed with or before the deviceside PR it tests.
- Async throughout; American English, matching HA.
