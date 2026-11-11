# Records name the scope they proved: a `Covers` row

## Coordination

Intent: each test record gains a `Covers` row: the domains, capabilities and fields DeviceSide's `deviceside_scope`
events named for the suite's proven cases, and each of the adapter's options on or off. deviceside's
`check_release.py --records` will hold it against the adapter's Verified scope.
Touches: ha_rig/client.py, ha_rig/engine.py, ha_rig/progress.py, tests/rig_fakes.py, tests/test_rig_engine.py, tests/test_progress.py, docs/rig.md
Tests-with: DeviceSide/DeviceSide#376

## Background

- deviceside `notes/plans/2026-11-11-hue-off-and-record-coverage.md`, B and B decisions (Jason, 2026-11-11).
- DeviceSide#376 (merged): while its wire logger is at DEBUG the dispatcher fires `deviceside_scope` per target it
  runs, past the Experimental gate: `entity_id` (the target results are keyed on), `action`, `integration`, `domain`,
  `capability` (null when none), `fields` (sorted, after drops).

## Decisions

- **Subscription:** `subscribe_wire` also subscribes to `deviceside_scope` (same admin connection, same logger).
  A scope tap, opened with the wire tap in `_Case.tap` on the same slots, collects the events on the case's entities;
  `drain_wire` drains both.
- **Per case:** `CaseResult.scope`, the distinct events' (action, integration, domain, capability, fields) as a
  sorted tuple, kept for PASS and FAIL like `trace`. Saved in the ledger (`_saved`/`_result`), so a resumed run
  records as a whole one would; an older entry without it loads as empty.
- **Per record:** the union over `SuiteResult.proven`, keeping only events of the suite's action and integration
  (a test's steps can call other actions). Options: `{name: name in suite.file.options for name in
  adapter.scope.options}`, as the run pins them, passed to `render_record` by `write_records`.
- **Format:** one key/value row after `Device`/`Options`, all four keys always, in this order, values sorted and
  comma-joined, empty when none:
  `| Covers | domains=light,switch; capabilities=Switch; fields=brightness; options=predicted_state:on |`.
- No grandfathering: every record is rerun before the next RC anyway.

## Handoff

Done: draft PR #62 on `wt/scope-covers` (worktree `../ha-rig-wt/scope-covers`). Commit 986dd99: `client.py` has
`SCOPE_EVENT`; `subscribe_wire` subscribes to it too; taps are typed `(event_type, entity_ids, list)`, and
`wire_tap(entity_ids, SCOPE_EVENT)` collects scope events (`wire(data, event_type)`). Fake tap type updated; 1064 tests
pass.
Next, in order:
1. `_Case.tap` (`engine.py` ~1520): open a second `wire_tap(slots, SCOPE_EVENT)` beside the wire one; `drain_wire`
   already covers both. In `_Case.run` (~1500) set `result.scope` from it for PASS and FAIL, like `trace`.
2. `CaseResult.scope: tuple[tuple, ...] = ()`: sorted distinct `(action, integration, domain, capability,
   tuple(fields))`. Ledger: `progress.py` `_saved` (lists) / `_result` (`case.get("scope", [])` → tuples).
3. `render_record(..., options: Mapping[str, bool] = {})` adds the `Covers` row after `Options`: union over
   `suite_result.proven` filtered to `suite.action` and `suite.integration`; capability None left out.
   `write_records` (~2660) passes `{n: n in suite.file.options for n in registry.adapter(suite.action,
   suite.integration).scope.options}`.
4. Fakes: `FakeHA._trace` (`tests/rig_fakes.py` ~376) also fires scope events through a `scope_for` hook (default
   None: none fired, so existing tests hold). Tests: Covers row content and format, the action/integration filter,
   only proven cases count, ledger round-trip (resumed run renders the same row), options on/off.
5. `docs/rig.md`: the records section names the `Covers` row.
6. pytest + pre-commit, `/audit-code` on the diff, fix, `gh pr ready` per the rig's CLAUDE.md (check its
   CLAUDE.local.md for how it merges). Before merging: `gh run rerun` the PR's Tests runs.
7. Then deviceside step (3): `notes/plans/2026-11-11-hue-off-and-record-coverage.md` § Handoff, in a fresh `wt/`
   worktree there.
Open: PR #61 (`wt/z2m-busy`, no live agent) overlaps engine/client/progress/fakes; resolve by merge if it lands first.
