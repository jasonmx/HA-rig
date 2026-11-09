# Ownership check follow-ups (issue #45)

## Context

PR #43 added `target.refuse_unowned_checkout`, which refuses a rig run unless the deviceside checkout is a linked
worktree on a `wt/*` branch with an open PR. `wait_or_run` (`scripts/rig.py:888`) calls it first, for every mode.
Issue #45 lists three follow-ups: stop it blocking modes that only look, fix the `docs/rig.md` example (it runs the
rig from the main checkout, which the check now refuses), and add a test that the check really runs.

## Finding: `--recover` is safe to exempt

`recover_all` (`scripts/rig.py:1484`) never writes to the checkout. It uses `REPO` only through
`git("rev-parse", "--abbrev-ref", "HEAD")`, a read that labels its lock. `recover_rig` and `prune_baseline` take only
`env` and the journal or baseline in the git common dir, and neither references `REPO`. It does move devices, but
the ownership rule is about whose code a run deploys and records, and `--recover` does neither. `--check` deploys
nothing and records nothing. `--dry-run` stops before the gate.

## Changes

1. `scripts/rig.py`, `wait_or_run`: call the check only when the run is not read-only:
   `if not (args.check or args.dry_run or args.recover): target.refuse_unowned_checkout(REPO)`.
   Leave `terms.write(REPO)` where it is (it writes to deviceside's main checkout in every mode, on purpose).
   Every other mode keeps the check: `--promote` (writes records into a checkout), `--deploy-only`, `--wait-free`,
   `--coverage`, `--roam-check`, `-- CMD`, and a real run. Their exemption is out of scope for this issue.
2. `docs/rig.md`, Setup: make the example a worktree path (`cd ~/src/deviceside-wt/my-change   # a wt/* worktree
   with an open PR`) and say in one sentence that `--check`, `--dry-run` and `--recover` skip the ownership check, so
   they work from any checkout, while `HA_RIG_UNOWNED_CHECKOUT=1` opts the rest out. No README change: no option
   changes.
3. `ha_rig/target.py`: extend the `refuse_unowned_checkout` docstring to say the read-only modes skip it.
4. `tests/test_rig.py`: new tests, driven through `rig.main([...])`, not `refuse_unowned_checkout` itself.
   - Writing modes (parametrized: a plain run, `--deploy-only`, `--promote DIR`, `--roam-check`): swap
     `target.refuse_unowned_checkout` for a recorder that raises a sentinel `SystemExit`, and assert it was called.
   - Exempt modes (`--check`, `--dry-run`, `--recover`): record the calls, make `target.refuse_moved_files` (the
     next line in `wait_or_run`) raise a stop sentinel, and assert the recorder was never called.
   - What it catches that nothing else does: `tests/test_target.py` unit-tests the check, but the autouse
     `no_ownership_check` fixture (`tests/conftest.py:78`) stubs it everywhere else, so deleting or moving the call
     in `wait_or_run` passes every test today.
   - Argument combinations that `main` rejects before `wait_or_run` (it may need a profile) are the implementer's
     to adjust; keep one case per mode.

## Coordination

Intent: skip the ownership check for `--check`, `--dry-run` and `--recover`, fix the Setup example, and assert
`wait_or_run` runs it for every other mode. Fixes #45.
Touches: scripts/rig.py, ha_rig/target.py, docs/rig.md, tests/test_rig.py

## Steps

0. Planning session: create worktree `wt/ownership-followups` off `main`, copy this plan to
   `docs/plans/2026-11-09-ownership-check-followups.md`, commit and push it there (repo `CLAUDE.md`, Changes).
1. Implementer, in that worktree: `pr-draft docs/plans/2026-11-09-ownership-check-followups.md`.
2. Make changes 1 to 4.
3. `.venv/bin/pytest`, then `pre-commit run --all-files`.
4. In the turn that marks ready: `audit-gate brief pr`, spawn its Agent, `audit-gate record`, fix findings,
   `gh pr ready`, then `merge-green <n> --squash --delete-branch` in the background.

Model for the implementer: **sonnet**. The plan settles the design, and the tests catch a wrong call. Nothing here
moves a device: never run `scripts/rig.py` for real. `--dry-run` is the only invocation allowed, and the tests cover
the rest.

## Verification

- New tests fail with the `wait_or_run` change reverted (exempt-mode cases) and with the call deleted (writing-mode
  cases), and pass with it.
- `.venv/bin/pytest` and `pre-commit run --all-files` are clean.
- `scripts/rig.py --dry-run` from the main checkout no longer says "not a linked worktree".
