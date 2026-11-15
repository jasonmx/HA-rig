# The rig

The rig runs DeviceSide's actions against a live Home Assistant and its real devices, and writes the test records
that sign off an integration's changes (deviceside's `docs/evidence.md`). Its tests are deviceside's YAML step files
in the checkout under test, shared ones in `tests/rig/steps/` and an integration's own in `tests/integrations/<x>/rig/`:
deviceside owns the test plan, the rig runs it.
`scripts/rig.py` runs each file against every integration in the profile that has an adapter for the action, after
an authorization gate (below): the devices change state, so nothing moves until the work is authorized and, at a
terminal, you say yes. `--dry-run` prints the plan and the decision, and stops.

## Setup

The rig lives here, apart from deviceside. It tests a deviceside checkout: the one you run it
from, so each worktree tests its own code, or `DEVICE_SIDE_REPO`, else a `deviceside` (or `device-side`) checkout
beside this one. A run refuses a checkout that is not a worktree on a `wt/*` branch with an open PR (`pr-draft`
opens it). `--check`, `--dry-run` and `--recover` skip that check, so they work from any checkout, while
`HA_RIG_UNOWNED_CHECKOUT=1` opts the rest out, for the main checkout or a CI clone. Run it from the deviceside
worktree under test:

```sh
cd ~/src/deviceside-wt/my-change   # a wt/* worktree with an open PR
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

The rig needs DeviceSide's Experimental features option on in its HA, and never changes it: a flagged item's case
would otherwise test the refusal. The option only lets flagged actions be called; unflagged ones behave the same.

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

A role's cases test the first entity the profile lists with it, on each integration, but for `switch_plug`
(`EACH_ENTITY` in `ha_rig/profile.py`): its cases run once per plug listed, so each device gets its own test and
record test, with its own model, maker string and class. The first plug's case keeps the bare id
(`on_then_off[switch_plug]`), and the n-th is `on_then_off[switch_plug-<n>]`, in profile order, so a profile with
one plug plans what it always did and adding plugs only adds cases (`-k 'switch_plug]'` picks the first's alone).
An entity is listed once per integration. Light roles stay first-entity on purpose: a
roaming bulb is listed beside each network's fixed lights. A role a step reaches only through `target:` takes the
first entity too.

`--profile <name>` picks one, and can be left out while there is only one. Every test record names its profile, and
a record never narrows to a profile: a run whose profile lacks a role (a bulb-only profile has no `switch_plug`), or
whose group lacks the light a case targets, or that lists fewer plugs than the run whose record passed
`<test>[switch_plug-3]`, writes no record over one that passed that case, and says `Kept <record>`. A record is one
file per integration and suite, so two profiles on one integration (one plug each, say) can't both hold their plugs'
tests in it: list every plug the record should hold in one profile. Run a profile with the role, or, to narrow on purpose
(a retired device, say), delete the record by hand in the deviceside PR. A record is public, so name a profile for its
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
  restarts), unless it moves an entity the profile leaves out (below); `--deploy-only`, as HA restarts are always
  allowed; a probe declared `--visual none`, or `--visual profile` on a pre-authorized profile.
- **Restricted** work is everything else. It goes when either `RIG_RESTRICTED_ENTITY` in `.env.rig`, an HA
  binary_sensor that is on while someone might see the lights move, is off, or Jason has approved it:
  `DEVICE_SIDE_RIG_RESTRICTED=1`, or a y at the terminal's `Restricted: <why>. Proceed anyway?`. With the key unset,
  restricted work needs the approval. `RIG_BLOCKER_ENTITY`, the key's old name, is still read, with a note.

At a terminal, every run asks `Proceed?` as a readiness check; an agent's call has no terminal, so it is never asked,
and unrestricted work needs no variable. A restricted run or probe reads the sensor every 15 s and aborts, putting the
devices back, when it turns on: two reads in a row of on, unknown or unavailable after a read of off. An approved run
started while the sensor was on goes on until it goes off and on again. `DEVICE_SIDE_RIG_APPROVED`, the variable this
replaced, is refused.

**One entity left out.** An entity listed with `pre_authorized: false` in a pre-authorized profile is left out of it
(a plug whose light someone might see, say): a run is restricted work, whole, when any case it will run moves that
entity, as its target, a group's listed member or the supply a case draws power through. A run that leaves it alone
(another integration, `-k`, its cases saved or skipped as unreachable) is as unrestricted as before, and the plan's
Authorization line names the entities that make it restricted. A `--visual profile` probe is restricted when an
integration it holds lists one. An entity takes only `pre_authorized: false`: being pre-authorized is the profile's to
say, so `true` on an entity is refused (in a restricted profile, `false` only restates it).

An aborted run's changes are restricted by the profile that left it, and by the run: a restricted run's journal says
so (`restricted`), and its changes are restricted to put back whatever its profile. The journal from before per-run
journals always is. `--recover`, and the replay before a run's gate, put back what the rig may now (never asking) and name the
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
  HA exclusively, and a run with no suite that may reload the entry (one whose adapter has DeviceSide options, or
  whose file moves the entry) steps down to shared once HA is back. The plan prints a run's locks. `--wait-free` waits until the rig is free, or with `--integration`, until those integrations are, without
  taking a lock.

A probe takes the same locks through `--hold`, which runs a command while holding them and returns its exit code:
`scripts/rig.py --hold zha -- <command>`, with `--exclusive` for one that restarts HA or reloads DeviceSide. It
writes no journal, so the probe puts back what it changes. Once it holds its locks it puts back what aborted runs
left changed under them, as a run's replay does and never asking; an orphan it may not put back (restricted, with the
sensor on) is named in a warning, with its journal and profile, and the probe starts anyway. A probe is restricted unless it declares, trusted, what it
changes that anyone could see: `--visual none`, nothing, or `--visual profile`, only what a pre-authorized profile
covers. `--dry-run` prints whether it may go and runs nothing.

### What runs, stopping and resuming

A run runs the suites it is asked for: every one the profile has an adapter for, narrowed by `--integration`,
`--action` and `-k`. What needs testing is deviceside's to say, not the rig's: the rig keeps no record of what is
proven or stale, and a test record in the checkout never leaves a suite out.

**`--signoffs`** runs what deviceside says needs a sign-off: the integrations its `scripts/check_release.py --signoffs
--json` lists, run in the checkout with the interpreter the rig imports it with, in place of `--integration`. A record
sign-off is one whole passing run of the integration at one package tree, so each is run whole, every suite it has, and
`--signoffs` takes no `--action` or `-k`; a suite that fails is rerun at the same commit with `--integration <x>
--action <a>` to complete the run. Those the profile has no entities for are named as needing a manual sign-off
(`_manual.md`), and the run goes on with the rest; when the list is empty, or the profile has none of it, the run says
so and exits 0 before touching HA. The check's exit code says whether the release gate fails, not whether the list is
empty: exit 0 and 1 both carry the list, and the rig points to `scripts/check_release.py --signoffs` in the checkout for
why each needs one. A check that fails (any other exit, or exit 1 with nothing listed) or prints no JSON list stops the
run, before anything moves.

**Fact cases.** A fact case is the watched case of a test with `observe:` (`<name>[<role>] observed`, `[<role>-<n>]` for the n-th plug), or any case of a
`firmware_fact: true` test, its watched one included (Writing a step file, below). It finds a fact, which stands once
written down in the integration's `_facts.md` (Facts, below), so a default run skips it, `skipped` with `fact case: runs
with --facts`. The automated case of a test with `observe:` is an ordinary check of a shipped call, and every default
run runs it, unless the test is a firmware fact. `--facts` adds the fact cases, for when a change or a bug calls for
one; with `--unattended` too, the watched cases are `not observed`, so `--facts --unattended` runs every automated fact.
A skipped case moves nothing. A default run never writes `_facts.md`, so it never erases a fact. A one-off probe (a
bulb's timer that can't be stopped through its bridge, say) is no rig test: it lives in a deviceside research file.

**The ledger.** A test run saves each case the moment it finishes, with the facts (HA version, devices) its suite's
parts took, in `deviceside-rig-progress/<profile>.json` in deviceside's git common dir (`ha_rig/progress.py`). It is
there only to resume a run: its entries are per suite, keyed by the commit (HEAD when the run plans), so a rerun at the
same commit picks up where the last one stopped, and any other starts the suite afresh. The commit covers the step
files, as deviceside's. A saved case holds while it planned the same entities (a profile change reruns it), and a `not
observed` one only for an `--unattended` run. An entry goes when HA runs another version (a record names one), when
its suite's record is promoted into the checkout (Test records, below), when the run that saved a failure of one of its
default cases ends (the suite reruns whole, so a record never covers up a failure; the run's report keeps it; a fact
case's failure is a fact, and drops nothing), or after 30 days untouched. `--fresh` drops the selected suites' saved
cases. `--no-deploy` saves nothing, as it writes no record, and neither does a run whose deploy left HA on another tree
than the one planned (origin moved). A run whose every selected case is saved says so and stops; one whose HEAD moved
while it waited for its locks stops too, to be run again.

**Uncommitted step files.** A record cites the commit, so it must hold the step files that ran. The rig reads HEAD,
then the checkout's step files in both homes, and parses each from the bytes it compares with HEAD's copy: a file
edited, added (tracked or not, ignored or not) or removed, or an edit git is told not to see (assume-unchanged,
skip-worktree), makes them uncommitted, and so does HEAD moving before the deploy state is read. Such a run still goes
ahead, so a step file can be iterated on, but it says so, writes no test record or `_facts.md` row and neither reads
nor saves the ledger: a pass made with an edit that is then reverted would otherwise resume into a record at that
commit. Commit the step files for a record; committing moves HEAD, so nothing saved before the commit resumes after
it.

A run that stops part way puts everything back as always and exits 75; rerun it and it picks up at the cases with no
saved result, planning, locking, moving and estimating only what is left, and printing `Resuming: N of M cases
saved`. So a 45-minute suite completes as 30 minutes one evening and 15 the next.

- **`--for DURATION`** (`30m`, `90s`, `1h15m`): once spent, no case or phase starts; cases in flight finish, the bulbs
  go home, and the run exits 75. The window counts from approval, so the deploy's restart eats into it. A move to
  another network starts only with a minute left, and always runs its phase's first device case, whatever the budget.
  Ctrl-C, SIGTERM and the restricted sensor still stop at once: the cases in flight are
  cancelled and rerun on resume.
- **Records.** The report joins the saved cases to the session's, so a suite finished across sessions writes its
  record, dated the finishing session and citing its commit.

## Writing a step file

A step file lives in deviceside, `tests/rig/steps/<action>.yaml`, and lands in the deviceside PR that adds or changes
the action it tests. It names roles, never entities: deviceside is public, and its household check covers the file.

An integration's own step file, `tests/integrations/<x>/rig/<action>.yaml`, belongs to that integration's maintainers.
Its tests join the shared file for the action (the one with no `options` or `name`), as if each said `only: [<x>]`:
the same suite, record and estimate, run on `<x>` alone; its `expected_log` applies only to the suite on `<x>`. One
with no shared file for its action stands as its own. It
takes no `options` or `name`; an `only:` naming another integration, a test name the shared file already has, or an
action with no adapter on `<x>` fails at load. A repo maintainer may adopt its cases into the shared file.
The rig's CI parses every one against the paired deviceside checkout (`tests/test_rig_steps.py`). A run while one is
uncommitted writes no record (Uncommitted step files, above). The rig's own tests use fixture copies in its
`tests/steps/`, so its CI doesn't depend on deviceside's test plan.

```yaml
action: turn_on_for        # the DeviceSide action every test in the file calls
rig_seconds: 25            # the gate's duration estimate
tests:
  - name: on_then_off
    roles: [switch_plug, light_onoff]   # one run per role the profile names; switch_plug, one per plug
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
| `set` | Puts the target in a known state with plain HA actions (`state`, plus light attributes). In a setup, a group whose member misses the value is set once more before the case fails: a Hue room's member can be left stale, and a fresh command makes it report (deviceside's integration-internals, Tested behavior). |
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
| `zcl` | ZHA only: sends one raw ZCL command to the target through `zha.issue_zigbee_cluster_command`, for probes. `cluster` and `command` are numbers, `params` the command's fields (a mapping, may be empty), `endpoint` defaults to the one in the target's ZHA unique_id. It is a server-cluster command (`cluster_type: in`, `command_type: server`). Fails in any other integration's suite. |
| `z2m_option` | Sets Zigbee2MQTT device options on the target (`hue_native_control: false`) and waits for Z2M's ok. Does nothing in another integration's suite. |
| `expect_z2m_option` | Expects the target's Zigbee2MQTT device options to hold the given values. Does nothing in another integration's suite. |

Any step takes `target: <role>` to act on another role the test lists; a `group` case that targets a light role is
skipped where the profile lists the group's `members` and that light isn't one. Durations accept numbers, `duration`
(the last call's), `tol` (the integration's timing tolerance) and `+ - *`. `only: [<integration>]` limits a test to
some integrations; when those pile up, split the file. A step takes `only` too, beside its kind (`- {wait: 2, only:
[zha]}`), for a step one integration needs and the others would only wait through; it names integrations the test
runs on.

`firmware_fact: true` marks a test that proves how the device behaves (which of two calls wins, the bounds it takes, a
timing it keeps), not a branch of the adapter's code. Its every case is a fact case (What runs, above), and it may also
`observe`: its results, pass or fail, are facts in `_facts.md`, its suite's record lists its cases as `skipped`, and it
runs only with `--facts`. Mark only a test whose pass hangs on the device, not on the call the adapter sends: another
case must prove the call.

The roles are listed in `ha_rig/profile.example.yaml`. `group_color` is a native group whose members all take
color: color moves groupcast only to a group like that, so it runs the color cases a `group` can't. Everything this
page says of a `group` holds for it too.

A file with `entry` steps (`lifecycle.yaml`) tests DeviceSide's config entry through its action: reloading it, and
the action refusing with `not_loaded` while it is disabled. It runs alone, holding HA exclusively, like an option
file, and needs a top-level `name:`, which its record takes (`lifecycle.md`) and `--action` picks it by, so it never
replaces the action's own. A file with no test for an integration (every test `only:` elsewhere, as `lifecycle.yaml`
is for all but Hue) isn't selected there, so the record and the dry-run don't show it.

An adapter with options runs each file for its action with them pinned through DeviceSide's options flow:
`options: [<name>]` at the top of a file turns those on, and every other option in the adapter's scope runs off. Its
record is `<action>-<name>.md`, with an `Options` row. The rig's own options are put back when the run ends. Each
save reloads the entry, so a run pins each set of options once: the suites that share a set run together, rather
than each `<action>` file and its `<action>-<name>` twin in turn.

A flagged item's case tests its call because the rig's HA keeps Experimental features on (Setup). A pass on a flagged
item is evidence a maintainer may use to lift its flag (deviceside's `docs/evidence.md`); a known failure gets
`only:` or leaves the profile.

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

1. **Automated**, with every other test first: setup and steps, every check applied. It runs in every run, as any
   test's does (unless the test is a firmware fact), and its result is the record's. It has no automated run if its
   steps check nothing or its setup has a `do`.
2. **Observed** (`<name>[<role>] observed`, or `[<role>-<n>]`), a fact case, so only with `--facts`, after all automated runs. Setup runs
   and settles for 2 s, then the runner prints `see` (in bold) and `watch` and waits for Enter. The steps run with their
   checks turned into waits, so the timing matches. As each `call` goes out the runner prints, and speaks with macOS
   `say` (when it exists), the action in words (`raise brightness`; a bare `call: {}` speaks the suite's action), so you know when a
   move starts and stops; the speech never delays the call. Then you answer `ask`: Enter or `y` if you saw it, `n` if not, which
   fails the test and asks what you saw instead. It is skipped if its automated run failed, and if a step fails after
   Enter it prints `nothing to observe` and the reason.

`roles: [<role>]` in `observe:` limits the observed run to those roles, and `only: [<integration>]` to those
integrations; the rest run automated only. `--unattended` doesn't ask, and reports each observed case `not observed`.
What you saw is a fact for `_facts.md` (Facts, below), never the record's.

## After a run

### Run reports

Every run, failed, partial or unattended, ends by writing a report to `deviceside-rig/` in the system temp folder and
printing its path: each suite's results with the reason for every failure and skip. What DeviceSide sent is in HA's
log, one DEBUG line per call (`--debug-logs` keeps it).

### Run logs

Z2M's frontend keeps only its last 1000 lines and HA's log restarts with HA, so a run captures both: Z2M's lines (for
a run that touches Z2M, or roams) to `<report>.z2m.log` as they arrive, and HA's log from the run's start to
`<report>.ha.log` when the run ends. A clean run deletes them; after a failure, a stopped move or a crash they stay
and their paths are printed. When Z2M's coordinator crashed during the run (`ASH_NCP_FATAL_ERROR` or
`Adapter disconnected`), the summary says so with each crash's time, and the logs stay even after a clean run.

With `HA_ADMIN_TOKEN`, a Z2M case also watches Z2M's log for the coordinator's BUSY refusals, which Herdsman doesn't
retry on a groupcast. One in a `call` step's window, timed by the rig from the call's send to its answer plus a second
of log lag, means the call may not have reached the lights: the case runs again, once, after 10 s, and a second
refusal leaves it `not run`, never saved. A refused `set` is sent once more.

A run also reads HA's log for `WARNING`, `ERROR` and `CRITICAL` lines from `custom_components.deviceside`. Each one
no step file of the run expects is listed in the summary, keeps the logs and fails the run, so a run that ends clean
also proves DeviceSide logged nothing amiss; the test records it staged stay staged, with the command that promotes
them. Another run on the same HA meanwhile
can put lines in, so read whose they are before chasing one. A log with no line dated from the run's start (HA
logging in another time zone) is kept whole and not checked, which the summary says.

`--debug-logs` keeps them whatever the outcome and runs HA's `deviceside`, each integration the run touches and Z2M
at debug until the run ends. It costs CPU, so timing cases may read differently. Z2M's debug lines reach the capture
only with `advanced.log_debug_to_mqtt_frontend: true` in its configuration; the brief warns when it is off, and the
rig never sets it.

### Test records

When every default case of an adapter that applies passes, the run stages its record, `<suite>.json` (or `.md`, below)
at its path in the checkout, in `<report>.records/` next to its report, and writes it to the integration's records directory in the
deviceside checkout only when the run ends cleanly: exit 0 (no failure, not stopped, the bulbs home, no unexpected
DeviceSide log line) with a result for every case it planned (an unreachable skip isn't one). Any other end, a crash
included, leaves the checkout untouched and prints the one command that promotes the staged records, `scripts/rig.py
--promote <dir>` (any checkout of this repo runs it), which copies them in and takes their suites out of the ledger. It
never overwrites a record that changed since staging: it names it and keeps it staged. A staged suite stays in the
ledger, and the next run at the same commit that ends cleanly, running any case, writes its record too. The staged copy
sits in the temp folder, which the OS may clear: when every selected case is saved there is no such run, and only
`--promote` or `--fresh` writes the record.

**The format follows the checkout.** One with deviceside's record schema, `docs/test-records.schema.json`, gets JSON
format 2, the schema's shape, and no markdown: the rig checks each record and `_facts.json` against that schema, from
the checkout under test, and writes none it refuses, saying why (`ha_rig/schema.py`). It loads the schema before
anything else, `--check` included, so a checkout without `jsonschema` (`uv sync` there adds it) stops the run before
anything moves. `--promote` refuses markdown records staged for a checkout that has since moved to JSON: rerun their
suites. A checkout without the schema, deviceside `main` before its records moved to JSON, gets markdown as below.

A format 2 record holds its integration, action and any options, the date, the commit, the package tree, Home
Assistant's version and a test per case. A test that ran lists the devices it reached, each with its model ID, maker
string, firmware and class. On a Zigbee device the model ID and maker string are the device's own Basic-cluster ones:
Z2M's `bridge/devices` `model_id` and `manufacturer` (HA's registry holds Z2M's product code and vendor), ZHA's
device diagnostics' `model` and `manufacturer` (the registry holds a quirks v2 device's friendly name; zha's
`unk_model` and `unk_manufacturer` read as null), and Hue's registry model ID and manufacturer, which HA takes from the
bridge. Elsewhere they are HA's registry model ID and manufacturer. Either is null where the integration reports none,
and the maker string is never a device name: a Tuya `TS011F` is hundreds of makers' plug. The class is as DeviceSide's
response reported it for the target, a native group's member's from its `member_capabilities`, or null where it reported
none (an integration that names no classes, or a device not placed yet). A response from before that map names a
group's members' class only when they share one. A native group's test (`group`) lists
its members, never the group, and a scene's (`group` too) its group's lights. A skipped test gives its reason, and a fact case reads `fact`. Each case's devices are
read as it ends and saved in the ledger; a case that ran with none noted (the lookup failed, or an older rig saved it)
keeps its suite's record back, and a run at the same commit reruns just that case.

A markdown record holds the date, a `Ran` row (`N passed, M skipped`), the Home Assistant version, the devices' model and
firmware, any `Options`, and each test's result; deviceside reads its commit (the Source row), its Package row (the integration's package tree at that commit, which a sign-off is checked by) and Result. It needs every
default case that applies to pass. The only skips it allows are of fact cases, which it always lists as `skipped`,
whatever they did in the run, and of cases that don't apply here: an action a test calls has no adapter, the profile
lacks the role, or a group lacks the light the case targets. Any other skip, a failure, or a case not yet run (left out
by `-k`, unreachable, or cut short) writes nothing until a later run at the same commit completes it (What runs, above).
A markdown record with fact cases ends with a line pointing to `_facts.md` and, for each, the latest row on file (``last on file
2026-11-12, on Mini (`S3`), firmware `2.0`: fail``, or `none on file`), which only reports what is there. The record's
Source row names `scripts/rig.py` and the deviceside commit HA loaded, never one of this repo's. Whether a record signs
off an integration's changes is deviceside's rule (`docs/evidence.md`, Release gate), not the rig's. A record never
narrows to a profile: a run never overwrites one with a record that skips, as not applying (a role the profile lacks, a
group without the light, no adapter), or doesn't plan (a plug beyond the profile's), a case the record passed
(Profiles, above). Facts live in `_facts.md` (`_facts.json`), never in
a record.

### Facts

`_facts.md`, or `_facts.json` in a checkout whose records are JSON, in each integration's records directory
(`ha_rig/facts.py`), holds what the fact cases found: one row per
firmware fact's automated case that passed or failed, and per watched case a person answered (`pass` if you saw it,
`fail` for a `n`; a step that broke, or nobody there, adds nothing). A row holds the date, the suite (case ids repeat
across suites), the case, its kind, which says how the fact was seen (`automated` or `observed`), the devices it ran on
with their firmware (a group's members included, as a record's Device row names them) and the result. The file is only
ever appended to, a row as each case ends, straight into the checkout, whether or not a record is written and however
the run ends, except while the step files are uncommitted (The ledger, above): a fact stands whatever the code, until evidence contradicts it, and a line the rig can't read stays as it
is. `_facts.json` is rewritten to add a row, with every row already there left as it was; one the rig can't read stays
as it is, and the run prints the row to add by hand. Its rows' devices are as a format 2 record's. Nothing reruns a fact
case by itself, and the rig decides nothing from the rows. Review the diff before committing it.
