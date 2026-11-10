# Observed passes stand while the call pattern stands (issue #36)

Issue: #36. Branch: `wt/observed-facts`.

## Context

Issue #36: when an unattended run reruns a suite, it writes `not observed` over an earlier observed pass, so the
record narrows. Jason's rule (2026-11-10) reframes the issue:

- **An observed run proves a call pattern, not a test.** It shows how a maker's devices respond to a sequence of calls
  from a given starting state (for example, which color-change pattern doesn't jump). Once observed, the pattern
  stays proven, whichever test sends it.
- **Prefixes are covered.** A device can't know which calls are still to come, so observing raise-stop-raise also
  observed raise-stop, from the same starting state.
- **The approach is optimistic.** Firmware upgrades, HA upgrades, other models from the same maker, other entities,
  roles and tests, and timing between calls never void an observation. An occasional regression will be missed;
  users report it, and an attended run then records the failure.
- **Only these block a lend:** calls that no observed pass covers, or an observed failure on that model whose
  sequence the candidate begins with.

Observations live in deviceside, in one file per integration, `docs/test-records/<integration>/_observed.md`. One
file per integration is needed because a prefix can match across suites (raise-stop-raise is in the raise suite,
raise-stop in the stop suite). `check_release.py` only reads `<action>(-<option>).md`, so it skips this file. It is
staged and promoted with the records (`ha_rig/staging.py`), stays public, and is versioned with the code. No
deviceside record has an observed pass today, so nothing needs migrating.

## Coordination

Intent: Observed results are kept per integration in `_observed.md`, keyed by maker, starting state and call sequence.
A run lends an observed pass when its automated twin's calls are covered by an observed pass, prefixes included, so an
unattended run never narrows a record. Closes #36.
Touches: ha_rig/engine.py, ha_rig/client.py, ha_rig/coverage.py, ha_rig/staging.py, ha_rig/observed.py,
docs/rig.md, README.md, tests/
Tests-with: jasonmx/deviceside#329
Depends-on: jasonmx/deviceside#329

## Design

### deviceside PR (lands first)

- **Wire trace.** A helper in `core/`, `wire_trace(hass, entity_id, transport, command, params)`. Each transport's
  send function calls it: ZCL (`zha/zcl.py`, `core/zcl_write.py`), Hue CLIP (`hue/bridge.py`), Shelly RPC
  (`shelly/rpc.py`) and MQTT (`zigbee2mqtt/bridge.py`). It fires the HA event `deviceside_wire` only while the logger
  `custom_components.deviceside.wire` is at DEBUG. `params` carries only the payload, never the target's addressing
  (IEEE, Hue resource ID, Shelly ID, MQTT device topic). The target goes in `entity_id`. Reads are not traced.
- **CLAUDE.md** ("Proof follows the call"): one sentence on what an `_observed.md` pass proves. A maker's devices,
  started from that state, respond as seen to that call sequence and to any sequence it begins with. It stands across
  firmware, HA and code; an observed failure on a model blocks lends to that model for sequences that begin with the
  failed one. A rule change is outward-facing, so Jason reviews that sentence.
- **`check_release.py --records`**: add a test pinning that `_observed.md` is never read as a record.
- Unit tests check that each transport emits the trace and that no addressing reaches `params`.

### rig PR

**Capture (`client.py`, `engine.py`)**
- Subscribe to `deviceside_wire`, as `client.py` already subscribes to `state_changed`. Raise the wire logger with
  the `set_log_level` / `Baseline` that `--debug-logs` already uses, and restore it afterwards.
- During a case's `steps`, keep the events for that case's entities only, which keeps parallel lanes apart. Steps
  that call HA services directly (not a deviceside action) bypass the trace, so the rig adds them itself as
  `("ha", "<domain>.<service>", data)` in order.
- **Starting state:** the case's `setup` steps as written, with entities replaced by their role or member slot,
  hashed. This uses the rig's own definition of the setup, not the trace, because setup often uses plain HA calls.

**Call sequences (`ha_rig/observed.py`, new)**
- A call is `(transport, command, params)`, normalized and hashed to 6 hex characters. **Normalized: every number
  becomes its sign** (`-`, `0`, `+`), recursively through dicts and lists; keys, strings, booleans and None stay as
  they are. So the hash is the call's structure: which command, which fields, which strings, zero or not, and a signed
  rate's direction. The legend (`## Calls` in `_observed.md`, and the run report) keeps each call as sent, exact
  values included. Decided 2026-11-10 from the spike (below): observed tests gate whether firmware behaves as expected
  (jumps, flashes, drift at a stop), which no step file or firmware fact ties to a magnitude. A sequence is, for each slot (role or member slot, never an entity ID), the ordered list of call
  hashes, with consecutive repeats collapsed. Interleaving between slots and timing are ignored.
- **Covers:** a pass with sequence P covers a candidate C when the starting states match, the slots match, and for
  each slot C's list is a prefix of P's (equal counts as a prefix).
- **Blocks:** a fail with sequence F on model M blocks C on M when F's list is a prefix of C's for every slot and the
  starting states match, and when the fail is newer than the latest pass on M that covers C. A failure of the longer
  sequence never blocks a shorter one.
- Pure functions, unit-tested without HA.

**The `_observed.md` file (`observed.py`; written via `write_records`)**
- `## Observed`: one row per observation: maker, model, firmware, starting state (hash), sequence (per slot:
  `slot: h1 h2 …`), observed in (`test[role]`, for information only), date, result (`pass` / `fail`).
- `## Calls`: a legend from each call hash to its readable call, so a user's regression report can be checked
  against what was observed.
- **Writing:** read the integration's file from `records_dir`. Add a row for each case this run actually observed
  (attended pass or fail). A row with the same (maker, model, firmware, starting state, sequence) is replaced, so
  only its date and "observed in" change. `not observed` adds nothing. No pruning: rows are few and a pattern
  can come back. It is staged and promoted like a record, and is written whenever a run observed something, even if
  no suite record is written.
- **Suite record:** a lent observed case shows `pass (observed <date>)` in its Tests table, plus a one-line footnote
  pointing to `_observed.md`. `CASE_ROW` parsing strips the suffix, as it strips `FIRMWARE_FACT`.

**Lending (`engine.py`, observed phase)**
- Every observed test takes part, with no flag.
- Lending happens after the automated twin has run, because it needs the twin's sequence from this run. An observed
  case is lent (not run, not asked, result `PASS` with an observed origin) when a `pass` row covers the twin's
  sequence, every device in the case has that row's maker, and no fail row blocks it on the case's models.
- If the twin passed but captured no trace, or no row covers it, nothing is lent. The observed case runs attended,
  or reports `not observed` when unattended. This is what fixes #36.
- An attended run still lends. `--reobserve` forces observed cases to run, for spot checks after a major firmware
  upgrade. The README `--help` block is regenerated.
- The ledger saves a lent result like any other, for resume.

**Coverage (`coverage.py`)**
- `--coverage` can't confirm a lend without a run (it has no sequence yet). For each observed case, it shows whether
  an earlier run's twin sequence (from the ledger, if any) is covered: `observed <date>, if the calls are unchanged`.
  It also notes, for information, rows observed on another firmware or model than the profile's devices.

**Out of scope:** test-level `firmware_fact` (ledger-based) stays as it is. A follow-up issue considers moving it to
this call-based, optimistic model.

**Docs:** `docs/rig.md` (Observed, Records, Coverage, Firmware facts cross-reference); `coverage.py` docstring.

## Spike results (step 2, 2026-11-10)

Two `--unattended --fresh` runs of `stop`, `set_attributes` and `raise_hue` on `lab` at the same DeviceSide code
(reports `rig-run-2026-11-10-130051.md` and `-131711.md` in `$TMPDIR/deviceside-rig/`; both exited 1 on things
outside the spike: a Z2M `get_attributes` timeout warning, and Hue `sets_after_a_soft_off[group]` failing in run B).

- 65 traced cases; every starting state matched. 53 sent identical calls.
- 11 differed (one more was run B's failed case, which sends fewer calls; only a passing twin lends), all in `stop` /
  `stop-predicted_state`, all numbers computed from where the light was when stopped, so timing showing up in the
  values: Hue's pinned `dimming.brightness` (68.27 vs 69.14) and `color.xy` (4th decimal), Hue `transition_time` of the
  remaining span (5860 vs 5892, 6875 vs 6898), Z2M pinned `brightness` (167-174) and `transition` (6.6-6.9), ZHA
  `transition_time` (57 vs 58). Largest gap 4.5 %.
- Rejected: 2 significant figures (68 vs 69 already splits); 1 significant figure (uneven precision, rounding edges);
  a 10 % tolerance (works, but isn't transitive, so can't be hashed: a rework of matching and the file format); an
  exact-values-but-blank-live-fields list (goes stale per transport); keeping integers 0-15 exact for enums (a second,
  arbitrary rule).
- **Known blind spot, accepted:** ZHA's EnhancedMoveHue (`integrations/zha/fade.py`, `hue_move`) carries direction as
  `move_mode` 1 (up) vs 3 (down), which both normalize to `+`. It matters only if an observed `lower_hue` test gets
  `raise_hue`'s setup on ZHA; none exists. One line for it in `docs/rig.md`, Observed tests.
- `normalize` (sign rule) is in, with tests; docs/rig.md carries the rule and the blind spot. Shared prefixes: none. Run A's 65
  traced cases, hashed with `normalize`, have 0 pairs where one begins another (same state and slots), so the live
  check's lend (Verification, Live 2) needs a prefix built for it, not one found in today's tests.

## Steps

1. `pr-draft` this plan. In a deviceside worktree, build the wire trace and open its draft PR, then fill in the
   `#<wire-trace PR>` placeholders above.
2. **Spike:** deploy that worktree; run the suites with observed tests (`stop`, `set_attributes`, `raise_hue`)
   `--unattended` twice with the trace on. Confirm the sequences match across the two runs at the same code. Where
   they don't, normalize the field that jitters (round it, or keep only its key), but never drop a field that decides
   the pattern, such as a transition time. Also note which observed tests share prefixes today. Write the results
   into the plan.
3. `observed.py`: sequences, covers, blocks, the file's read and write.
4. Capture in `client.py` and the engine: sequences on `CaseResult`, full calls in the run report.
5. The lend in the observed phase; writing `_observed.md` through staging; the suite record's suffix and footnote.
6. `--reobserve`, the README `--help` block, `--coverage` wording, docs.
7. Live check (below), `/audit-code`, then mark both PRs ready. The deviceside PR merges first; then `gh run rerun`
   the rig PR's Tests runs before merging.

## Verification

- Unit tests (`tests/`, fakes):
  - `observed.py`: equal sequences cover; a prefix covers; a longer one doesn't; the starting state must match; the
    slots must match; repeats collapse.
  - Blocks: a fail on M blocks sequences that begin with it on M only; a newer pass on M lifts it; a fail of
    raise-stop-raise doesn't block raise-stop.
  - `_observed.md` round-trips; same key replaces; rows survive unattended runs and #35 keeps; promoted with records.
  - Lend across tests, suites, roles, models and firmware of one maker; no lend for another maker, a missing trace,
    or an uncovered sequence.
  - An unattended run with a lend writes `pass (observed …)`, not `not observed`; `--reobserve` runs them.
- deviceside: the `check_release.py` test that `_observed.md` isn't a record.
- `.venv/bin/pytest`, `pre-commit run --all-files`, `tests/test_rig.py` (the `--help` block).
- Live, through `scripts/rig.py` behind its gate:
  1. Attended, on the suite with the longer sequence: rows are written to `_observed.md`.
  2. `--unattended` on a suite whose observed test is a prefix of it: lent, `pass (observed …)`.
  3. Change the adapter's call on a scratch branch, then `--unattended`: `not observed`, no lend.

## Model

opus: two repos, a rule change in deviceside's `CLAUDE.md`, a new public file format, and a call normalization that a
wrong call would let slip past tests.

## Handoff

**Done** (branch `wt/observed-facts`, worktree `~/projects/ha/ha-rig-wt/observed-facts`, draft PR #48; deviceside
jasonmx/deviceside#329 draft, `wt/wire-trace`, worktree `~/projects/ha/deviceside-wt/wire-trace`):

- Steps 1, 3-6 coded (`18b331a`); full suite green (1044) after `714f4bf` (FakeHA `logger.set_level`, the wire hold in
  the signal test, README `--help` regenerated). `8a71198`: the run report's `### Calls` section per traced case
  (`engine.calls`, `observed.render_call`), and `docs/rig.md` for observed lends (Observed tests, Coverage, Run reports,
  Test records).
- Step 2's spike: two runs, results and the normalization decision above (Spike results; Design, Call sequences).
- #329: Jason approved its observed-pass sentence in `CLAUDE.md` as written, and had "passed on one light (PR #204)"
  generalized to "one device" (`fca797f`, pushed).

**Also done:** `observed.normalize` (numbers to their sign, bool exact), legend keeps the exact call, tests, `docs/rig.md`
(sign rule, ZHA `move_mode` blind spot), shared-prefix result in Spike results (none).

**Next:**

1. Step 7, live check (Verification, Live 1-3) through `scripts/rig.py` behind its gate. It needs Jason at the rig
   (Live 1 is attended) and a prefix pair built for Live 2, as today's tests have none.
2. `audit-gate brief pr` and the audit; both PRs ready; #329 merges first, then `gh run rerun` #48's Tests runs
   before merging.

**Open:** nothing for Jason. Known gap from before: rows from a resumed session's saved cases are re-dated to the
finishing session.
