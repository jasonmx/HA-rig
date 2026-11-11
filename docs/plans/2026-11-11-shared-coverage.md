# Records carry the run's `core/` coverage (#64)

## Coordination

Intent: An opt-in `--shared-coverage` run measures DeviceSide's `core/` lines in the live HA process through a small
rig-only helper component, and writes deviceside's `## Shared coverage` section into each record it writes.
Touches: ha_component/rig_coverage/**, ha_rig/shared_coverage.py, ha_rig/engine.py, scripts/rig.py, ha_rig/env.example, docs/rig.md, README.md, tests/**

## Context

DeviceSide/DeviceSide#377 lets one passing Verified record clear a `core/` change for every adapter, if the record
carries `## Shared coverage` (format and rules: deviceside `scripts/check_release.py` docstring, "Shared code";
parser `shared_rows`). The rig writes none, so every `core/` change still stales every adapter's records.

The issue proposed coverage from HA's startup (`.pth`, `COVERAGE_PROCESS_START`, fetch the data file). That's
unnecessary: deviceside's `needed_headers` never clears a change to a module's or class's own statements, and a
function's header needs its first body statement, so import-time lines prove nothing either way. Coverage can start
mid-process, with no restart and no file to fetch.

## Decisions (settled with Jason)

- **Mechanism:** a rig-only custom component `rig_coverage`, kept in this repo. Admin services
  `rig_coverage.start` (erase, then start a `coverage.Coverage(source=[<config>/custom_components/deviceside/core],
  data_file=None)` with the sysmon core) and `rig_coverage.stop` (stop it and return `{abs_path: [lines]}` as service
  response data). `start` while running restarts it from empty, `stop` while idle returns `{}`. Manifest pins
  `coverage` in `requirements`, so HA installs it. No restart, no data file.
- **Deploy:** Jason extends `RIG_SYNC_CMD` (his out-of-repo tooling) to copy `ha_component/rig_coverage/` too, and
  adds `rig_coverage:` to HA's `configuration.yaml` once. The rig doesn't deploy it.
- **Concurrency:** opt-in `--shared-coverage` takes HA **exclusively** for the run (so no other run's `core/` lines
  mix in) and needs `HA_ADMIN_TOKEN`. Default runs are unchanged and write no section.
- **No spike:** the sysmon core catching tasks started before `start()` is checked by the first real run
  (Verification), not up front.

## Decisions taken here (push back at review if wrong)

- **One bracket per run, not per suite.** Suites' cases interleave (lanes, phases in `engine.run`), so per-suite
  brackets don't fit. Every record the run writes gets the run's lines, **only if every suite in the run with a
  finished case passed**; otherwise no record gets a section (a failing suite's lines must not count as passing
  proof). Say so in the run summary.
- **Undercount is safe.** A suite whose record also takes saved cases from earlier runs carries only this run's lines;
  it never claims more than it ran.
- **Missing or empty → no section**: helper absent (service not found), `start` failed, `stop` failed or returned no
  `core/` lines. Print why; the record is still written without the section. Never an empty table.
- `stop` runs in a `finally` around the run's cases, so an aborted run doesn't leave tracing on.

## Steps

1. `pr-draft` this plan in the worktree (`wt/shared-coverage`).
2. **Helper component** `ha_component/rig_coverage/`: `manifest.json` (domain, name, version, `requirements:
   ["coverage==<current 7.x>"]`, `iot_class: local_push`), `__init__.py` with `async_setup` registering the two
   services via `async_register_admin_service`, `stop` with `SupportsResponse.ONLY`. Run start/stop in the executor
   only if coverage needs it; keep it under ~60 lines.
3. **`ha_rig/shared_coverage.py`**: `rows(executed: dict[str, list[int]]) -> list[tuple[str, str]]` mapping each path
   with a `custom_components/deviceside/core/` suffix to `core/<module>.py` (others dropped), lines to
   `12-40, 55` ranges, sorted by module, modules with no lines dropped; `section(rows) -> list[str]` rendering
   deviceside's heading, header and rows, `[]` for no rows. `start(admin)` / `stop(admin)` wrappers over
   `HAClient.call_service(..., return_response=True)` (`ha_rig/client.py:230`), returning None with a reason on
   failure.
4. **Engine**: `render_record` (`ha_rig/engine.py`) takes optional rows and appends the section after `## Tests` and
   its notes. `write_records` takes them and passes them on. `engine.run` brackets the cases with start/stop when asked
   and applies the all-passed rule.
5. **`scripts/rig.py`**: `--shared-coverage` (exclusive HA claim via `ha_rig/locks.py`, refuses without
   `HA_ADMIN_TOKEN`, refuses with `--no-deploy`'s no-record mode). Regenerate README's `--help` block
   (`COLUMNS=100 scripts/rig.py --help`).
6. **Docs**: `docs/rig.md` "Test records" gains the section, the flag, the exclusive lock and the all-passed rule.
   `ha_rig/env.example` says `RIG_SYNC_CMD` also copies `ha_component/rig_coverage/`, and names the one-time
   `configuration.yaml` line.
7. **Tests** (`tests/`, fakes in `tests/rig_fakes.py`):
   - `rows`: path mapping, non-`core/` paths dropped, range collapsing (single line, runs, gaps), sort order.
   - Rendered record parsed by deviceside's own `shared_rows`, imported from the target checkout's
     `scripts/check_release.py` the way the tests already reach its registry: no errors, the same lines back.
   - Empty or missing executed lines, failed start/stop, and a run with a failed suite each write the record with no
     section.
   - The flag takes HA exclusively and requires the admin token.
8. `.venv/bin/pytest`, `pre-commit run --all-files`, `/audit-code`, then `gh pr ready` and `merge-green <n> --squash
   --delete-branch`.

Follow-up outside this PR: a deviceside PR ticking TODO "Shared code proof" once the first record with a section lands.

## Verification

- Unit tests above, including the round-trip through deviceside's `shared_rows`.
- Once Jason has extended `RIG_SYNC_CMD` and restarted: one attended `--shared-coverage` run of a single small suite
  (behind the authorization gate). Check the staged record has a section, that it includes lines of a coroutine that
  was already running before `start()` (the dispatcher loop, if there is one; otherwise note that sysmon missed it and
  fall back to listing that gap in `docs/rig.md`), and that `check_release.py --records` in deviceside accepts it.
