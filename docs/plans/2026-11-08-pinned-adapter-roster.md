# Rig tests: a pinned adapter roster over deviceside's registry

## Context

deviceside #287 added `ShellySetAttributes` to Shelly's adapters, and six tests in `tests/test_rig_engine.py` broke.
Every ha-rig PR has failed CI since. Neither deviceside nor the engine is wrong:

- `Snapshot.deviceside` (`ha_rig/engine.py:1305`) turns on when an integration has both `get_attributes` and
  `set_attributes`. When it's on, `Snapshot.take()` (`ha_rig/engine.py:970`) reads an off light with
  `deviceside.get_attributes` before the case runs. ha-rig #30 made that change on purpose, for Shelly.
- The engine tests run as `shelly` against `AdapterRegistry.discover()`, the live registry. `FakeHA.call_service`
  (`tests/rig_fakes.py:297`) sends every deviceside call to the test's single `respond`. Once Shelly had both
  adapters, the snapshot read went to handlers written for `turn_on_for`. Those handlers raised, hit a `KeyError`
  on `duration`, or used up a scripted reading meant for a later step.

The fix: tests declare which adapters exist, so deviceside's adapter list can't change which engine path they take.
A companion PR (`wt/ci-tests-with`, `docs/plans/2026-11-08-ci-tests-with.md`) closes the CI gap that let this reach
main, and depends on this one.

## Coordination
Intent: The rig's engine, auto and progress tests declare which deviceside adapters exist, so a new deviceside
adapter can't silently send them down a different engine path. Fixes main's red CI from deviceside #287.
Touches: tests/rig_fakes.py, tests/test_rig_engine.py, tests/test_rig_auto.py, tests/test_progress.py, CLAUDE.md

## Steps

1. **`Roster` in `tests/rig_fakes.py`.** It wraps `AdapterRegistry.discover()`, discovered once at module level and
   shared, and takes `{integration: {action, ...}}`.
   - `adapter(action, integration)`: the real adapter when the pair is declared, else `None`. A declared pair that
     the real registry lacks **raises** (naming the pair), so a roster can never claim an adapter deviceside
     doesn't have.
   - Every other attribute (`actions`, `integrations`, ...) delegates to the real registry, so real scopes, kinds
     and titles stay honest.
   - Docstring: why the roster exists, in one paragraph (an adapter's presence changes which path the engine takes).
2. **Engine tests.** Replace `REGISTRY = AdapterRegistry.discover()` (`tests/test_rig_engine.py:49`) with a
   declared roster. Declare only what the tests need. Shelly gets the snapshot pair **off** by default, as the
   tests assumed before #287. ZHA and Z2M keep `get_attributes` + `set_attributes` where tests rely on them (see the
   comments around lines 1507 and 1545). The tests at ~289–306 and ~1876–1930 that exercise the snapshot path get
   rosters that declare the pair explicitly. Keep the `SimpleNamespace` stubs (`GROUP_REGISTRY` etc.): they are
   already decoupled.
3. **Snapshot coverage.** Check what the existing tests cover before adding any. Make sure one test, on a roster
   with Shelly's pair declared, shows: an off light is read with `get_attributes` before step 1; the case's
   `respond` for its own action never sees that read; and restore turns the light off, then `set_attributes`
   with what was stored. Add only what's missing.
4. **`tests/test_rig_auto.py:16` and `tests/test_progress.py:64`** get rosters declaring what they use. Leave
   `tests/test_rig_steps.py` on the real registry: it is the contract test that step files match deviceside's
   actions and integrations.
5. **CLAUDE.md**, Layout's `tests/` line: the tests take adapters from a declared roster over deviceside's
   registry (`tests/rig_fakes.py`, `Roster`). One clause, no more.

## Verification

- `.venv/bin/pytest` and `pre-commit run --all-files` pass.
- **Decoupling proof:** export deviceside at `a003b10` (before #287), `8d23731` (#287) and `origin/main` into the
  scratchpad with `git -C ../deviceside archive <c> | tar -x -C <dir>`. Run the full suite with
  `DEVICE_SIDE_REPO=<dir>` on each. All three must pass. Before the fix, `8d23731` fails 6 tests.
- `/audit-code` on the diff, fix its findings, then `gh pr ready`. Merge: `merge-green <n> --squash
  --delete-branch`, backgrounded.
