# Deploy-only runs (#5) and stop/resume (#6)

## Context

Two open ha-rig issues:

- **#5 Deploy-only run.** Deploy a deviceside branch to HA to try non-device features by hand, with no tests, no
  profile, and locks released as soon as HA is back.
- **#6 Stop and resume.** Today a run that stops part way (Ctrl-C, SIGTERM, the blocker) keeps nothing: a 45-minute
  suite only completes in a window of 45+ minutes. A run should stop at any time, put everything back as it already
  does, and on rerun pick up at the first unfinished case, so a 45-minute suite completes as 30 minutes plus 15.

Decisions taken in the interview:

| Question | Decision |
|---|---|
| How a rerun resumes | **Automatically**, when the key matches; `--fresh` discards progress. A mismatch starts fresh and says why. |
| Stopping for a window | **`--for DURATION`** budget: once spent, start no new case, let in-flight ones finish, put back, exit. Ctrl-C, SIGTERM and the blocker still abort at once (in-flight cases cancelled, redone on resume). |
| Finished failures on resume | **Kept.** Finished means finished; the resumed run reports them and ends failed. `--fresh` retries. |
| Deploy-only gate | **None.** No y/N, no blocker: it moves no device. |

Step 0 of the implementing session: copy this plan to `docs/plans/2026-11-06-deploy-only-and-resume.md` in the
worktree (`wt/deploy-only-resume`) and commit it with step 1.

## Step 1: `--deploy-only` (#5), one PR

`scripts/rig.py`:

- New flag `--deploy-only` ("deploy this checkout's branch and restart HA, then release the locks: no profile, no
  tests, no gate"). `parser.error` when combined with `--no-deploy`, `--check`, `--roam-check`, `--recover`,
  `--wait-free`, a `--hold` command, `--integration`, `--action`, `-k`, `--serial`, `--unattended`,
  `--debug-logs`, `--for`, `--fresh`. `--profile` is an error too (it needs none). `--dry-run` is allowed: it
  prints the deploy state and stops.
- `wait_or_run` routes it to a new `deploy_only(args, directory) -> int`, before `prepare` (which loads a profile):
  1. `load_env()`; `HA_URL` and `HA_TOKEN` required (for `wait_until_ready`).
  2. `deploy_state(env["RIG_STATUS_CMD"])`, print the `Integration: local/synced/loaded` line (factor it out of
     `print_plan` into `print_deploy(deploy, deploying)` so both use it).
  3. Not `needed` → `HA already runs this checkout's tree; nothing to deploy.`, exit 0, no lock.
  4. `check_deploy(env, deploy)` (uncommitted / unpushed blockers as today). Dry run stops here.
  5. `locks.hold(Claim("exclusive", frozenset()), holder, directory)`, re-read `deploy_state` once held (as `take`
     does), return 0 if another run deployed this tree meanwhile.
  6. Under `end_on_signals()`: `deploy_and_wait(env, deploy.branch)` (its `finish_first` already holds a signal until
     HA is back). No journal, no baseline, no replay of orphans (it holds no integration, so `replay` could only
     print; leave that to the next test run).
  7. `held.close()` in `finally`; print `Deployed <branch> (<short commit>); HA is back. Locks released.`
- Usage block in the module docstring: add `scripts/rig.py --deploy-only [--dry-run]`, and a sentence on it.

Docs: `docs/rig.md` Deploys section gets a paragraph on `--deploy-only`. `CLAUDE.md`'s gate line ("Drive it only
through `scripts/rig.py`, behind its gate") gets "(`--deploy-only` has none: it restarts HA but moves no device)".

Tests (`tests/test_rig.py`, with the existing fakes for `deploy_state`/`deploy_and_wait`/`locks`): flag conflicts;
not-needed exits 0 without a lock; needed takes HA exclusively with no integrations, deploys, releases; a deploy that
becomes unneeded after the wait skips the deploy; no profile is ever loaded (empty `PROFILES_DIR` passes).

## Step 2: progress store (#6)

New module `ha_rig/progress.py`, modelled on `ha_rig/journal.py` (atomic JSON writes in deviceside's git common dir):

- File: `<common_dir>/deviceside-rig-progress/<profile>--<branch, slashes to _>.json`. One per profile and branch,
  so a worktree's runs don't collide and a mismatch can say why.
- `Key`: profile name, deviceside **local tree** (`Deploy.local_tree`), and the selection: sorted `--integration`,
  sorted `--action`, `-k`, `--serial`, `--unattended`. `--no-deploy` runs (no record possible) get no progress.
- Contents: `key`, `ha_version` (first one seen), `started` (ISO), and `cases`: list of
  `{integration, suite (Suite.name), id, role, domain, outcome, detail, entity_ids, twin}`, plus `facts` per
  `(integration, suite)`: `ha_version` and `devices`.
- API: `Progress.load(directory, profile, branch, key, out)` returns an empty store, or the saved one when the key
  matches, or prints why it starts fresh (`tree changed`, `selection changed`) and discards it.
  `record(suite, CaseResult)`, `facts(suite, ha_version, devices)`, `done() -> dict[(integration, suite, id), CaseResult]`,
  `parts(suites) -> list[SuiteResult]` (saved results as `complete=False` parts for `merge`), `discard()`.
- HA version: once connected, if the saved `ha_version` differs from HA's, discard and say so: a record must name
  one HA version.

## Step 3: the engine skips finished cases

`ha_rig/engine.py`:

- `run(..., done: dict | None = None, on_result: Callable[[Suite, CaseResult], None] | None = None,
  budget: Budget | None = None, on_facts=...)`.
- Finished cases are excluded through the existing `only` filter (compose `only` with "not in `done`" in `run` and
  `_plan`), so `merge` joins the saved parts with the new ones exactly as it joins lanes with phases.
- Seed `_Run.results` with `done` entries for the twin lookup in `_Run.automated` (an observed case whose automated
  twin failed in an earlier session is still skipped). Seed keys must use the run's suite identity: map by
  `(integration, suite.name)` to the planned `Suite`.
- `_Run.report` calls `on_result` after storing, so each case is saved the moment it finishes. A cancelled case is
  never reported, so it reruns.
- Facts: move the `_capture_facts` loop into the `finally`, so a part stopped by a signal or the budget still
  captures its devices while its bulbs are on that network (the `SuiteResult` comment explains why it must be
  then), and pass each to `on_facts`. Guard it like the options restore: a failure there must not hide the one
  that ended the run.
- `Budget` (small dataclass: `deadline: float`, `spent` property on `time.monotonic()`): `_Run.automated` and
  `_Run.observed` check it before starting each case and stop starting new ones once spent; in-flight lane cases
  finish. Budget-stopped cases get no result (not "skipped"): they are unfinished.

## Step 4: the runner resumes and stops on budget

`scripts/rig.py`:

- Flags: `--for DURATION` (`30m`, `90s`, `1h15m`; parse in a helper with a test) and `--fresh`. Both only for a test
  run (errors with `--check`, `--roam-check`, `--recover`, `--wait-free`, `--hold`, `--deploy-only`). `--for` with
  `--no-deploy` is fine; `--fresh` with it is pointless but harmless.
- `prepare`: after `deploy_state`, load `Progress` (unless `--no-deploy`), `--fresh` discards first. Compute
  `remaining` = suites with at least one runnable, unskipped case not in `done`. If none remain (a finished run whose
  file wasn't deleted), discard and start fresh.
- Everything that plans the run uses `remaining`, not `suites`: `roams`, `makes_groups`, `phases`, `claim`
  (so a resume holds and moves only what it still needs; a resume with no Z2M case left doesn't roam there),
  `check_tokens`, `check_devices`, `estimate`, and `print_plan`, which uses the existing `only` hook to show only
  unfinished cases and adds `Resuming: N of M cases done (started <date>); --fresh to start over.`
- `estimate` scales each suite's `rig_seconds` by its unfinished share of cases.
- `run_rig` passes `done`, `on_result=progress.record`, `on_facts=progress.facts` and the budget into
  `run_phases` → `run`. `run_phases` checks `budget.spent` before each phase and stops without moving the bulbs
  further; `leave_lab` already brings them home.
- `report`: merge `progress.parts(...)` (saved results of every selected suite) with this session's parts, so a
  suite completed across sessions is recordable; records still need the commit (`tested_commit`), which is the same
  tree by the key. A record's Date is the finishing session's date.
- End of run: if every planned case now has a result, `progress.discard()` after the report and records. Otherwise
  print `Stopped with N cases left (budget spent | aborted); rerun the same command to resume.` and exit
  **75** (EX_TEMPFAIL: incomplete, resumable), whether stopped by budget or signal. A stopped run's unexpected
  DeviceSide log lines still fail it (exit 1 wins over 75). The report still writes for every session.
- Signals: no change to `cancel_on_signals`; the saved progress is already on disk case by case. A blocker abort
  behaves like SIGTERM, so it resumes the same way.

Docs: `docs/rig.md` gets a "Stopping and resuming" section under Shared state (store, key, `--for`, `--fresh`, exit
75, failures kept), the module docstring and usage block in `scripts/rig.py` the same in brief. No deviceside
change: a record still proves one tree, one profile, one HA version, every case passing; only the session count
changes. Say so in the PR body.

## Tests

- `tests/test_progress.py` (new): round-trip; key mismatch on tree / selection discards with the reason; HA
  version mismatch discards; `parts` rebuilds `SuiteResult`s `merge` accepts; branch with slashes.
- `tests/test_rig_engine.py`: `run` with `done` skips those cases and reports the rest; an observed case whose
  saved twin failed is skipped; `on_result` fires per case; a cancelled run still captures facts and never reports
  the cancelled case; `Budget` spent before a case stops new cases while a lane's in-flight case finishes.
- `tests/test_rig_lanes.py` / `test_rig_roam_run.py`: a resume with only one network's cases left visits only it;
  budget spent between phases skips the later moves and the bulbs go home.
- `tests/test_rig.py`: `--for` parsing and flag conflicts; `claim` and `estimate` shrink with progress; a
  two-session run (first stopped by budget, second resumes) writes the record once, and the progress file is gone
  after; exit 75 on a stopped run.

## Verification

1. `.venv/bin/pytest` and `pre-commit run --all-files` in the worktree, after each step.
2. Rig runs (each needs Jason's yes; run from the deviceside checkout, in the background):
   - `--deploy-only --dry-run`, then `--deploy-only` on a branch HA hasn't loaded: HA restarts, locks free right
     after (`scripts/rig.py --wait-free` returns at once).
   - `--action turn_on_for --for 1m` on a suite longer than a minute: stops, exit 75, devices and pauses back;
     the rerun prints `Resuming`, runs only the rest, writes the record, deletes the progress file.
   - Same, stopped with Ctrl-C mid-case: the cancelled case reruns on resume.
   - A roaming profile stopped by `--for` after the lanes: the resume visits only networks with cases left.
