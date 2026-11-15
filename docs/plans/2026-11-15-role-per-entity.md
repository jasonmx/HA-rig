# A plug role's cases run once per listed plug

## Coordination

Intent: a profile that lists several `switch_plug` entities on one integration tests each of them, one case per plug,
so one run gives each device (each Tuya maker string) its own record test.
Touches: ha_rig/profile.py, ha_rig/engine.py (Case, cases, narrows), scripts/rig.py (plan_lines, the authorization
gate, journal marks, probes), ha_rig/profile.example.yaml, docs/rig.md, tests/
Tests-with: DeviceSide/DeviceSide#440
Pairs with device-classes step 5 part 3, the Z2M Tuya countdown `turn_on_for`: its lab run needs one test per maker
string (deviceside's device-classes plan, step 5, item 9).

## Why

`Profile.entity(integration, role)` returns the first entity with the role, so a profile of three `switch_plug` plugs
tests one. Part 3's lab run needs one TS011F per maker string tested, each a test reaching its own device, so its
record test names that device's `manufacturer` and `class`.

## Scope: `switch_plug` only

The per-entity roles are a set, `EACH_ENTITY` in `ha_rig/profile.py`, holding `switch_plug` alone. Light roles stay
first-entity-wins on purpose: a roaming bulb is listed under every network it visits beside that network's fixed
lights (`Profile.add_roaming`), so today's lab profiles already hold several entities per light role and test the
first. Expanding those would multiply the lab's Zigbee cases and change its records, unasked. Group roles are one per
network already (`roam_section`, `add_roaming`), and `scene` has a single fixed use. A role joins the set when a run
needs it, with its test here.

A role a case only reaches through `target:` (a test's other roles) still takes the first entity: the per-entity cases
are of the case's own role.

## Shape

- `Profile.entities(integration, role)`: every entity with the role, in profile order. `entity()` stays the first.
- `cases()`: for a role in `EACH_ENTITY`, one case per entity. `Case.index` is the entity's 1-based place among the
  role's entities. `Case.id` is `<test>[<role>]` for the first, as today, and `<test>[<role>-<n>]` for the n-th
  (`on_then_off[switch_plug-2]`), ` observed` after as before. A profile with one plug plans and records exactly what
  it does now, and adding plugs only adds ids, so the ledger and an earlier record keep the first's.
- `twin` carries the index, so an observed case pairs with its own entity's automated case.
- The record (format 2, deviceside's `docs/test-records.schema.json`): a test per case, `role: switch_plug`, its own
  `domain` and its one device. The schema already allows it: `id` is any string, and `role` is its own field. No
  validation change: DeviceSide#440 updates the schema's `id` description to name the `[<role>-<n>]` form.
- **Never narrows, per entity.** A record test that passed `X[switch_plug-3]` would vanish if a later run of that
  suite lists two plugs. `narrows` counts, beside today's inapplicable skips, every id the record passed from the same
  test, role and kind (automated or observed) as a case this run plans that this run doesn't plan, so the file is
  Kept, as for a role the profile lacks.
- Everything that works per case already covers every listed plug: the check (`--check`, `targets`, `platforms`),
  locks (per integration), the journal and put-back (each case journals and restores its entities), `powered_by` (a
  plug that powers or is powered goes to the phases, `in_head`) and `--ignore-unreachables` (`unreachable` reads the
  case's entities). `plan_lines` lists every plug of the suite.

## One plug left out of a pre-authorized profile (Jason, 2026-11-15)

A record is one file per integration and suite, so part 3's four plugs must run in one profile: the lab's, whose plug
is pre-authorized and stays first (`[switch_plug]` names the same device), while the other three may not move
unrestricted. So an entity takes `pre_authorized: false`, which leaves it out of a pre-authorized profile:

- `RigEntity.pre_authorized`; `Profile.restricted` and `Profile.restricts(entities)`, which adds each one's
  `powered_by` supply. An entity takes only `false`: `true` on an entity is refused, as it can't widen its profile.
- The run's gate (`run_held`): restricted when the profile isn't pre-authorized, or when `targets` (every case the run
  will run, members included; saved and unreachable cases left out) or their supplies hold a restricted entity. The
  plan's Authorization line names them. Otherwise unchanged.
- The journal: a restricted run writes `restricted: true` beside `profile`, and `Authority.withholds` holds a marked
  journal to the sensor or the approval whatever its profile, so `--recover` and the replay put it back as a
  restricted profile's.
- A `--visual profile` probe is restricted when an integration it holds lists a restricted entity (or one powered
  through it).

## Tests

- Profile: `entities()` order; `entity()` the first.
- `cases`: three plugs give three cases per `switch_plug` test, ids `[switch_plug]`, `[switch_plug-2]`,
  `[switch_plug-3]`, each with its own entity; one plug gives today's single case; a light role with two entities still
  gives one case on the first; observed twins pair by index; `-k switch_plug-2` keeps that one.
- An engine run on a profile of two plugs: each plug driven and restored, each case its own result, the format 2 record
  a test per plug with its own device.
- `narrows`: a record that passed `[switch_plug-3]` and a run with two plugs is Kept; one with three is written.
- `--check` / `targets`: every listed plug; `powered_by` on the second plug takes its case out of the lanes; an offline
  second plug skips only its own case.
- `plan_lines` names every plug.
- Left out: the profile rule both ways, a supply; the run's gate restricted (sensor on: refused; off: goes, watched)
  and unrestricted (`-k` on the first plug, another integration, the entity unreachable); the journal mark; `--recover`
  holding a marked journal of a pre-authorized profile to the sensor; `--visual profile` probes on and off the
  integration.

## Verification

1. `.venv/bin/pytest`, `pre-commit run --all-files`, `/audit-code` on the diff, then the draft PR (CLAUDE.md).
2. Live, Jason, with part 3: the `plugs` profile's `--dry-run` lists a case per plug.
