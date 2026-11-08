# Test records never narrow: a run keeps a record that proves cases it skipped

## Context

On 2026-11-08 a deviceside session ran profile `shelly_bulb` (a bulb with no relays). The run rewrote
`docs/test-records/shelly/{turn_on_for,turn_off_for,get_attributes}.md`. An earlier `shelly` run had written those
records with `switch_plug` / `light_onoff` / `light_wrapped` passes. The rewritten records show only the bulb, which
drops the proof for Shelly's Verified `switch` domain (`verified("light", "switch")` in those three adapters). The
session restored main's versions by hand in jasonmx/deviceside#295.

**Cause, confirmed:**

1. `engine.cases()` (`ha_rig/engine.py:681`) plans a role the profile lacks as a skipped case (`no switch_plug entity
   in the profile`). That case counts as a result, so `merge()` marks the suite `complete`, and `recordable` only needs
   one proven case and no failure (`engine.py:900`).
2. `write_records()` (`engine.py:2308`) writes `<integration>/<suite>.md` with no look at what is already there.
   `docs/rig.md` even says "records from different profiles of the same suite overwrite each other".
3. The reverse problem is in `coverage.freshness()` (`ha_rig/coverage.py:139`). It compares only case IDs, and those
   are the same under every profile (all roles plan, absent ones as skips). So a bulb-only record reads as **fresh**
   for a later full `shelly` run, which would then leave the suite out as covered and never run the switch cases.

The other bulb-only Shelly records (`clear_auto_*`, `set_auto_*`, `lifecycle`, `set_attributes`, dimming) were never
broader, and the auto adapters are experimental. Nothing else was lost.

**Decided with Jason:** a record never narrows. A run doesn't write a record that would turn a case the old record
passed into a profile skip. A suite the profile can't fully refresh still runs, and the brief says it won't write a
record. To narrow on purpose (say, a retired device), delete the record by hand in the deviceside PR. There is no
override flag. A record stays one run, one profile, one commit, so deviceside's record rules are unchanged.

## Coordination

Intent: A test record never loses a case it proved to a profile that lacks the role, and coverage doesn't count a
narrower profile's record as covering a broader one.
Touches: ha_rig/engine.py, ha_rig/coverage.py, scripts/rig.py, docs/rig.md, tests/test_rig_engine.py,
tests/test_coverage.py, tests/test_rig.py, tests/test_progress.py

No deviceside change: what a record proves is unchanged, and check_release.py reads the same files. No `Tests-with`.

## Steps

1. **Mark profile skips.** Add `absent: bool = False` to `Case` and `CaseResult` (`engine.py:607`, `:862`), set it
   only at `engine.py:681` (the `no <role> entity in the profile` branch), and carry it through `_not_run`. Don't
   match the skip string. Check that `progress.py` reads an old ledger entry without the key (the dataclass default
   should cover it). Add a test in `tests/test_progress.py` if it doesn't.

2. **Read what a record proved.** Move `Record` and its regexes from `coverage.py` to the test-records section of
   `engine.py`, next to `render_record`, since the writer now reads it (coverage imports engine, not the other way
   round). Then re-export it from `coverage`. Extend `Record.read` to parse each Tests row's result column into
   `passed: frozenset[str]` (result `pass`) and `skipped: frozenset[str]` (result `skipped`; records write no detail).
   Keep `case_ids`.

3. **Refuse to narrow in `write_records`.** Before it writes, read the existing record. The suite *narrows* when a
   case the old record passed is still planned and is now `absent` in the result. In that case, keep the file, and
   return the suite with those case IDs instead of a path. Change the return to `(written, kept)`, where `kept` is a
   list of `(path, case_ids)`. `rig.py:2110` prints for each kept record:
   `Kept docs/test-records/shelly/turn_on_for.md: profile shelly_bulb skips on_then_off[switch_plug], … which it passed;
   run a profile with those roles, or delete the record to narrow it.`
   A kept suite stays in the ledger (only `written` drops it, `rig.py:2113`), which is correct.

4. **Coverage, both directions** (`coverage.freshness` / `SuiteCoverage`):
   - *Broader profile, narrower record:* the record is stale when this profile plans a case without a skip that the
     record shows as `skipped`. Reason: `stale: this profile runs what the record skipped: on_then_off[switch_plug], …`.
     Put this after the code and case-ID checks. `freshness` needs the planned `Case`s, not only their IDs. Pass
     `planned` as cases and derive the IDs inside.
   - *Narrower profile, broader record:* add `SuiteCoverage.narrows` = the record's passed cases that this profile
     plans as `absent`. Where the suite isn't covered, `__str__` adds `; won't write a record: this profile skips
     <ids>, which the record passed`. Use the same wording for `covered (saved; no record yet)`, so the next bulb run
     shows why the record never comes. The suite still runs (decision above). A fresh broader record still covers the
     narrower profile, as today.

5. **Docs.** In `docs/rig.md` § Profile (line 67), replace "records from different profiles of the same suite
   overwrite each other" with the never-narrow rule and how to narrow on purpose. Add one line to § Test records
   (line 330) and one to § Coverage. Update the `write_records` and `freshness` docstrings. No `--help` change.

## Tests

- `tests/test_rig_engine.py`, next to `test_a_full_pass_writes_a_record_and_a_failure_writes_none`:
  - a record with `switch_plug` and `light_dimmable` passes, then a bulb-only run: the file is unchanged, and the
    result reports `kept` with `on_then_off[switch_plug]`;
  - the same old record, then a run with both roles: it is rewritten;
  - an old record whose `switch_plug` case is no longer planned (test removed): the run writes, as nothing proven is
    lost;
  - an old record that already skipped `switch_plug`: the bulb run writes.
- `tests/test_coverage.py`: a bulb-only record (switch rows `skipped`) is stale for a profile with `switch_plug`,
  with that reason, and fresh for the bulb profile. A broader fresh record is fresh for the bulb profile, and `narrows`
  is empty. A broader record that is stale for code sets `narrows`, and the string carries the warning.
- `tests/test_rig.py`: the report path prints `Kept …`, and the kept suite stays in the ledger.
- `tests/test_progress.py`: a saved `CaseResult` with no `absent` key still loads.

## Verification

- `.venv/bin/pytest` and `pre-commit run --all-files`.
- Without a rig run: point coverage at the deviceside checkout with `scripts/rig.py --coverage --profile shelly_bulb
  --integration shelly`, which is read-only. Its turn_on_for / turn_off_for / get_attributes lines should be covered
  by the `shelly` records restored in #295. Then run `--coverage --profile shelly`; with those records fresh, they read
  covered too. Jason decides on any real run.
- `/audit-code` on the diff before the PR (CLAUDE.md).

## Out of scope (file as an issue)

An **unattended** run can also narrow a record. It writes `not observed` where the old record had proven observed
passes. Blocking that too would mean only an attended run could refresh a stale record. That changes what deviceside
accepts as a record (rig.md: "Tests marked `not observed` had nobody watching"), so it needs Jason's and deviceside's
call. File it in ha-rig with that framing. Recommended model: opus, since it's a rule change.
