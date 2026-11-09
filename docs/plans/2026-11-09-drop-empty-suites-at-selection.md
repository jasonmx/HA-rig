# Drop a suite with no cases on its integration at selection

## Context

`select()` (`scripts/rig.py:224`) pairs every step file with every integration in the profile. Where every test
in a file has an `only:` that leaves an integration out, the suite has no cases on it. `lifecycle.yaml`
(`only: [hue]` on both tests) shows the problem: Shelly, ZHA, Z2M and Matter each get a `lifecycle` suite with no
cases.

The paths handle that suite differently:

- **The run** drops it: `left()` (`rig.py:998`) returns no case IDs, so `plan_coverage` leaves it out.
- **`--coverage`** keeps it: `coverage.of` gives it `planned == []` and no record, `SuiteCoverage.complete` needs
  `bool(self.planned)`, so it reports `uncovered (no record)`. Every profile shows it (`house`: shelly, matter, zha,
  zigbee2mqtt), so `--coverage` exits 1 forever and the pre-RC full-coverage view can never read clean.

**Decided with Jason:** filter at selection, so dry-run, `--coverage`, the run and the ledger all see the same set.

## Coordination

Intent: A suite whose every test is `only:` elsewhere is never selected on that integration, so `--coverage` stops
reporting `<integration> / lifecycle: uncovered (no record)` for integrations it never runs on.
Touches: ha_rig/engine.py, scripts/rig.py, tests/test_rig.py, tests/test_rig_engine.py, docs/rig.md

No deviceside change: deviceside has only `hue/lifecycle.md` and `check_release.py` expects no other. No
`Tests-with`.

## Steps

1. **`StepFile.runs_on(integration) -> bool`** (`ha_rig/engine.py`, next to `roles`): true when any test has
   `only is None` or the integration in `only`. That mirrors the test-level filter in `cases()`
   (`engine.py:708`). Make `cases()` use it as an early return, so the two can't drift apart. Step-level `only:` is
   not considered: a test whose steps all skip still plans a case.
2. **Filter in `select()`**: add `if step_file.runs_on(integration)`. Update its docstring. A suite with no adapter
   still stays, so the dry-run still says `no adapter, will skip`. That's a different case: the file runs there, the
   adapter is missing.
3. **Selection that becomes empty:** `--action lifecycle --integration shelly` now selects nothing, and
   `run_suites` raises `No rig test matches the profile's integrations.` Accept that, but check that the message
   still reads right. If it doesn't, reword it to cover a selection with no suites at all.
4. **docs/rig.md:** where the doc covers `only:` (or the lifecycle paragraph near line 256), add one sentence: a file
   with no test for an integration isn't selected there, and the record, dry-run and coverage don't show it.

## Tests

- `tests/test_rig_engine.py`: `runs_on` covers a file with no `only` (true everywhere), a file whose tests all say
  `only: [hue]` (true for hue only), and a mixed file (true for both).
- `tests/test_rig.py`, near line 923: `select(step_files, profile, None, ["lifecycle"])` yields only the hue suite
  on a profile with hue and shelly. `coverage.of` over that selection holds no `shelly / lifecycle`.
- The existing select and plan-line tests (`test_select_plan_lines_and_estimate`) still pass. If one counted the
  empty suites, fix the count. Don't loosen the assertion.

## Verification

- `.venv/bin/pytest` and `pre-commit run --all-files`.
- From the deviceside checkout: `../ha-rig/scripts/rig.py --profile house --coverage | rg lifecycle` prints only
  `hue / lifecycle`. That suite stays stale until the next Hue run, because #37 dropped its `switch_plug` cases.
- `--dry-run` on `shelly` shows no lifecycle line, as now.

## Out of scope

- `hue / lifecycle` being stale is real (#37 changed its cases). The next Hue lab run rewrites it.
