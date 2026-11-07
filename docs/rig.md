# The rig

The rig runs DeviceSide's actions against a live Home Assistant and its real devices, and writes the test records
that clear an adapter's `experimental` flag. Its tests are YAML step files in `ha_rig/steps/`, one per action.
`scripts/rig.py` runs each file against every integration in the profile that has an adapter for the action, after
an authorization gate (below): the devices change state, so nothing moves until the work is authorized and, at a
terminal, you say yes. `--dry-run` prints the plan and the decision, and stops.

## Setup

The rig lives here, apart from deviceside, which is public. It tests a deviceside checkout: the one you run it
from, so each worktree tests its own code, or `DEVICE_SIDE_REPO`, else a `deviceside` (or `device-side`) checkout
beside this one. Run it from the deviceside worktree under test:

```sh
cd ~/src/deviceside            # or a linked worktree of it
~/src/ha-rig/scripts/rig.py --dry-run
```

`uv sync` here installs what the rig's own tests need (`.venv/bin/pytest`, which import the sibling checkout). A run
needs what deviceside's environment already has: the shebang's `uv run` uses the project of the directory it is run
from.

The rig keeps the profiles and `.env.rig` in this repo's main checkout, and its locks, journals and baseline in the
deviceside checkout's git common dir, shared by every worktree of it. It writes its test records into the
deviceside checkout, to commit there. On every invocation it also writes the household's terms, every profile's
entity IDs and IEEEs and `.env.rig`'s HA and Hue bridge hosts, tokens, Hue app key and restricted sensor, to
`.private-terms.rig` in deviceside's main checkout (gitignored), where deviceside's household check reads them;
`python -m ha_rig.terms` does it by hand after editing a profile. A run refuses while `.env.rig` or `tests/rig/profiles/` is still in deviceside's main checkout, where the
rig kept them before it moved: move them here.

## Before a run

The run first checks every device it will use: each fixed entity must be in HA, on its integration and available, and each roaming bulb
must be on a network. An unpowered device stops the run there, before anything moves. `--check` runs only this
check, taking no lock and deploying nothing, so you can power devices up until it passes; it names the runs holding
locks it would need. The check, and the wait for devices after a deploy, cover only the cases the run will run, so a
run that completes a suite needs only the devices its missing cases use.

**`--ignore-unreachables`** runs what the devices that are up allow, so testing needn't wait for the whole rig. An
unavailable fixed entity no longer stops the run: its cases skip as `<entity> unreachable`, and so do those of a group
it is a member of or a device it powers. While anything is offline, cases on a fixed group whose `members` the
profile doesn't list skip too (`group: members not listed`), and so do those on fixed scenes (`scene: contents
unknown`), since the rig can't tell which devices they hold. An entity not in HA or on the wrong platform still stops
the run, and so does an offline roaming bulb, which is checked strictly: a bulb that passes counts as up on every
network, so the rig-made groups and scenes stay in scope. The plan lists the skipped cases. Unreachable skips are
never saved, and their suite writes no record until a later run with the devices up runs them: the summary shows it
as `partial: N of M cases; K unreachable`. An entity that was up at the check and drops out after the deploy's
restart still stops the run.

### Lanes

The run starts with one lane per integration, side by side, each holding that integration's cases on fixed entities.
A case stays out of the lanes when its step file sets DeviceSide options or has `entry` steps (either reloads the
integration, cutting off every lane's fades), or when it uses a roaming bulb, a rig-made group or scene, or a powered entity or
its supply. Those run afterwards, one at a time. `--serial` runs every case one at a time: a case that passes with it
and fails in a lane points at radio or HA contention.

### Profiles

A profile is one test setup on the Home Assistant `.env.rig` connects to: `profiles/<name>.yaml`,
gitignored, copied from `ha_rig/profile.example.yaml`. It lists the entities the rig may move, by integration and
role, and under `pause:` the automations and switches turned off for the run and back on after it (motion lighting,
an Adaptive Lighting switch). `.env.rig` (from `ha_rig/env.example`) holds what belongs to the HA instance: its
address, tokens, deploy commands and Hue bridge.

`--profile <name>` picks one, and can be left out while there is only one. Every test record names its profile, and
records from different profiles of the same suite overwrite each other. A record is public, so name a profile for its
setup (`lab`, `house`), never for a person or place. The household checks, deviceside's (through
`.private-terms.rig`) and this repo's (`scripts/check_private.py`), keep every profile's entity IDs, members, supplies,
pause list and IEEEs out of commits.

### Token

Make a Home Assistant user without admin rights and put its long-lived token in `HA_TOKEN`. The rig only calls
actions and reads states and registries, and a non-admin token can't change your configuration if it leaks.

Some suites need an admin token in `HA_ADMIN_TOKEN`: ZHA (its cluster command is admin-only, and the adapter passes
the caller's context through), Zigbee2MQTT (setting device options over MQTT), suites whose adapter has options
(to set them), `lifecycle` (reloading and disabling DeviceSide's entry), and roaming. Without it, a run that
includes them stops before the gate; `--integration` or `--action` leaves them out.

### Authorization

The rig classifies the work, not who runs it (`ha_rig/authorize.py`):

- **Unrestricted** work goes at any time, whatever the sensor reads: a test run or `--recover` on a profile with
  `pre_authorized: true` (it covers everything that run does: pauses, DeviceSide reloads and disables, roaming, HA
  restarts); `--deploy-only`, as HA restarts are always allowed; a probe declared `--visual none`, or
  `--visual profile` on a pre-authorized profile.
- **Restricted** work is everything else. It goes when either `RIG_RESTRICTED_ENTITY` in `.env.rig`, an HA
  binary_sensor that is on while someone might see the lights move, is off, or Jason has approved it:
  `DEVICE_SIDE_RIG_RESTRICTED=1`, or a y at the terminal's `Restricted: <why>. Proceed anyway?`. With the key unset,
  restricted work needs the approval. `RIG_BLOCKER_ENTITY`, the key's old name, is still read, with a note.

At a terminal, every run asks `Proceed?` as a readiness check; an agent's call has no terminal, so it is never asked,
and unrestricted work needs no variable. A restricted run or probe reads the sensor every 15 s and aborts, putting the
devices back, when it turns on: two reads in a row of on, unknown or unavailable after a read of off. An approved run
started while the sensor was on goes on until it goes off and on again. `DEVICE_SIDE_RIG_APPROVED`, the variable this
replaced, is refused.

An aborted run's changes are restricted by the profile that left it, and the journal from before per-run journals
always is. `--recover`, and the replay before a run's gate, put back what the rig may now (never asking) and name the
rest: `left <journal> (profile <name>, restricted): sensor off or DEVICE_SIDE_RIG_RESTRICTED=1 restores it`. A run
whose locks cover one it can't put back stops before its gate rather than test on top of it.

### Deploys

Each run deploys the deviceside checkout's committed, pushed branch and restarts HA whenever HA hasn't loaded that
tree, so a run queued behind another tests its own code. A record cites the commit HA loaded, read before the tests, so
committing mid-run is safe. `--no-deploy` tests whatever HA runs and, unless that is your checkout's tree, writes no
record.

`scripts/rig.py --deploy-only` deploys the branch and restarts HA with nothing else: no profile and no tests. It
moves no device, and HA restarts are always allowed, so it is unrestricted. It holds HA exclusively, with no integration, until HA is back, then releases the locks, so
you can try a branch's non-device features by hand. When HA already runs the checkout's tree it does nothing;
`--dry-run` prints the deploy state and stops.

## Shared state

- **Roaming.** A profile's `roam:` lists bulbs the run moves between Zigbee networks (Hue, ZHA, Z2M), so one set of
  bulbs serves every Zigbee suite, and `groups:` native groups the run makes on each network. They end home on Hue
  as they were. `--recover`, or the next run, brings them home after a crash; `--roam-check` runs the moves with no
  suites. Roaming also needs `HUE_BRIDGE_HOST` and `HUE_APP_KEY`. How: `ha_rig/roam.py`.
- **Home record.** What a roaming bulb was before its first move is kept until it is back as it was, across crashes
  (`ha_rig/home.py`).
- **Journal.** Each run journals what it changes, so Ctrl-C, a signal, or the next run after a crash puts the devices
  back, DeviceSide's options, a disabled entry and a Shelly device's persistent auto-off / auto-on included;
  `--recover` puts back everything (`ha_rig/journal.py`). One Ctrl-C or signal, even mid-move, still brings the
  roaming bulbs home before the run stops; one that can't exits 1, saying so. A second leaves them where they are.
- **Baseline.** Changes beyond the devices (paused automations, log levels, Z2M device options) are shared by runs
  side by side, and the last one out puts them back (`ha_rig/baseline.py`).
- **Locks.** Runs share the rig through one lock per integration and one on HA, so runs on different integrations go
  side by side (`ha_rig/locks.py`). A run takes all its locks at once and holds none while it waits. A deploy holds
  HA exclusively, and a run with no option suites steps down to shared once HA is back. The plan prints a run's
  locks. `--wait-free` waits until the rig is free, or with `--integration`, until those integrations are, without
  taking a lock.

A probe takes the same locks through `--hold`, which runs a command while holding them and returns its exit code:
`scripts/rig.py --hold zha -- <command>`, with `--exclusive` for one that restarts HA or reloads DeviceSide. It
writes no journal, so the probe puts back what it changes. Once it holds its locks it puts back what aborted runs
left changed under them, as a run's replay does and never asking; an orphan it may not put back (restricted, with the
sensor on) is named in a warning, with its journal and profile, and the probe starts anyway. A probe is restricted unless it declares, trusted, what it
changes that anyone could see: `--visual none`, nothing, or `--visual profile`, only what a pre-authorized profile
covers. `--dry-run` prints whether it may go and runs nothing.

### Coverage, stopping and resuming

The rig treats the full-suite run before an RC as **coverage**: every test has a result at code unchanged since,
built up over any number of runs. A suite's code is that of each adapter its tests call on its integration (its own
action's, and those its `call` steps name), as deviceside's `check_release.py --records` defines an adapter's code:
`core/` and `actions/` don't count, and nor does HA's version. Editing a suite's step file counts too. How:
`ha_rig/coverage.py`.

A suite is **covered** when its record (`docs/test-records/<integration>/<suite>.md`) cites a commit at which that
code is unchanged and lists exactly the cases the suite plans now, or when the ledger holds a result for every case
it plans. A run leaves a selected suite that is covered out, saying `covered (record <date>, commit <c>), skipped`,
and runs only the cases of the rest with no saved result; a run whose every selected suite is covered says so and
stops. `--fresh` ignores the records and the saved cases of the selected suites and reruns them whole. The selection
(`--integration`, `--action`, `-k`, `--serial`) decides only what runs, so a `-k` run's cases count towards their
suite's record.

**`--coverage`** prints each selected suite's coverage at the checkout's HEAD, as `--check` does the devices: no
lock, no deploy, nothing written. A suite is `covered`, `partial N/M` with its missing cases and the entities they
need, or `uncovered`, with its record stale (naming the files changed since) or missing. It ends with the total and
every device the missing cases need: what to power up for the next run. With no selection it covers the whole
profile, which is the RC view, and exits 0 only when every suite is covered.

**The ledger.** A test run saves each case the moment it finishes, with the facts (HA version, devices) its suite's
parts took, in `deviceside-rig-progress/<profile>.json` in deviceside's git common dir (`ha_rig/progress.py`). Its
entries are per suite, keyed by the suite's code at the commit tested, so runs from worktrees with different code
sit side by side, and a run on one integration never discards another's. A saved case holds while it planned the
same entities (a profile change reruns it), and a `not observed` one only for an `--unattended` run. An entry goes
when HA runs another version (a record names one), when its suite's record is written (the record carries it from
then on), when the run that saved a failure of one of its cases ends (the suite reruns whole, so a record never
covers up a failure; the run's report keeps it), or after 30 days untouched. `--no-deploy` saves nothing, as it
writes no record.

A run that stops part way puts everything back as always and exits 75; rerun it and it picks up at the cases with no
saved result, planning, locking, moving and estimating only what is left, and printing `Resuming: N of M cases
saved`. So a 45-minute suite completes as 30 minutes one evening and 15 the next.

- **`--for DURATION`** (`30m`, `90s`, `1h15m`): once spent, no case or phase starts; cases in flight finish, the bulbs
  go home, and the run exits 75. The window counts from approval, so the deploy's restart eats into it. A move to
  another network starts only with a minute left, and always runs its phase's first device case, whatever the budget.
  Ctrl-C, SIGTERM and the restricted sensor still stop at once: the cases in flight are
  cancelled and rerun on resume.
- **Records.** The report joins the saved cases to the session's, so a suite finished across sessions writes its
  record, dated the finishing session and citing its commit. Its cases may have run at earlier commits, all with the
  same code for the suite, which is what deviceside's gate checks.
- **Not covered by a record:** a step-file edit that keeps every case ID. The record doesn't name the step file's
  version, so such an edit reruns a suite only while it is in the ledger; use `--fresh` to rerun a recorded one.

## Writing a step file

```yaml
action: turn_on_for        # the DeviceSide action every test in the file calls
rig_seconds: 25            # the gate's duration estimate
tests:
  - name: on_then_off
    roles: [switch_plug, light_onoff]   # one run per role the profile names
    steps:
      - set: {state: off}
      - call: {duration: 6}
      - expect_response: {status: expected, integration: self}
      - expect_state: {state: on, within: tol}
      - wait: duration - tol
      - expect_state: {state: off, within: 2 * tol}
```

| Step | Does |
|---|---|
| `set` | Puts the target in a known state with plain HA actions (`state`, plus light attributes). |
| `call` | Calls the file's action with these fields; `duration` is sent in seconds. |
| `expect_response` | Checks the call's response: `status` (`expected` means `confirmed` where the adapter can confirm, else `sent`), `integration` (`self` is the one under test), `detail`, `clamped_to`, `attributes`. |
| `expect_error` | The call before it must fail, with this `key` and/or `summary`. |
| `expect_state` | Checks state and attributes now, or `within` seconds. `unchanged` is the state before the test. A `{min, max}` range fits values that drift. On a `group` it checks every member. |
| `expect_matches_read` | Checks that HA's state of each of `attributes` (dotted: `xy_color.0`) is within `tolerance` of a live `deviceside.get_attributes` read, now or `within` seconds. On a `group` it checks every member. |
| `wait` | Sleeps. |
| `expect_no_reload` | Checks the target doesn't go unavailable (its device's entry reloading) for `for` seconds: after a call that should have written nothing. |
| `sample` | Polls `attribute` (dotted: `hs_color.1`) for `for` seconds and expects it `rising`, `falling` or `steady` (brightness may spread one Zigbee level step, 1/254, as a bulb settles after a stop), optionally within `min`/`max`. `source: deviceside` reads `deviceside.get_attributes` instead of HA's state, in its units (brightness in percent), for a fade HA shows only the end of. |
| `mark_hue` | Notes each light's hue on its own wheel, for `expect_hue_travel`. On ZHA it is read live off the bulb (EnhancedCurrentHue), since HA works its hue out from the bulb's xy, which departs from the bulb's wheel by 25° and more in the greens; elsewhere it is HA's `hs_color`. On a `group` it marks every member. |
| `expect_hue_travel` | Checks each light has turned `by` degrees round its own wheel since `mark_hue` (negative: down), within `tolerance`, the shorter way round. A hue move's rate is in the bulb's units, so this holds on any gamut. |
| `do` | Prints an instruction for the person and waits for Enter. Only in the `setup` of an observed test. |
| `restore` | Puts back the state from before the test. It also runs after every test, pass or fail. |
| `entry` | `reload`, `disable` or `enable` DeviceSide's config entry, and waits until it has. A test that leaves it disabled has it enabled when it ends, pass or fail, before its restore. |
| `bridge` | Hue only: writes to the Hue bridge directly, bypassing Home Assistant, as another actor (the Hue app) would. `put: {...}` sends that CLIP v2 body to the target's light (a `group` target's grouped_light); `recall: true` recalls the `scene` target. In an observed run it runs as written. It needs `HUE_BRIDGE_HOST` and `HUE_APP_KEY` in `.env.rig`, and fails in any other integration's suite. |
| `z2m_option` | Sets Zigbee2MQTT device options on the target (`hue_native_control: false`) and waits for Z2M's ok. Does nothing in another integration's suite. |
| `expect_z2m_option` | Expects the target's Zigbee2MQTT device options to hold the given values. Does nothing in another integration's suite. |

Any step takes `target: <role>` to act on another role the test lists; a `group` case that targets a light role is
skipped where the profile lists the group's `members` and that light isn't one. Durations accept numbers, `duration`
(the last call's), `tol` (the integration's timing tolerance) and `+ - *`. `only: [<integration>]` limits a test to
some integrations; when those pile up, split the file.

The roles are listed in `ha_rig/profile.example.yaml`. `group_color` is a native group whose members all take
color: color moves groupcast only to a group like that, so it runs the color cases a `group` can't. Everything this
page says of a `group` holds for it too.

A file with `entry` steps (`lifecycle.yaml`) tests DeviceSide's config entry through its action: reloading it, and
the action refusing with `not_loaded` while it is disabled. It runs alone, holding HA exclusively, like an option
file, and needs a top-level `name:`, which its record takes (`lifecycle.md`) and `--action` picks it by, so it never
replaces the action's own.

An adapter with options runs each file for its action with them pinned through DeviceSide's options flow:
`options: [<name>]` at the top of a file turns those on, and every other option in the adapter's scope runs off. Its
record is `<action>-<name>.md`, with an `Options` row. The rig's own options are put back when the run ends.

A test that means to make DeviceSide warn lists the line under `expected_log:` at the top of its file: regular
expressions, each searched for in the whole log line, logger included. A matched line is counted in the summary and
fails nothing. Keep a pattern as narrow as the warning it expects.

```yaml
expected_log:
  - 'core\.dispatcher\] turn_on_for on zha timed out'
```

An integration in `SETTLE_S` (`ha_rig/profile.py`) predicts a light's brightness for a while after a raise, lower
or `stop`, so a later test on that light waits until it reads the device again.

An action in `ENTRY_RELOADS` (`ha_rig/profile.py`: Shelly's `set_`/`clear_auto_off`/`_on`) makes core reload the
device's entry a while after it writes, so its entities go unavailable and come back. After a call that wrote
(`confirmed` or `sent`), the engine watches for that reload, and the next `call` or `set` on the light, the restore
and the test's end wait it out; a reload that never comes fails the test. Checks don't wait, so a countdown can be
watched meanwhile. A test calling one of these actions has its light's auto config read first and put back, before
its on/off.

### Observed tests

Some things need eyes, such as a light climbing smoothly without flashing. A test asks for them with an `observe:`
block and puts its start state in `setup:`. Each question costs the person at the rig, so observe only what hardware
has never shown and a check can't measure: once watched on an integration, a behavior stays automated there.

```yaml
  - name: raise
    roles: [light_ct, group]
    setup:
      - set: {state: on, brightness_pct: 20}
    steps:
      - call: {sweep_time: 5}
      - sample: {attribute: brightness, for: 3, expect: rising}
    observe:
      see: The light at 20 %, climbing to full over 5 s.
      watch: Steps or flashes on the way up.
      ask: Did it climb smoothly to full?
```

Such a test runs twice, so you are asked only what a machine can't check:

1. **Automated**, with every other test first: setup and steps, every check applied. It has no automated run if its
   steps check nothing or its setup has a `do`.
2. **Observed** (`<name>[<role>] observed`), after all automated runs. Setup runs and settles for 2 s, then the runner
   prints `see` (in bold) and `watch` and waits for Enter. The steps run with their checks turned into waits, so the
   timing matches. Then you answer `ask`: Enter or `y` if you saw it, `n` if not, which fails the test and asks what
   you saw instead. It is skipped if its automated run failed, and if a step fails after Enter it prints
   `nothing to observe` and the reason.

`roles: [<role>]` in `observe:` limits the observed run to those roles, and `only: [<integration>]` to those
integrations; the rest run automated only. `--unattended` skips the observed runs and reports them `not observed`.
They don't fail, but a test counts toward a test record only when both of its runs pass.

## After a run

### Run reports

Every run, failed, partial or unattended, ends by writing a report to `deviceside-rig/` in the system temp folder and
printing its path: each suite's results with the reason for every failure and skip.

### Run logs

Z2M's frontend keeps only its last 1000 lines and HA's log restarts with HA, so a run captures both: Z2M's lines (for
a run that touches Z2M, or roams) to `<report>.z2m.log` as they arrive, and HA's log from the run's start to
`<report>.ha.log` when the run ends. A clean run deletes them; after a failure, a stopped move or a crash they stay
and their paths are printed. When Z2M's coordinator crashed during the run (`ASH_NCP_FATAL_ERROR` or
`Adapter disconnected`), the summary says so with each crash's time, and the logs stay even after a clean run.

A run also reads HA's log for `WARNING`, `ERROR` and `CRITICAL` lines from `custom_components.deviceside`. Each one
no step file of the run expects is listed in the summary, keeps the logs and fails the run, so a run that ends clean
also proves DeviceSide logged nothing amiss; the test records it wrote stand. Another run on the same HA meanwhile
can put lines in, so read whose they are before chasing one. A log with no line dated from the run's start (HA
logging in another time zone) is kept whole and not checked, which the summary says.

`--debug-logs` keeps them whatever the outcome and runs HA's `deviceside`, each integration the run touches and Z2M
at debug until the run ends. It costs CPU, so timing cases may read differently. Z2M's debug lines reach the capture
only with `advanced.log_debug_to_mqtt_frontend: true` in its configuration; the brief warns when it is off, and the
rig never sets it.

### Test records

When every test of an adapter passes, the run writes `docs/test-records/<integration>/<action>.md` in the deviceside
checkout: the date, the Home Assistant version, the devices' model and firmware, and each test's result. A failed
suite, or one with a case not yet run (left out by `-k`, unreachable, or cut short), writes nothing until a later run
completes it (Coverage, above). The PR that clears an adapter's `experimental` flag cites one, and
`cut-release-candidate` and `cut-release` refuse a head where a released adapter's code (its module and the modules it
imports from its integration package) differs from the commit one of its records cites: deviceside's
`scripts/check_release.py --records`. The record's Source row names `scripts/rig.py` and the deviceside commit HA
loaded, never one of this repo's. So every adapter changed since its last run needs a new run before the next RC:
`scripts/rig.py --coverage` lists them.
