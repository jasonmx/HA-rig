# Test consolidation: deviceside and the rig

## Coordination

Intent: cut test lines in both repos without losing what the tests catch. This is a plan only. Each item below is
its own small PR in the repo it touches, with no behavior change.
Touches: deviceside `tests/` (integrations, core, scripts, fixtures, conftest); rig `tests/`.
Tests-with: none. No item changes rig code or step files, or deviceside code.

## Measurements (2026-11-12, deviceside `5aae1fe`, rig `c5ce79f`)

**Units.** Sizes are code lines, not counting blanks, comments or docstrings. They were counted with an AST and
token line counter, so the numbers match the brief's.

| | Tests | Source |
|---|---|---|
| deviceside, all | 13,027 | 8,068 (component 6,307 + scripts 1,761) |
| integrations, total | 7,746 | 4,495 |
| zigbee2mqtt | 3,033 | 1,698 |
| zha | 1,913 | 1,114 |
| hue | 1,687 | 994 |
| shelly | 870 | 499 |
| matter | 243 | 187 |
| rig | 10,283 | 7,654 |

The integration rows count each package's tests and source. Their fixtures in `tests/fixtures/` add
hue 144, z2m 190, shelly 93, matter 82 and zha 37 lines.

**Baselines.**
- deviceside: 1,833 passed, and `check_release.py --coverage` passes.
- rig: 1,089 passed, 96% total line coverage.

**Tight modules under the ≥95% per-module bar.** Each of these fails the bar if it loses one more covered line:
- `integrations/shelly/fade.py`: 36 statements, 1 missed.
- `integrations/hue/transition_cache.py`: 56 statements, 2 missed.
- `integrations/zigbee2mqtt/get_attributes.py`: 38 statements, 1 missed.
- `core/wire.py`: 23 statements, 1 missed.

`zha/fade.py` is at 96.8% with a margin of 5 lines, and `zha/turn_on_for.py` can lose 1 line.

**Measure with real runs, not per-test contexts.** On Python 3.14, coverage 7.16 uses the sysmon core. That core
files each line under the first test that runs it. In the rig run only 485 of 1,089 tests show in the contexts at
all. Any "only this test covers it" answer from `--cov-context=test` is therefore wrong. Every coverage claim below
comes from a real run with the tests removed (`--deselect`/`--ignore`), or, in the rig, from a
`COVERAGE_CORE=ctrace` per-test run.

**Verified: these 10 deviceside deletions lose nothing.** Deselecting them together gives 1,823 passed,
`check_release.py --coverage` exits 0, and no line in the component or `scripts/` is newly missed:
- `test_config_flow.py::test_entry_loads_and_unloads`
- 7 tests in `zigbee2mqtt/test_turn_on_for.py`
- `hue/test_get_attributes.py::test_query_raises_when_any_target_fails`
- `test_check_release.py::test_a_failing_record_of_an_experimental_or_unproven_adapter_is_exempt`

**Checked: the raise/lower files are load-bearing.** A real run with all 18 `test_raise_*`/`test_lower_*` files
ignored drops four modules under the bar:

| Module | Now | Without the 18 files |
|---|---|---|
| `hue/fade.py` | 99.1% | 94.4% |
| `hue/transition_cache.py` | 96.4% | 94.6% |
| `shelly/fade.py` | 97.2% | 91.7% |
| `zha/fade.py` | 96.8% | 94.0% |

They are the tests of each integration's `fade.py`, so item D5 merges them and deletes none.

## Verification, for every item

**deviceside:**
1. `.venv/bin/pytest --cov=custom_components/deviceside --cov=scripts --cov-report=json:after.json`, then
   `scripts/check_release.py --coverage after.json` exits 0.
2. Diff `missing_lines` per file against the baseline JSON (`before.json`, from `main` at the item's base). No
   module may gain a missed line. The bar alone is not enough: four modules have no margin.
3. The test count may fall only by the tests the item names. A parametrized row counts as a test.
4. `pre-commit run --all-files`.

**rig:**
1. `DEVICE_SIDE_REPO=<deviceside checkout> .venv/bin/pytest`.
2. Then `COVERAGE_CORE=ctrace ... --cov=ha_rig --cov=scripts --cov-report=json`, and the same `missing_lines`
   diff. The rig has no per-module bar, so the rule is simply no newly missed line.
3. `pre-commit run --all-files`.

Known run quirks, not caused by this plan:
- `test_rig_busy::test_a_quiet_feed_subscribes_afresh_and_gets_what_came_after` timed out once in a plain run.
- `test_rig::test_readme_shows_the_current_help` fails under `coverage run`, because the program name in `--help`
  changes; pytest-cov runs pass.

## Plan, ranked by lines saved per hour

### deviceside

Hours are estimates. Lines/h is rounded.

| # | Item | Saves | Hours | Lines/h | Risk |
|---|---|---|---|---|---|
| D1 | Compute `test_registry`'s matrix | ~40 | 0.3 | 130 | low |
| D2 | Delete 10 verified-redundant tests | ~66 | 0.75 | 88 | low |
| D3 | `test_registry` tidy-up | ~23 | 0.4 | 58 | low |
| D4 | `add()` helper in `test_dispatcher` | ~25 | 0.5 | 50 | low |
| D5 | Merge raise/lower tests into one moves file per integration | ~220 | 4.5 | 49 | low |
| D6 | `gen_compat` non-deferred tables | ~39 | 0.8 | 49 | low |
| D7 | Integration setup helpers | ~90 | 2 | 45 | very low |
| D8 | Parametrize refusal tests over actions, per integration | ~125 | 3 | 42 | low |
| D9 | Fold kept z2m addressing tests into `test_addressing.py` | ~20 | 0.5 | 40 | low |
| D10 | Parametrize integration siblings that differ only in data | ~80 | 2 | 40 | low |
| D11 | Small core parametrizations | ~20 | 0.5 | 40 | low |
| D12 | `wire_off` fixture, scope-trace check into `test_wire` | ~15 | 0.4 | 37 | low |
| D13 | `test_scope` refusal table | ~11 | 0.3 | 37 | low |
| D14 | Entry-setup helper and `entry` fixture | ~36 | 1.1 | 33 | low |
| D15 | `gen_features` tables | ~18 | 0.5 | 36 | low |
| D16 | `test_runtime` table | ~18 | 0.5 | 36 | low–med |
| D17 | `test_dispatcher` parametrize pairs | ~22 | 0.7 | 31 | low |
| D18 | `check_release` non-deferred tidy-up | ~31 | 1.2 | 26 | medium |
| D19 | Shared fake registry in `tests/scripts/conftest.py` | ~15 | 1 | 15 | medium |
| | **Total** | **~914** | **~21** | | |

The total is about 7% of the 13.0k test lines.

**D1. Compute `test_registry`'s matrix** (`tests/core/test_registry.py`)
- Replace the 48 hard-coded rows with the computed cross product, a sortedness check and the 4 adapter pairs.
- Still caught: the same assertions, computed.

**D2. Delete 10 tests a real run shows redundant**

| Deleted test | Still caught by |
|---|---|
| z2m `test_turn_on_for.py::test_is_z2m_group_true_for_any_base_topic` | the group publish tests and the base-topic refusal tests |
| `::test_detect_returns_true_for_z2m_identifier` and `::test_detect_returns_false_when_identifier_prefix_does_not_match` | every z2m test, and `test_non_z2m_mqtt_entity_is_refused` |
| `::test_z2m_address_returns_none_when_no_matching_identifier` | the rows in `test_addressing.py` |
| `::test_topic_returns_set_topic_for_z2m_device` | every publish test, which asserts the topic |
| `::test_multiple_devices_each_get_their_own_topic` | `test_device_and_group_each_get_their_own_topic` |
| `::test_light_with_multiple_fields_passes_them_all_to_turn_on` | `test_light_with_brightness…`; or keep it as one row of D10's table |
| hue `test_get_attributes.py::test_query_raises_when_any_target_fails` | `test_empty_room_and_missing_light_fail` (the same not-on-bridge line), and core `test_dispatcher.py` |
| `test_config_flow.py::test_entry_loads_and_unloads` | `test_runtime::test_unloading_the_entry_leaves_a_must_run_job_to_run` (asserts the unload) and `test_saving_the_option…` (asserts LOADED) |
| `test_check_release.py::test_a_failing_record_of_an_experimental_or_unproven_adapter_is_exempt` | `test_an_experimental_adapter_is_exempt` and `test_an_adapter_with_no_verified_call_needs_no_record[reported]` (the same `continue` in `record_errors`) |

- Coverage: verified unchanged, see Measurements.

**D3. `test_registry` tidy-up**
- Move the 6 in-function imports of `SimpleNamespace` and the fake integration to the top, with a
  `registry_of(*adapters, info=)` helper.
- Merge `test_an_import_that_raises_anything…` into `test_import_failure_warns…` as a parameter.

**D4. `add()` helper in `test_dispatcher`**
- Add `add(hass, fake, *ids, **attrs)` for its 36 consecutive `add_fake_entity` lines.
- Still caught: the same tests.

**D5. Merge raise/lower tests into one moves file per integration**
- Files: `tests/integrations/{hue,zha,zigbee2mqtt,shelly}/test_{raise,lower}_*.py`, 18 files, 898 lines. Fold
  them into each integration's `test_fade.py`, or into a new `test_moves.py` beside it.
- Parametrize across all of an integration's move actions, not only raise/lower pairs. The clones run across
  pairs:
  - "off / at top / at white is unchanged": zha 7 tests, z2m 6.
  - "end point with the time left" plus "cycle in the sweep time": z2m 6.
  - "nothing is sent": hue 6.
- Keep every action as a row, so each adapter class still has its own row.
- About 150 lines come from parametrizing and about 70 from the 18 file headers. Merging pairs only saves about
  half that.
- Tight: `shelly/fade.py` has no margin. Port its tests unchanged, with every row kept: it can lose no line.

**D6. `gen_compat` non-deferred tables**
- Fold `test_an_unknown_key_in_an_entry_is_an_error` and `test_devices_yaml_shapes…` into
  `test_devices_yaml_is_validated`'s table.
- Parametrize the 3 maintained/formerly-maintained heading tests.
- Parametrize the two link-on-another-integration's-page tests.
- Don't build on the `report()` helper: only the deferred level-tier tests use it today.

**D7. Integration setup helpers**
- A one-line helper in `fixtures/shelly.py` for the 55 `shelly_device` + `add_shelly_entity` pairs (~45).
- The same for matter's 11 pairs (~10).
- A z2m `kitchen` fixture for the 14 `z2m_entry` + `add_z2m_entity` pairs (~12).
- Import `SENT`/`CONFIRMED` from each conftest instead of the ~12 local copies (~12).
- Move `setup_production` into `pytestmark` where 27 signatures wrap only to take it (~15).
- Still caught: the same tests.

**D8. Parametrize refusal tests over actions, per integration**

| Integration | What to table | Saves |
|---|---|---|
| shelly | gen1 / not addressable, 6 tests | ~25 |
| zha | non-admin: move `admin_command` and a `call_as(hass, user, service, data)` helper into conftest | ~45 |
| matter | the 8 refusal tests, one table over (action, break) | ~30 |
| hue | v1 bridge refused, 4 tests | ~13 |
| z2m | another base topic / device without a name, 6 tests | ~11 |

- Merge these but delete none. Each adapter's `supports()` has its own refusal line, and only that action's
  refusal test pins it. Deleting a refusal test can show no coverage change and still lose that check.
- Don't fold the zha non-admin tests into one: each ends with an action-specific assertion.

**D9. Fold kept z2m addressing tests into `test_addressing.py`**
- These four stay, because a real run shows coverage drops without them:
  - `test_detect_returns_false_when_device_is_none` (`detect.py:17`)
  - `test_is_z2m_group_false_for_a_device` (`addressing.py:45`)
  - `test_topic_returns_none_when_identifier_does_not_match` (`addressing.py:58`)
  - `test_supports_returns_super_reason_when_scope_refuses` (`turn_on_for.py:68`)
- Move them into `test_addressing.py`, and rewrite the 27-line supports test with a
  `monkeypatch.setattr(..., "scope", replace(...))`, as shelly's tests do.

**D10. Parametrize integration siblings that differ only in data**
- z2m `turn_on_for` publish tests: light or group, with or without fields (~30).
- Duration tests in z2m, shelly and matter (~20).
- z2m `native_control` pairs (~20).
- zha zcl non-admin / unknown user, and hue `set_attributes` gamut (~10).

**D11. Small core parametrizations**
- `test_ha_entity`'s three `replace_if_set` tests.
- `test_zcl_row`'s mode tests.
- `test_actions_setup`'s three set-equality tests.
- The `probatio.Invalid` tests in `tests/actions/test_turn_on_for.py` and `test_auto.py`.

**D12. `wire_off` fixture; scope-trace check into `test_wire`**
- Add a `wire_off` fixture in `tests/conftest.py`, the mirror of `wire`, and use it to replace the hand-rolled
  set-level/try/finally in 3 places.
- Move `test_dispatcher::test_the_scope_trace_is_off…` into `test_wire.py` as a direct `scope_trace` call, beside
  the `wire_trace` check.
- Tight: `core/wire.py` has no margin. Confirm with the real run.

**D13. `test_scope` refusal table**
- Fold the 7 `pytest.raises(ValueError, match=…)` refusals into one table.
- Hold this item until the evidence-rule simplification settles. If it drops sibling rules, several rows go
  with it.

**D14. Entry-setup helper and `entry` fixture**
- `setup_integration` and `setup_production` in `tests/conftest.py` share a body. The same entry setup recurs in
  `test_config_flow.py` (`loaded_entry` plus 3 inline copies), `test_ha_watch.py`'s `watched` fixture and
  `test_init.py`. Make one helper for all of them, then an `entry` fixture for the 10
  `await loaded_entry(hass)` calls (~25).
- `test_config_flow.py`:
  - Fold `test_predicted_state_is_on…` into `test_saving_the_option…` with one added assert.
  - Parametrize the z2m and plain-MQTT report tests.
  - Use the `zha` fixture in `test_a_report_for_a_supported_light…`.
  - About 11.

**D15. `gen_features` tables**
- One table for the 4 NO_PATH failure tests.
- One table for the 4 `gf.keys` tests. `test_domains_show_at_any_level…` names levels, so check it against the
  rule simplification first.

**D16. `test_runtime` table**
- Table-drive the 4 cancel_pending/must_run tests.
- Merge `test_ha_stopping…` and `test_unloading_the_entry…` as parameters.
- Keep every final-state assert.

**D17. `test_dispatcher` parametrize pairs**
- `test_response_shape` with `test_unchanged_everywhere…`.
- `test_no_targets_left…` with `test_an_empty_selection…`.
- `test_a_device_failure_raises…` with `test_an_unauthorized_target…`.
- `test_empty_ha_group_is_left_out` with `test_native_group_expanding_to_nothing…`.

**D18. `check_release` non-deferred tidy-up**
- Add `members.py` as a parameter of `test_a_change_to_the_adapters_code_after_its_record_is_an_error`, and drop
  `test_a_change_to_the_packages_group_helper_stales_the_record`.
- Merge `test_every_record_of_the_adapter_must_pass` into `test_a_fresh_record_that_did_not_pass…`.
- Give the 5 commit→record→commit flag/maintainer tests one helper, and merge the fresh and stale flag tables on
  an `expect_stale` parameter.
- Add a `(repo, sha, registry)` fixture for the repeated `FakeRecordsRegistry(repo, monkeypatch)`.
- Medium risk: keep the exact error strings.
- This touches the binding-rule surface (`scripts/check_release.py`'s tests), so the PR audit includes an Opus
  auditor (deviceside `CLAUDE.md`, Pull requests). Do it after the rule simplification lands.

**D19. Shared fake registry in `tests/scripts/conftest.py`**
- Merge the 3 `FakeRegistry`/`adapter` pairs (check_release, gen_compat, gen_features).
- Medium risk: their `matrix` rows differ.

**Rejected: one shared cross-integration suite.**
- The "off light unchanged" family is about 13 tests and 85 lines. D5 already brings it to about 35.
- A shared suite needs a shim per integration to build an off light and read what was sent (commands, broker,
  bridge puts, RPC calls). The shims cost about 20 lines against about 15 saved.
- Clamp tests are worse: each integration's limits and payloads are the table rows.
- Each integration's package also has its own maintainers, so one shared file would cross them.

### Rig

| # | Item | Saves | Hours | Lines/h | Risk |
|---|---|---|---|---|---|
| R1 | Remove 2 tests, fold 6 into R3/R4's tables | ~20 | 0.5 | 40 | low |
| R2 | Parametrize `test_rig.py` | ~65 | 2 | 33 | low |
| R3 | Parametrize engine clusters | ~95 | 3 | 32 | low |
| R4 | Step-file builder in `rig_fakes.py` | ~100 | 3 | 33 | medium |
| R5 | One shared `scripts.rig` import | ~12 | 0.5 | 24 | low |
| R6 | Fakes and fixtures into `rig_fakes.py`/`conftest.py` | ~15 | 1 | 15 | low |
| | **Total** | **~307** | **~10** | | |

The total is about 6% of the 5.4k non-deferred test lines, 3% of all 10.3k. Most of the rig's test code is in
deferred subjects (below).

**R1. Remove 2 tests; fold 6 into tables**
- A ctrace run shows dropping all eight loses no covered line.
- Remove `test_the_lifecycle_step_file_loads`. Still caught by `test_rig_steps::test_every_shipped_step_file_parses`;
  its `moves_entry` assert by `test_check_tokens_stops_a_run_with_entry_steps…`.
- Remove `test_profile_entity_by_role`. Still caught by `test_select_plan_lines_and_estimate` and every planning
  test.
- Fold these into R2's and R3's tables as rows rather than deleting them: the cross-action positive test,
  `group_color_is_a_group`, the deviceside settle test, the group_color members test, `one_read_not_clear`,
  `unarmed_watcher`.

**R2. Parametrize `test_rig.py`**
- The 8 profile-refusal tests as one `(yaml, message)` table, plus the group/group_color members pair (~30).
- The 5 watcher tests into the existing table, with `armed`/`clear` columns; keep the capsys message check (~12).
- The 4 `check_tokens` tests as one table (~15).
- The 3 `wait_until_ready` failure tests (~8).
- Optional, no lines saved: move the profile, `load_env` and `main_checkout` tests (~100) to a `test_profile.py`,
  beside the module they test.

**R3. Parametrize engine clusters** (`tests/test_rig_engine.py`)
- No test below is the sole coverer of any line (ctrace).
- The 4 `expect_response` tests: query response, dotted name, numeric index, null (~40).
- The 6 bridge-step failure tests (~15).
- EOF at brief / do / question, plus do-waits (~13).
- The options and entry-steps admin-token pair (~10).
- Option schema and `applies` (~8).
- The steady-settle pair (~8).
- The with-adapter / without-adapter cross-action pair (~4).

**R4. Step-file builder in `rig_fakes.py`**
- The non-deferred engine tests carry 25 inline step-file YAML strings, 303 lines, each repeating the same
  6-line header.
- Add a `steps_yaml(action, role, *steps)` helper. Keep the shared texts that tests `.replace()` on.
- Medium risk: the fixtures read less like real step files. `test_rig_steps` keeps parsing of the shipped files
  pinned.
- Still caught: the same tests.

**R5. One shared `scripts.rig` import**
- Replace the per-file `importlib` loads in `test_rig.py`, `test_rig_runlogs.py` and `test_terms.py` (for
  `check_private`) with `from scripts import rig` / `from scripts import check_private`. `test_rig_roam` already
  does the latter.
- The distinct module names don't matter:
  - `rig.py` has no module-level mutable state, only constants, `REPO` and its classes.
  - Nothing depends on `__name__` or the logger name.
  - All 191 patches go through `monkeypatch`, which undoes them per test.
- Leave the loads in `test_rig_lanes.py`, `test_rig_locks.py` and `test_rig_roam_run.py`: they are deferred.

**R6. Fakes and fixtures**
- A no-op `FakeHA.close`, which drops `RigHA` and `LoggedHA`.
- A `connected(monkeypatch, ha)` helper for the repeated `rig.connect` patch.
- `reports_in_tmp` into `conftest.py`, after R5.
- Move engine's `suite`/`outcomes`/`header`/`REGISTRY`/`TOLERANCE` into `rig_fakes.py`: `test_rig_busy` imports
  them from `test_rig_engine` today.
- Rename the engine tests' local `FakeBridge`, which clashes with `rig_fakes.FakeBridge`.

## Deferred: subject may be removed

These tests are left as they are while the release/evidence rules are simplified. Consolidate them only for
subjects that survive.

**deviceside: ~487 lines**

| Subject | Lines | Where |
|---|---|---|
| `check_release.py`: shared-code clause proof | 232 | tests 195 + helpers used only by them 37 (`SharedRegistry`, `covering_record`, `shared`, `stale_message`, `checked`, …) |
| `check_release.py`: retests via report and stale_tests | 119 | `_manual.md` lines 34, accepted reports 35, stale_tests 21, retest base 29 |
| `gen_compat.py`: level tiers | 136 | 14 tests + `report()` |

Mixed tests outside those totals, which need edits if their subjects go:
- stale_tests rows in `test_check_release.py`'s flag tests (about 3 lines).
- gen_compat stale-tests marks: `test_a_card_marks_stale_tests…` 14, `test_an_evidence_row_shows_both_marks` 6.
- The `stale_tests` parameters in `tests/test_init.py`.
- `test_a_card_leaves_out_flagged_adapters_and_failing_records` and
  `test_every_evidence_row_of_an_unmaintained_adapter_is_marked`, 9 lines each.
- If tier removal reaches `core/`: `test_scope.py`'s sibling tests and
  `test_dispatcher.py::test_every_level_above_experimental_passes_with_the_option_off`.

**rig: ~4,842 lines**

| Subject | Lines | Where |
|---|---|---|
| roaming | 2,124 | `test_rig_roam` 751, `test_rig_roam_run` 866, `test_rig_groups` 316 (its groups live in `roam.py`), `rig_fakes` `Lab`/`lab_profile`/`LAB_*` 152, `test_rig_busy` group availability 35, a runlogs roaming test 4 |
| locks | 909 | `test_rig_locks` 890, `test_rig.py` deploy-only lock tests 19 |
| progress / resume | 482 | `test_progress` 147, `test_rig.py` stop/resume 195, engine resume and budget 125, busy 15 |
| observed lending | 349 | `test_observed` 146, engine observed-lent section 138, `test_rig.py` observed rows 47, `test_coverage` lend hint 18 |
| lanes | 348 | `test_rig_lanes` 324, `test_rig.py` recover-lanes tests 24 |
| staging | 256 | `test_staging` 75, `test_rig.py` staging tests and helpers 181 |
| baseline holders | 234 | `test_rig_baseline` 217, `test_rig.py` 10, runlogs 7 |
| firmware-fact ledger | 140 | `test_progress` 77, engine 29, `test_coverage` 24, `test_rig_steps` 10 |

Notes on the rig's deferred tests:
- About 74 roam-only lines in `FakeHA` are not counted above: `join`, `register`, `unregister`, `_leave`, the
  `_z2m_*` helpers.
- Inside `test_rig_locks`, the journals/orphans/recover-auth section (180) and the `--hold`/probe-auth section
  (185) test `journal.py` and the authorization gate through locks. Rescue them into their own file if locks go.
- `DebugLevels` goes through `Baseline`, so the runlogs debug tests change if baseline holders go.

## Order

1. deviceside D1–D4: about 2 h, about 150 lines.
2. deviceside D5, D7–D10, one PR per integration package, so each needs only its maintainer's approval. Take
   shelly first: its `fade.py` has no coverage margin.
3. rig R1–R3 and R5, then R4 and R6.
4. After the rule simplification lands: deviceside D13, D15, D18, D19, and any surviving deferred subjects.

Each PR states in its body the `missing_lines` diff against `main`: empty.
