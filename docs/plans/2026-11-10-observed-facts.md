# Observed passes stand while the call pattern stands (issue #36)

Issue: #36. Branch: `wt/observed-facts`.

## Context

Issue #36: when an unattended run reruns a suite, it writes `not observed` over an earlier observed pass, so the
record narrows. Jason's rule (2026-11-10) reframes the issue:

- **An observed run proves a capability.** It shows how a device responds to a call pattern (for example, which
  color-change pattern doesn't jump). Once observed, the capability stays proven.
- **A code change that still sends the same calls needs no new observed run.**
- **The approach is optimistic.** Firmware upgrades, HA upgrades, other models from the same maker, other entities
  and other roles never void an observation. An occasional regression will be missed; users report it, and an
  attended run then records the failure.
- **Only these void or block a lend:** a different call pattern (fingerprint), an edited test, or an observed
  failure on that model.

The observations live in the deviceside test records, in a new `## Observed` section. That section is the only
store: they ride the existing staging/promote path (`ha_rig/staging.py`), stay public, and are versioned with the
code they describe. No deviceside record has an observed pass today, so nothing needs migrating.

## Coordination

Intent: Observed results are kept in each record's `## Observed` section, keyed by test, maker and call fingerprint.
A run lends an observed pass when its automated twin sends the same calls, so an unattended run never narrows a
record. Closes #36.
Touches: ha_rig/engine.py, ha_rig/client.py, ha_rig/coverage.py, ha_rig/staging.py, docs/rig.md, README.md, tests/
Tests-with: jasonmx/deviceside#<wire-trace PR>
Depends-on: jasonmx/deviceside#<wire-trace PR>

## Design

### deviceside PR (lands first)

- **Wire trace.** A helper in `core/`, `wire_trace(hass, entity_id, transport, command, params)`. Each transport's
  send function calls it: ZCL (`zha/zcl.py`, `core/zcl_write.py`), Hue CLIP (`hue/bridge.py`), Shelly RPC
  (`shelly/rpc.py`) and MQTT (`zigbee2mqtt/bridge.py`). It fires the HA event `deviceside_wire` only while the logger
  `custom_components.deviceside.wire` is at DEBUG. `params` carries only the payload, never the target's addressing
  (IEEE, Hue resource ID, Shelly ID, MQTT device topic). The target goes in `entity_id`. Reads are not traced.
- **CLAUDE.md** ("Proof follows the call"): one sentence on what a row in `## Observed` proves. An observed pass
  proves a maker's devices respond as seen to that fingerprint's calls. It stands across firmware, HA and code until
  the calls change, the test changes, or an observed failure on a model blocks lends to that model. A rule change is
  outward-facing, so Jason reviews that sentence.
- **`check_release.py --records`**: confirm it ignores the new section, and add a test that pins this.
- Unit tests check that each transport emits the trace and that no addressing reaches `params`.

### rig PR

**Capture and fingerprint (`client.py`, `engine.py`)**
- Subscribe to `deviceside_wire`, as `client.py` already subscribes to `state_changed`. Raise the wire logger with
  the `set_log_level` / `Baseline` that `--debug-logs` already uses, and restore it afterwards.
- During a case's `steps` (not its setup), keep the events for that case's entities only, which keeps parallel lanes
  apart.
- The fingerprint is each entity's ordered list of `(transport, command, params)`, with consecutive repeats collapsed.
  Entities are ordered by their role or member slot, never by entity ID. Interleaving between entities and timing
  are ignored. It is stored on `CaseResult` as a 12-hex hash. The run report lists the full calls.

**Record (`engine.py`: `Record`, `render_record`, `write_records`)**
- A new `## Observed` table, written after `## Tests`. Columns: test, maker, model, firmware, fingerprint, test
  digest, date, result (`pass` / `fail`). `Record.read` parses it.
- **Writing:** start from the rows of the existing record in `records_dir`. Add a row for each observed case this run
  actually observed (attended pass or fail). A row with the same (test, maker, model, firmware, fingerprint, digest)
  is replaced, so only its date changes. Drop rows whose test no longer exists in the step file. `not observed`
  adds nothing. The rows carry forward even when the #35 narrowing rule keeps the record.
- **Tests table:** a lent observed case shows `pass (observed <date>)`. `CASE_ROW` parsing strips that suffix, as it
  strips `FIRMWARE_FACT`.
- The footnote is one line on what a lent observed pass means.

**Lending (`engine.py`, observed phase)**
- Every observed test takes part, with no flag.
- Lending happens after the automated twin has run, because it needs the twin's fingerprint from this run. An
  observed case is lent (not run, not asked, result `PASS` with an observed origin) when the record has a `pass` row
  that matches all of these:
  - the same test name (any role), digest and fingerprint as the twin's
  - the same maker as every device in the case
  - no `fail` row for one of the case's models that is newer than the latest `pass` on that model
- If the twin passed but captured no trace, or there is no matching row, nothing is lent. The observed case runs
  attended, or reports `not observed` when unattended. This is what fixes #36: an unattended run writes
  `not observed` only where no observation covers the calls the code sends now.
- An attended run still lends. Add `--reobserve` to force observed cases to run (it reruns everything they would
  have lent), for spot checks after a major firmware upgrade. README `--help` block is regenerated.
- The ledger saves a lent result like any other, for resume.

**Coverage (`coverage.py`)**
- `--coverage` can't confirm a lend without a run (it has no fingerprint). It shows an observed case with a matching
  row as `observed <date>, if the calls are unchanged`, and counts it as covered only for an unattended plan. It also
  notes, for information, rows observed on another firmware or model than the profile's devices.

**Out of scope:** test-level `firmware_fact` (ledger-based) stays as it is. A follow-up issue considers moving it to
this record-based, optimistic model.

**Docs:** `docs/rig.md` (Observed, Records, Coverage, Firmware facts cross-reference); `coverage.py` docstring.

## Steps

1. `pr-draft` this plan. In a deviceside worktree, build the wire trace and open its draft PR, then fill in the
   `#<wire-trace PR>` placeholders above.
2. **Spike:** deploy that worktree; run the suites with observed tests (`stop`, `set_attributes`, `raise_hue`)
   `--unattended` twice with the trace on. Confirm the fingerprints match across the two runs at the same code. Where
   they don't, normalize the field that jitters (round it, or keep only its key), but never drop a field that decides
   the pattern, such as a transition time. Write the result into the plan.
3. Capture and fingerprint on `CaseResult`; full calls in the run report.
4. `## Observed` read/write in `Record`, `render_record`, `write_records`; the carry-forward, replace and prune rules.
5. The lend in the observed phase; `--reobserve`; the README `--help` block.
6. `--coverage` wording; docs.
7. Live check (below), `/audit-code`, then mark both PRs ready. The deviceside PR merges first; then `gh run rerun`
   the rig PR's Tests runs before merging.

## Verification

- Unit tests (`tests/`, fakes):
  - `## Observed` round-trips; rows carry forward through unattended runs and through a #35 keep.
  - Same key replaces the row; a deleted test prunes its rows.
  - Lend when test, digest, maker and fingerprint match, on another model, firmware or role.
  - No lend when the fingerprint differs, the twin captured nothing, the maker differs, or a newer `fail` on that
    model exists.
  - An unattended run with a lend writes `pass (observed …)`, not `not observed`.
  - `--reobserve` runs the observed cases.
- `.venv/bin/pytest`, `pre-commit run --all-files`, and `tests/test_rig.py` (the `--help` block).
- Live, through `scripts/rig.py` behind its gate, on one suite:
  1. Attended: the `## Observed` rows are written.
  2. A no-op adapter change, then `--unattended`: the observed cases are lent and the record shows
     `pass (observed …)`.
  3. Change the adapter's call on a scratch branch, then `--unattended`: `not observed`, no lend.

## Model

opus: two repos, a rule change in deviceside's `CLAUDE.md`, a new section in a public record format, and a
fingerprint normalization that a wrong call would let slip past tests.
