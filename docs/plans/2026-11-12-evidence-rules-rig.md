# Evidence rules, step 3: the rig runs what it is asked

Step 3 of deviceside's evidence-rules plan (`docs/plans/2026-11-12-evidence-rules.md` there), with the owner's
direction of 2026-11-12: the rig is a dumb implementer of a test plan, and deviceside decides the facts and logic of
what needs testing.

## Coordination

Intent: drop the rig's dependence on deviceside's `deviceside_wire` and `deviceside_scope` events (removed in step 2),
and every rule the rig kept about what is proven or stale.
Touches: ha_rig/{client,coverage,engine,observed→facts,progress,runlogs,staging,target}.py, scripts/rig.py, README.md,
docs/rig.md, pyproject.toml, tests/**
Tests-with: the deviceside PR for step 2 (runtime), which removes the events.

## Parts

1. **Wire and scope taps.** Gone from the client, the engine, the fakes and the run's logging: no `deviceside_wire`
   or `deviceside_scope` subscription, no wire logger raised to DEBUG. Records lose `Covers`, reports their Calls.
2. **Coverage.** `ha_rig/coverage.py` goes: fingerprints, fresh records, observed hints, `check_release.adapter_code`
   and `--coverage`. A run runs the suites it is asked for; a record never leaves one out. `target.test_records_dir`
   reads deviceside's layout directly, so the rig no longer loads `check_release.py`.
3. **The ledger** (`progress.py`) only resumes an interrupted run: entries keyed by suite and commit, no firmware-fact
   ledger, no traces or scopes. (Landed with step 3b: the step-file digest went once the step files moved into
   deviceside, whose commit covers them.)
4. **Fact cases** (owner, 2026-11-12): the watched case of a test with `observe:`, and every case of a
   `firmware_fact: true` test. A default run skips them, saying how to ask, and runs an observed test's automated
   case like any other; `--facts` adds them. Their results go to an append-only `_facts.md` per integration, kind `automated` or `observed`, never into a record,
   which lists them as skipped and quotes the latest row on file. No hashing, lending or blocking. One-off probes are
   no rig tests: they live in deviceside research files.
5. **Records** need every default case that applies to pass (owner, 2026-11-12), with a `Ran` row.
6. **Z2M BUSY** windows come from the rig's own call-step times, on the admin connection's Z2M log feed.

## Later

- Deviceside's `check_release.py --signoffs` (step 4) is to list the integrations needing a sign-off in a
  machine-readable form; a later rig change takes it as the selection (TODO in docs/rig.md).
- The step files move to deviceside in a separate step. Landed as step 3b: they live in deviceside's
  `tests/rig/steps/`, the rig reads them from the checkout under test (`target.steps_dir`), and a run with uncommitted
  step files goes ahead but writes no record and neither reads nor saves the ledger, so a record always cites the step
  files that ran.
