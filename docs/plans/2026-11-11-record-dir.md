# Records follow deviceside's `record_dir`

## Context

deviceside PR #362 moves each integration's test records from `docs/test-records/<x>/` to
`docs/integrations/<x>/records/`, and names the place once: `scripts/check_release.py`'s `record_dir(integration)`.
The rig builds the old path in four places (`scripts/rig.py` `TEST_RECORDS_DIR`, `coverage.py` `RECORDS`, staging's
`records_dir`, the engine's `records_dir / integration`). This PR asks deviceside instead, so the next move costs
nothing here.

## Design

- `ha_rig/target.py` loads the target's `check_release` once per checkout (moved from `coverage.adapter_code`'s
  loader) and gives `record_dir(repo, integration)`: deviceside's answer, relative to the checkout.
- The engine's record writers and readers (`write_records`, `add_observed`, `write_observed_case`,
  `load_observations`) take `records: Callable[[str], Path]`, an integration's records directory, in place of a root
  they joined the integration onto; `write_records`' `into` is one too. The engine stays free of deviceside paths and
  its tests pass a lambda.
- Staging mirrors the checkout: a staged record sits at `<staging>/<its path in the checkout>`, and the manifest keys
  records by that path. `records_dir` leaves the manifest; a manifest that still has one was staged by the old
  layout and is refused, naming the problem (promote it with the rig it came from, or rerun).
- `coverage.record_path` uses `record_dir`.

## Coordination

Intent: The rig reads and writes test records where deviceside's check_release.record_dir says, not at a path it builds.
Touches: scripts/rig.py, ha_rig/target.py, ha_rig/coverage.py, ha_rig/staging.py, ha_rig/engine.py, tests/**, README.md, docs/rig.md
Tests-with: jasonmx/deviceside#362
