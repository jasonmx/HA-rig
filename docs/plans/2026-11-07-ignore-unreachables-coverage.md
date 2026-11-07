# Rig coverage: `--ignore-unreachables` and per-adapter coverage

## Context

Time on the full rig, with every device powered and the house free, is the pipeline's key constraint. Two things waste it
today:

1. **One powered-off device stops the whole run.** `check_devices` (`scripts/rig.py:2136`) and `wait_for_targets`
   (`scripts/rig.py:2015`) exit on any unavailable entity, so nothing gets tested until the whole rig is up.
2. **Coverage doesn't build up across runs.** Saved progress (`ha_rig/progress.py`) is keyed on the whole component
   tree plus the exact selection and branch, so it resets far more often than it needs to. It is also deleted when
   a run finishes (`end_code`, `scripts/rig.py:1772`), and every selected suite is rerun even if it already has a
   fresh record.

The aim is to treat the "full-suite run before an RC" as **coverage**: every test has a result, built up over
any number of runs. A test stays covered while the code it proves is unchanged, judged exactly as deviceside's release
gate judges it (`check_release.py --records`: per adapter, through `adapter_code`, with `core/` and `actions/` left
out, and HA's version ignored). Adapters whose code is locked down get tested early, on whatever devices happen to be
up, and the full-rig slot then runs only what is missing.

Decisions already taken with Jason:
- Flag: `--ignore-unreachables`. Read-only view: `--coverage`.
- Validity is per adapter, as the gate judges it. A recorded suite stays covered after an HA upgrade. A suite that
  is unfinished (some cases saved, no record yet) restarts on a new HA version, because a record names one version.
- A suite that is already covered is skipped when a run selects it; `--fresh` reruns it.
- Any failed case sends its whole suite back to uncovered, so a record never covers up an earlier failure.
- Roaming bulbs stay under the strict check. A bulb that passes `--check` counts as online on every network it can
  roam to, so the rig-made groups and scenes (all made of roaming bulbs) stay in scope.
- A fixed group is in scope when the profile lists its `members` and all of them are online. A group whose members
  aren't listed is skipped, and so is every fixed scene (the profile doesn't list what a scene contains).

## Design

### 1. Validity: a fingerprint per suite

`ha_rig/coverage.py` (new) computes each suite's **fingerprint** at the deployed commit:
- the code of every adapter its tests call on its integration: the suite's action plus `RigTest.calls`, filtered by
  `adapters_for` (`ha_rig/engine.py`). Each adapter's files come from deviceside's own
  `check_release.adapter_code`, imported from the target checkout's `scripts/` (append it to `sys.path` the way
  `scripts/rig.py:121` appends `REPO`), so the definition lives in one place;
- the blob ID of each of those files at the commit (`git rev-parse <commit>:<path>`), which is valid because a
  deploy refuses uncommitted changes under the component;
- the hash of the rig's step file (`suite.file.path`), so editing a test also makes its suite stale.

Saved cases also keep their `entity_ids`, which already exist. A saved case whose entities differ from the ones the
case now plans is stale (the profile changed).

### 2. The ledger: progress becomes per suite

Rework `ha_rig/progress.py`:
- **One file per profile** (`<profile>.json`), with entries keyed `(integration, suite, fingerprint)`. Runs from
  different worktrees with different code then sit side by side, and the branch drops out of the file name. Remove
  the run-level `Key`, which held the selection, keyword, serial and unattended settings.
- Each suite entry holds its HA version, its facts and its cases. When HA's version differs from an entry's, that
  entry is dropped. This replaces the run-wide `check_ha` reset.
- `save` re-reads the file and merges it under `fcntl.flock`, since two runs on different integrations can now
  write the same file.
- When a suite's record is written, its entry is dropped, because the record now carries its coverage. Entries
  untouched for 30 days are pruned on load.
- When a case fails, its suite's entry is dropped once the run ends (the run report keeps the failure). The suite
  then reruns in full.
- `end_code` stops calling `discard()`; the ledger lives on.

### 3. Coverage: records plus the ledger

A suite is **covered** when either:
- it has a **fresh record**: `docs/test-records/<integration>/<suite.name>.md` cites a commit at which the suite's
  fingerprint code is unchanged (`git diff --name-only <cited> <deployed> -- <files>` is empty, as the gate does it),
  and the record's case IDs (from its Tests table) are exactly the IDs the suite plans now. A changed step file
  changes the IDs, or else its hash shows the change; or
- its ledger entry at the current fingerprint holds a result for every case it plans.

The resume path in `scripts/rig.py` (`resume`, `left`, line 903) is replaced by a coverage plan. Selected suites that
are covered are left out and listed as `covered (record <date>)`. The rest run only their cases that have no valid
saved result. `--fresh` ignores both records and the ledger for the selected suites. Selection (integrations,
actions, keyword, serial) only decides what runs. `merge` (`ha_rig/engine.py:2140`) stops depending on the keyword:
a suite is complete when every planned case has a result.

`--unattended`: a saved `not observed` result counts as done, as records allow it today. No change to what an
attended run does.

### 4. `--ignore-unreachables`

- `check_fixed` (`scripts/rig.py:2116`): with the flag, an `unavailable` fixed entity goes into an `offline` set
  instead of `problems`. "Not in HA" and wrong-platform entities still stop the run, and so does an offline roaming
  bulb (`check_roaming` is unchanged).
- Add `offline: frozenset[str] = frozenset()` to `Profile` (`ha_rig/profile.py:131`) and set it after the check. Then
  `cases()` (`ha_rig/engine.py:636`) marks a case `skip=...` with one of these reasons:
  - one of its entities, members or supplies (`profile.power()`) is offline: `"<entity> unreachable"`;
  - when `offline` is non-empty and the case targets a fixed group-role entity (`GROUP_ROLES`) whose members the
    profile doesn't list: `"group: members not listed"`;
  - when `offline` is non-empty and the case targets a fixed `scene` (one not in `profile.made_entities`):
    `"scene: contents unknown"`.

  Putting it on `Profile` reaches every `cases()` call site (`targets`, `platforms`, the plan printout, `run`,
  `merge`) with no further plumbing.
- These skips are **unreachable skips**, kept apart from permanent profile skips (`no X entity in the profile`).
  Mark them on `Case` with `unreachable: bool`, carried into `CaseResult`. They are never saved to the ledger, and a
  suite holding one is incomplete, so it writes no record. The run summary and report show the suite as
  `partial: N of M cases; K unreachable`.
- `wait_for_targets` needs no change. Offline entities are already gone from `targets()`, and an entity that was
  online at the check and drops out after the restart still stops the run.

### 5. Device checks look only at what will run

`targets()` and `platforms()` (`scripts/rig.py:2049`, `2066`) also apply the coverage plan's done-case filter
(`unfinished`, `ha_rig/engine.py:1956`). Then `check_devices` and `wait_for_targets` require only the devices of
the cases still to run, so a completion run needs just the devices that were off before. This applies to every run,
with or without the flag.

### 6. `--coverage`

A read-only command in the shape of `--check` (`scripts/rig.py:1179`): no lock and no deploy. It works off the local
HEAD commit, not what HA runs. For each selected suite it prints one of:
- `covered (record <date>, commit <c>)`;
- `partial N/M` with the missing case IDs and the entities they need;
- `uncovered (stale: <changed files>)`, or `uncovered (no record)`.

It ends with a total and the set of devices the missing cases need, which is what to power up for the next run.
With no selection it covers the whole profile, which is the RC view.

## Files

- `ha_rig/coverage.py` (new): the fingerprint, the fresh-record check, the coverage plan.
- `ha_rig/progress.py`: the per-suite ledger, file locking, pruning.
- `ha_rig/engine.py`: `cases()` unreachable skips, `Case`/`CaseResult.unreachable`, `merge` completeness,
  `SuiteResult.recordable` and the report's partial label.
- `ha_rig/profile.py`: `Profile.offline`.
- `scripts/rig.py`: the flags, `check_fixed`, `targets`/`platforms` filtering, replacing `resume`, `end_code`, the
  `--coverage` command, the usage docstring.
- `docs/rig.md`: Before a run, the new flag, coverage and resume.
- `CLAUDE.local.md`: the RC rule reads "full coverage (`--coverage` with no selection shows every suite covered)",
  not "a full-suite run".
- Deviceside: no change. What a record proves is unchanged under the gate's own rule. A record's cases may have run
  across sessions at different commits whose fingerprinted code is identical. The record cites the last of those
  commits, which the gate accepts. Say so in the PR body.

## Steps

0. Copy this plan to `docs/plans/2026-11-07-ignore-unreachables-coverage.md` in a worktree `wt/coverage` off `main`.
1. `coverage.py` fingerprint and fresh-record check, with tests against a temporary git repo holding a fake adapter
   package and records (the pattern in `tests/test_target.py`).
2. Ledger rework in `progress.py`, with its tests (`tests/test_progress.py`): per-suite entries, HA drop, failed-suite
   drop, merge under lock, prune.
3. Coverage plan replacing `resume`/`left`, and `merge` completeness; tests in `tests/test_rig.py`.
4. `Profile.offline` and the skip rules in `cases()`; tests in `tests/test_rig_engine.py` and `tests/test_rig_groups.py`
   (listed members all online, one member offline, unlisted members, fixed scene, rig-made group or scene still in
   scope, supply offline).
5. `--ignore-unreachables` in `check_fixed`/`check_devices`, and the done-case filter in `targets`/`platforms`; tests
   with the fakes in `tests/rig_fakes.py`.
6. `--coverage` command and its output.
7. Docs (`docs/rig.md`, the usage docstring, `CLAUDE.local.md`).
8. `.venv/bin/pytest`, `pre-commit run --all-files`, `/audit-code` on the diff, PR, `merge-green <n> --squash
   --delete-branch`.

## Verification

- Unit tests, as in the steps. Key cases:
  - an unreachable skip never writes a record, and is never saved;
  - an unreachable-only run followed by a completion run (devices back) writes the record, running only the missing
    cases and checking only their devices;
  - an adapter-code change stales one integration's suites and not another's;
  - a `core/` change stales nothing, as the gate decides;
  - an HA version change drops partial entries and keeps record coverage;
  - a failure reruns the whole suite;
  - a covered suite is skipped, and `--fresh` reruns it.
- `scripts/rig.py --coverage` against the real profile (read-only, no gate). The suites with fresh records in
  deviceside should show as covered.
- `scripts/rig.py --dry-run --ignore-unreachables` with one device powered off: the plan lists that device's cases and
  the unlisted groups and fixed scenes as skipped, and the gate isn't reached.
- A real `--ignore-unreachables` run, then a completion run, **only with Jason's yes for each run** through the
  authorization gate.
