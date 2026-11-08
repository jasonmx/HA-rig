# Option step files apply only where the adapter has the option

## Context

`scripts/rig.py --coverage`, run from deviceside at e0b8ea9, lists `lower_brightness-predicted_state`,
`raise_brightness-predicted_state` and `stop-predicted_state` as "uncovered (no record)" for zha, zigbee2mqtt and
shelly. Only Hue's fade and stop adapters declare `options=frozenset({"predicted_state"})`, so full-suite coverage
(the RC gate) can never reach 100%.

**Cause.** The engine's `_plan` (`ha_rig/engine.py:2026`) is the only code that knows an option file doesn't apply to
an adapter without that option. It skips the file at run time (`no options, … skipped`). Everything that plans before
it treats "an adapter exists" as "the suite applies":

- `coverage.of` (`ha_rig/coverage.py:220`): it counts the suite, so the RC view can never be complete.
- `runnable_suite` (`scripts/rig.py:1036`), and through it `left`, `runnable`, `reloads`, `claim`, `print_resume`
  and `print_unreachable`. The suite has "cases left", so it stays in `setup.suites`. `reloads()` reads
  `file.reloads` (it has options), so **a Shelly-only run takes HA exclusively** for an entry reload that never happens.
- `print_phases` (`:286`), `lane_order` (`:330`), `run_suites` (`:1473`), the targets set (`:2235`) and
  `platforms` (`:2252`): the dry-run lists the cases, the estimate counts their `rig_seconds`, and the device checks
  cover them.

**The 2026-11-08 Shelly runs** (`$TMPDIR/deviceside-rig/rig-run-2026-11-08-111839/111915/111942.md`) have tables only
for the plain `raise_brightness`, `lower_brightness` and `stop` suites. The predicted_state cases did not run (`_plan`
skipped them) and wrote no record, so no bad record needs removing. The cost was the exclusive HA hold, a padded
estimate and coverage that can't complete.

**Decisions (Jason, 2026-11-08):**
- A step file that doesn't apply to an integration is **dropped silently at selection**: not listed in the plan, not
  counted in coverage. It appears by itself once deviceside gives that adapter the option.
- An option is a **typo (SchemaError) only when no adapter of that action, on any integration, declares it**
  (`registry.matrix()`). That is checked once at selection, before the gate. It replaces the run-time rule "this
  adapter has options, but not this one".

## Coordination

Intent: Option step files apply only to adapters whose scope declares every option they set. Selection, coverage,
locks and the plan all agree with the engine.
Touches: ha_rig/engine.py, ha_rig/coverage.py, scripts/rig.py, tests/rig_fakes.py, tests/test_rig.py,
tests/test_rig_engine.py, tests/test_rig_coverage.py

## Steps

1. **The predicate, in `ha_rig/engine.py`** next to `adapters_for`:
   - `applies(suite, registry) -> bool`: the adapter exists and `suite.file.options <= adapter.scope.options`. A file
     with no options applies wherever there is an adapter, which is today's behavior.
   - `check_options(step_files, registry)`: raises `SchemaError(f"{path.name}: no {action} adapter has option
     {name}")` for an option that no `(action, _, adapter)` in `registry.matrix()` with a non-None adapter has in
     `adapter.scope.options`. Use the file's `action`, not its `name`.
   - In `_plan`, replace the two option branches (`no options, … skipped` and the per-adapter SchemaError) with
     `if not applies(suite, registry): continue`. That is defense in depth for engine-level callers and tests. The
     `no adapter, skipped` line stays.

2. **Selection, in `scripts/rig.py` `run_suites`:** call `check_options(load_step_files(), registry)` once. After
   `select(...)`, keep a suite if `registry.adapter(...) is None` (so the dry-run still says `no adapter, will skip`)
   or `applies(suite, registry)`. Every downstream consumer (coverage, `left`, `reloads`/`claim`, `estimate`, lanes,
   targets, platforms, `check_tokens`) then sees only suites that apply. `coverage_only` reaches coverage through
   `run_suites`, so it is fixed by this too.

3. **`ha_rig/coverage.py` `of`:** change the guard at :220 to `if not applies(suite, registry): continue`, so a
   caller that doesn't go through `run_suites` still can't count such a suite. Update the docstring ("for those with
   an adapter whose scope has the file's options").

4. **Tests fake, `tests/rig_fakes.py` `Roster`:** add `matrix()`, which yields only the declared pairs, so the typo
   check sees the same adapters a test declared. Without it, `__getattr__` would pass the real, undeclared matrix
   through.

5. **Tests:**
   - `tests/test_rig_engine.py`: rewrite `test_an_option_file_skips_an_adapter_without_the_option` so it asserts
     `result.suites == []` and no line, now that the skip is silent. Add unit tests for `applies`: a Hue
     predicted_state file applies on hue and not on zigbee2mqtt, shelly or zha; a plain file applies wherever there
     is an adapter. Add `check_options` tests: `predicted_state` passes, `predicted_stat` raises naming the file.
   - `tests/test_rig.py`: `run_suites` over a profile with hue and shelly keeps `stop-predicted_state` for hue
     only. A test that `claim()` stays `shared` for a shelly-only selection that includes the predicted_state files.
     A dry-run plan test showing no `-predicted_state` line for shelly.
   - `tests/test_rig_coverage.py` (or wherever `coverage.of` is tested): `of` omits a suite whose file options the
     adapter lacks, and `coverage_lines` totals exclude it.
   - Use `Roster` with real deviceside adapters (Hue fade/stop have predicted_state, Shelly's don't), not hand-built
     scopes, so the tests follow deviceside's real scopes.

6. **Docs and comments:** `ha_rig/engine.py`'s module docstring (lines 4-5) and `scripts/rig.py`'s `select`
   docstring: an option file runs only on adapters whose scope has its options. `--help` output is unchanged, so the
   README is untouched.

## Verification

- `.venv/bin/pytest` and `pre-commit run --all-files`.
- From `../deviceside` at e0b8ea9 or later: `../ha-rig/scripts/rig.py --coverage`. No `*-predicted_state` line for
  zha, zigbee2mqtt or shelly. Hue's are still listed. The suite total drops by 9.
- `../ha-rig/scripts/rig.py --profile shelly_bulb --dry-run`: no predicted_state cases, `Locks:` shows `shared`
  (not exclusive), and a lower estimate. Read-only; no rig run is needed.
- This doesn't change what a record proves (deviceside's rules), so no deviceside change goes with it.
