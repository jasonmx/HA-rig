# Stage test records; promote only a clean run's (issue #41)

## Context

`report()` (`scripts/rig.py`) writes each fully passed suite's record straight into the target checkout's
`docs/test-records`, whatever the rest of the run does, and drops the suite from the ledger. A run that then fails,
is stopped, has its logs flagged, or dies leaves those files modified in a worktree nobody owns (2026-11-07: 14 records
stranded in `deviceside-wt-lab-records`).

## Decisions

- **Where staged records live:** `<report>.records/` next to the run's report in `REPORT_DIR`, laid out as
  `docs/test-records` (`<integration>/<suite>.md`), with a `staged.json` manifest: the target checkout, the ledger's
  directory and profile, and per record its ledger entry key and the hash of the target file at staging time.
- **Clean and publishable:** the run's final exit code is 0 (bulbs home, no failure, not stopped, logs not flagged)
  and every case it planned has a result: with the ledger, no selected suite has a case `left()` (the keyword's); with
  no ledger (`--no-deploy`), no unreachable skip. Then the run promotes its staged records itself.
- **Promote:** copy each staged record into the checkout's `docs/test-records`, then drop its suite's ledger entry
  (by the key in the manifest), then remove the staging dir. A record whose target changed since it was staged (a
  later run, or another promote) is left staged and named, never overwritten. Any other end prints one command:
  `DEVICE_SIDE_REPO=<checkout> <rig>/scripts/rig.py --promote <dir>`.
- **Ledger:** a staged record no longer drops its suite from the ledger; promotion does. So a suite finished in an
  unclean run stays covered by saved cases with no record, and the next run that ends cleanly stages and promotes
  its record from the ledger too (`pending`: complete in the ledger, no fresh record), even though it ran no case of
  it. The staged dir is a convenience; the ledger is what keeps the result, so a cleared temp folder loses nothing.
- `refuse_moved_files`, resume and the ledger's other drops are unchanged.

## Coordination

Intent: A run that doesn't end cleanly leaves the target checkout's docs/test-records untouched: records are staged
next to the run's report and promoted only by a clean run, or by the one `--promote` command it prints.
Touches: scripts/rig.py, ha_rig/staging.py, ha_rig/engine.py, ha_rig/progress.py, tests/test_rig.py,
tests/test_staging.py, docs/rig.md, README.md

## Tests

- Staging and promote (`tests/test_staging.py`): promote copies, drops the ledger entry, removes the dir; a target
  changed since staging is kept staged; the command names the checkout.
- `tests/test_rig.py`: a run with a failure stages and prints the command, leaving `docs/test-records` untouched; a
  budget-stopped run stages, and the resumed clean run promotes both suites; an unreachable run promotes nothing.
