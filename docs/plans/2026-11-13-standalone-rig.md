# The rig as a standalone framework, with DeviceSide as its first target

**Status: parked (2026-11-13); refreshed 2026-11-20** against rig `main` ef15d5f and DeviceSide `main` 9158aea8.
DeviceSide v0.1.0 shipped on 2026-11-19, so the first condition holds; the plan waits on the second, an integration
owner wanting the rig. See Before starting.

Starts once DeviceSide v0.1.0 has shipped and an integration owner outside DeviceSide wants to use the rig (Jason,
2026-11-13), so v0.1.0's sign-off runs use today's rig; see Before starting. Builds on the
evidence-rules work on both `main`s (rig `docs/plans/2026-11-12-evidence-rules-rig.md`, DeviceSide
`docs/plans/2026-11-12-evidence-rules.md`), whose principle it carries further: **the rig is a dumb executor**;
DeviceSide decides what needs testing and what a record proves.

## Context

The rig drives a live Home Assistant and its real devices: it runs step-file tests, holds locks for probes, puts back
what it changed, and captures HA's and Z2M's logs. Already done by the evidence-rules work: the step files live in
DeviceSide (`tests/rig/steps/`, and `tests/integrations/<x>/rig/` run on `<x>` alone); the rig keeps no rule about
coverage or staleness (`coverage.py`, `observed.py`, `Covers`, wire and scope taps gone); it no longer loads
`check_release.py`, and takes its sign-off selection from DeviceSide's `scripts/check_release.py --signoffs --reruns`
(a JSON object of each integration's suites to rerun), run as a command (`target.signoffs_needed`). What still ties it
to DeviceSide or to one household:

- **The registry import.** `scripts/rig.py` imports `AdapterRegistry` at load and uses `adapter(action, integration)`,
  `scope.options`, `scope.confirmable`, `matrix()` and `integrations[x].title`. `core/scope.py` imports
  `homeassistant`, so the rig runs only in DeviceSide's environment (design.md's Registry bullet, "import the
  registry without `hass`", is not true of `scope.py`). Every command, a probe included, needs a DeviceSide checkout.
- **DeviceSide constants in the rig:** `target.py` (`COMPONENT`, `STEPS`, `INTEGRATIONS`, `RECORDS`, `SCHEMA`,
  `SIBLINGS`, `SIGNOFFS`), the call-transcript path `tests/integrations/<x>/calls/<action>.json` (`scripts/rig.py`),
  the `deviceside` service domain, `source: deviceside` (`deviceside.get_attributes`), the config entry domain in
  `EntryOptions`, the logger in `runlogs.DEVICE_SIDE_LINE`, `ADMIN_INTEGRATIONS`, and the `--integration` choices,
  which are `profile.TOLERANCE_S`'s keys. Control4 shipped in v0.1.0 with none, so the rig can't name it; its
  sign-offs are `_manual.json` rows.
- **DeviceSide's response shape, in the engine:** the class each entity's result reports, read for the step files'
  class selector (#97) and the records' classes (#85).
- **DeviceSide's record rules, in the rig:** `engine.write_records`, `record_json` and `render_record` (the strict
  bar: every default case that applies passes; never narrows, via `Record.read` and `narrows`; fact cases listed as
  skipped), the device rows (`record_model`, `record_firmware`, `record_manufacturer`, Zigbee2MQTT's fallbacks
  included), `facts.py`, and no record from step files that aren't HEAD's. Records are JSON, checked against
  DeviceSide's `docs/test-records.schema.json` before writing (`schema.py`), which also picks the format: 3, bound
  to the action's call transcript (#99), where the schema lists it, else 2, bound to the package tree; markdown for a
  checkout with no schema. `_facts.json` is format 2 (`_facts.md` for markdown).
- **Record changes keep pairing:** since this plan was written, #84, #85, #89, #99 and #105 were all DeviceSide-driven
  record changes, each paired with a DeviceSide PR, on top of the four Seam 2 names.
- **Household data and state:** `.env.rig` and `profiles/` in the rig's main checkout (`profile.py`), now with
  per-entity roles and authorization (#92); locks, journals, baseline, home record and ledger as `deviceside-rig*`
  files in DeviceSide's git common dir; reports and logs in `$TMP/deviceside-rig/`; staging beside the report;
  `.private-terms.rig` written into DeviceSide, which feeds both repos' shared `check-private` hook
  (jasonmx/pre-commit-hooks, #101).
- **DeviceSide's docs:** its CLAUDE.md and CONTRIBUTING no longer mention the rig (DeviceSide #527 to #529); design.md's
  Registry bullet still says the rig imports the registry, and TODO 9b still calls for the rig kit. DeviceSide's
  Experimental level is now Labs (#541); this repo's CLAUDE.md still says `experimental` flag.
- **Personal workflow:** `DEVICE_SIDE_RIG_RESTRICTED`, the `wt/*` plus open-PR ownership check
  (`refuse_unowned_checkout`, `HA_RIG_UNOWNED_CHECKOUT`), `refuse_moved_files` for a finished move.
- **Probes:** `--hold` children inherit only the environment (`run_command`): no credentials, so each probe finds
  `.env.rig` itself, and writes no journal.
- **23 flags on one command**, several valid only together (`--hold`, `--exclusive`, `--visual`) or alone
  (`--promote`, `--deploy-only`, `--recover`, `--roam-check`, `--wait-free`).

**Goal.** A framework power users and maintainers of custom (non-core) integrations install and use on their own HA:
probe devices, run repeatable tests, retrieve HA and Z2M logs, safely, on a household's real devices. It works out of
the box with DeviceSide and is the "rig kit" of DeviceSide's TODO 9b and design.md (Release gates).

**Shape.** A CLI run from a machine on the household's LAN, as today. No daemon, add-on or container: a household-side
agent would make cloud sessions and shared rigs possible, but costs a second versioned contract, a network service that
can install code into HA, and an engine development loop through a deploy. The seams keep that door open. A cloud
session that needs the rig uses Remote Control to a LAN machine.

**Non-goals.** Core HA integrations; a hosted or multi-user service; the household agent; deploy backends beyond the
shell hooks; roaming beyond Hue-home.

## Decisions

Settled with Jason, 2026-11-13:

1. **Standalone repo.** ha-rig stays its own repo, named `ha-rig`, and becomes public in Stage 2. This settles the
   evidence-rules plan's open "repo merge" (no merge) and supersedes its "the rig engine stays private" (Stage 2.1
   updates DeviceSide's docs).
2. **The target is data across a process boundary** (Seam 1): a generated manifest replaces the registry import;
   design.md's registry line changes to say the rig reads the manifest.
3. **The target renders its records** (Seam 2, after a debate): DeviceSide owns a `render` command, and its record
   rules move with it. (Refresh: DeviceSide #529 dropped CLAUDE.md's "no rig code here", so 1.2 adds the line that
   keeps only the engine out, rather than changing one.)
4. **Timing:** after v0.1.0, and only once an integration owner wants to use the rig.
5. **Docs describe only what a run has exercised.** No example setups the rig hasn't run on.
6. **The repo move waits for Stage 1.0, and starts from fresh history:** `DeviceSide/ha-rig` is a new repo whose
   first commit is the audited tree; `jasonmx/ha-rig` stays private and archived, keeping the history, issues and
   PRs.

## Before starting

- Confirm both conditions hold: v0.1.0 has shipped (yes, 2026-11-19), and an integration owner wants to use the rig
  (open).
- Re-ask Jason which non-DeviceSide custom integration proves the seam (1.5), most likely the interested owner's,
  and where it runs (the household HA or the owner's own). Record the answer here before Stage 0 starts.
- Rebase this plan on both repos' `main` and refresh its Context if the code has moved (done 2026-11-20; again if
  Stage 0 starts after more record changes land).
- Ask Jason for a yes to 0.4 (dropping the pre-v0.1.0 record writers) before Stage 0 starts.

## Seams

Each seam names the failure it prevents now and what it keeps possible. Code across a seam talks only through it.

1. **The target is data, across a process boundary.** The rig never imports target code. A target gives it a
   generated manifest and its step files; the rig runs the target's commands, named in the manifest, in the target's
   own environment, JSON in and out. Manifest, results file and the commands' JSON form one contract with one
   `contract_version`. Prevents: the rig breaking when DeviceSide's imports, Python version or `homeassistant` change;
   the rig needing DeviceSide's environment; version drift with more than one place to check. The rig already runs
   one such command (`--signoffs`), so this adds no trust.
2. **The executor emits results; the target renders.** A run is: select (manifest + step files + profile, or the
   target's `signoffs` command → a plan), execute (plan → a results file: JSON lines of cases, calls, samples,
   outcomes, the commit and whether the step files were HEAD's), render (results → the target's records and facts,
   by its `render` command). The rig's own run report is the output for a target with no `render`. Prevents:
   DeviceSide's record rules changing on the rig's release schedule (rig #58, #59, #62, #70 were all DeviceSide-driven
   record changes, each a paired PR), a crash mid-render losing a run's evidence, and a record-format change needing a
   rerun. Keeps possible: the executor moving next to HA later.
3. **One home for household state.** A `household` module owns every path: config (`rig.env`, profiles) and state
   (locks, journals, home record, baseline, ledger, run reports and results), keyed by HA instance. Nothing else
   builds a path to them. Prevents: household data in a checkout, and two checkouts on one HA missing each other's
   locks.
4. **Every change is journaled before it is made.** The HA, Hue and MQTT clients' mutating calls are reachable only
   through the journal's write-ahead wrapper. Probes get the same clients, so the rig puts back what a probe changed
   through them even when it crashes or forgets; a probe that calls a device directly (Shelly RPC, the Hue bridge)
   still puts back its own changes. Prevents: a change nobody can put back because a code path forgot to journal it.
5. **Credentials load in one place.** `rig.env` is read once, refused unless mode 0600; the admin token goes only
   where it is needed (today: ZHA and Z2M suites, options and entry steps, log capture, roaming). A probe declares
   its need: `ha-rig probe` passes `HA_URL` and `HA_TOKEN` in its environment, and `HA_ADMIN_TOKEN` only with
   `--admin`; nothing is written for it. No token, host or entity ID reaches a log line, run report, results file or
   record. Prevents: a token or household detail in a public record or shared log.
6. **Deploy is one interface with one implementation.** `status`, `sync(tree)`, `restart`, behind the existing hooks
   (`RIG_SYNC_CMD`, `RIG_STATUS_CMD`, `RIG_RESTART_CMD`, `scripts/deploy.sh`). The docs describe the protocol; the rig
   ships no backends.

**Keeping maintenance low.** Single user until Stage 2, so renames break cleanly: the old name is refused with the new
one named, and no shim outlives one rig release. No feature lands before a second user needs it.

## Stages

Each stage leaves the rig fully working for DeviceSide, and records unchanged byte for byte for the same run. The split
(Stage 2) waits for a second, real target (1.5), so the contract is designed from two users.

### Stage 0: tidy in place (rig only; repo stays `jasonmx/ha-rig`)

**0.1 Data out of the repos**, through the `household` module (Seam 3), following XDG:

| What | Now | After |
|---|---|---|
| `.env.rig` | rig main checkout | `~/.config/ha-rig/rig.env` |
| `profiles/*.yaml` | rig main checkout | `~/.config/ha-rig/profiles/` |
| locks, journals, home record, baseline, ledger | `deviceside-rig*` in DeviceSide's git common dir | `~/.local/state/ha-rig/<ha>/`, generic names |
| run reports, run logs, staging | `$TMP/deviceside-rig/` | `~/.local/share/ha-rig/runs/` |

- Records and `_facts.md` stay in the target checkout, where DeviceSide's gate reads them.
- `<ha>` is a short hash of `HA_URL`: the locks guard an HA, not a checkout, which also fixes two separate DeviceSide
  clones not seeing each other's locks today.
- `HA_RIG_CONFIG` (a directory) overrides `~/.config/ha-rig`, for a second HA or for tests.
- `ha-rig migrate` moves the files from their old homes once `--wait-free` shows no lock held and no journal pending;
  until then every command refuses, naming it. `refuse_moved_files` goes (its move is done).
- `terms.py` reads the profiles from the new home and still writes `.private-terms.rig` into DeviceSide.
- Probes that read `.env.rig` themselves break with the move; 0.2 gives them their credentials, and lands with 0.1.

**0.2 Personal workflow out; credentials in one place** (Seam 5).

- `DEVICE_SIDE_RIG_RESTRICTED` becomes `HA_RIG_APPROVE_RESTRICTED`; the old name is refused with the new one named,
  as `DEVICE_SIDE_RIG_APPROVED` was. Docs say "the rig's owner", not a name.
- The ownership check becomes a setting, `HA_RIG_REQUIRE_PR=1` in `rig.env` (set in Jason's);
  `HA_RIG_UNOWNED_CHECKOUT` goes. It prevents a record citing code nobody owns, which is one maintainer's process.
- `RIG_BLOCKER_ENTITY` stops being read, and the `DEVICE_SIDE_RIG_APPROVED` refusal goes (its rename is done).
- CLAUDE.md's `experimental` flag becomes DeviceSide's Labs flag.
- `rig.env` loads once with the 0600 check; probes get `HA_URL` and `HA_TOKEN`, and `HA_ADMIN_TOKEN` only with
  `--admin`.

**0.3 Subcommands for the 23 flags.** One mode per subcommand; run options keep their names.

| Now | After |
|---|---|
| (default), with `--profile --integration --signoffs --action -k --facts --unattended --serial --debug-logs --for --fresh --ignore-unreachables --no-deploy` | `ha-rig run …` (same options) |
| `--dry-run` | `--dry-run` on `run`, `deploy`, `probe` and `recover` |
| `--check` | `ha-rig check` |
| `--deploy-only` | `ha-rig deploy` |
| `--recover` | `ha-rig recover` |
| `--promote DIR` | `ha-rig promote DIR` (becomes `ha-rig render` in 1.2) |
| `--roam-check` | `ha-rig roam` |
| `--wait-free` | `ha-rig wait [--integration …]` |
| `--hold X --exclusive --visual V -- cmd` | `ha-rig probe [--hold X] [--exclusive] [--visual V] [--admin] [--python] -- cmd` |

- `probe --python script.py` runs the script with the rig's own interpreter, so it can import the rig's clients.
- `probe`, `wait` and `recover` need no target: the registry import becomes lazy, so they run from any directory.
  `check` and `run` need the target until the manifest (1.1).
- `scripts/rig.py` becomes the `ha-rig` console script; `scripts/rig.py` stays one release as a shim, then goes.
- README's help block is generated per subcommand; `tests/test_rig.py` checks each.
- The records' Source row names `ha-rig run`; DeviceSide's `CITED_COMMIT` reads only the commit, so the gate is
  unaffected.

**0.4 Drop the pre-v0.1.0 record writers** (proposed in the 2026-11-20 refresh; needs Jason's yes).

- Every DeviceSide checkout from v0.1.0 on has the schema, and it lists format 3. The rig stops writing markdown
  records, `_facts.md` and format 2 records, and refuses a checkout with no schema, or whose schema lacks format 3,
  naming the DeviceSide release it needs. `_facts.json` stays format 2, as the schema pins it.
- `render_record`, the markdown branches of `Record.read`, `record_name` and `facts.py`, and `RecordSchema.binds_calls`
  go, with their tests.
- Why now: 1.2 then moves one writer, not three, and its golden test covers the one format DeviceSide's gate reads.

**Stage 0 PRs**, in order: 0.1 with 0.2's probe credentials; 0.2; 0.3; 0.4 (it can go first, as it touches none of
the others' files bar `docs/rig.md`). Each updates `docs/rig.md`, `README.md` and
`CLAUDE.md`. After 0.1 the implementing session stops for Jason to run `ha-rig migrate` and a rig run live.

### Stage 1: move the repo, then build the seam inside it

**1.0 Start `DeviceSide/ha-rig`, private, from fresh history** (decision 6).

- Audit the tree that will be the first commit: the household check, plus a read of every tracked file for household
  details. The old history stays behind, so it needs no audit.
- Create `DeviceSide/ha-rig` (private), push the audited tree as one commit, and archive `jasonmx/ha-rig` (private).
  No transfer: it would carry the history.
- Set up afresh what a transfer would have carried: the Tests workflow's secret that reads DeviceSide PRs
  (`scripts/ci_deviceside.sh`), branch protection and `ci-green`, Renovate, and the Claude GitHub App. Point every
  checkout's `origin` at `https://github.com/DeviceSide/ha-rig.git`.
- Open issues on the old repo are copied over or closed with a pointer; old PR links in DeviceSide's history keep
  pointing at the archived repo.
- PR: the rig's docs name `DeviceSide/ha-rig`; DeviceSide's docs link it.

**1.1 The manifest instead of the registry import.** DeviceSide gains `scripts/gen_rig_manifest.py`, beside its other
`gen_*` scripts, writing committed JSON with a `contract_version`; CI's `--check` keeps it current, as for the others.
It carries what the rig reads from the registry and `target.py` today:

- per action and integration: whether an adapter exists, its `options` and `confirmable`;
- per integration: key, title, `ha_domain`;
- the target's HA service domain, config entry domain and logger name;
- the component directory (detection and the deploy unit), the step dirs, the records dir, the records' schema path,
  the call-transcript path, the terms file path;
- the commands: `signoffs` (today's `check_release.py --signoffs --json`) and `render` (1.2).

The rig reads the manifest and never imports `homeassistant`: it installs with `uv tool install`, and `check` and
`run` need only the manifest. `--integration` choices come from it, so Control4 becomes nameable; a profile with no
Control4 devices still runs none of its suites, and `TOLERANCE_S` gains a default for an integration it doesn't list.
`ADMIN_INTEGRATIONS` becomes rig knowledge of
HA domains (`zha`, `mqtt`), not DeviceSide keys. The rig refuses a stale manifest or an unknown `contract_version`,
naming the regenerate command. DeviceSide's design.md registry line changes to say the rig reads the manifest.

**1.2 Results file and DeviceSide's renderer** (Seam 2).

- The executor writes the results file into the run's data directory; the rig's run report is rendered from it.
- `write_records`, `record_json`, the device rows, `Record.read`, `narrows`, `facts.py`, `schema.py`'s record
  check and the HEAD-step-files rule move into DeviceSide's `scripts/rig_render.py` (results file → records and
  `_facts.json`), with its tests in `tests/scripts/`. The results file carries each case's reported classes and the
  devices it reached (model, firmware, manufacturer, Zigbee2MQTT's bridge data), so the renderer needs no HA. The rig
  keeps the ledger (resuming an interrupted run is execution).
- `ha-rig render RESULTS` runs the target's `render`; it replaces `--promote` and staging.
- Proven by a golden test: the old renderer and DeviceSide's give byte-identical records and `_facts.json` from the
  same results.
- DeviceSide's CLAUDE.md gains a line: the engine stays out; its manifest generator and renderer live there. The rig's
CLAUDE.md
  loses "a change here that alters what a record proves needs a matching change there".

**1.3 Journaled mutations, probes' included** (Seam 4).

**1.4 Generic profiles.** Roles become tags the manifest or step files declare; DeviceSide's manifest declares today's
roles, so profiles stay valid. Per-integration lists key on the target's integration keys.

**1.5 A second target, to prove the seam.** A plain `ha` target: step files calling any HA service on any domain, no
commands, the run report its only output, run on one real non-DeviceSide custom integration on the household HA
(the integration chosen in Before starting), with a few step files and at least one probe. **Exit criterion:** it runs with no engine change of
its own, and DeviceSide's records are byte-identical to Stage 0's for the same run.

**Stage 1 PRs:** 1.0; 1.1 (DeviceSide + rig, `Tests-with`); 1.2 (DeviceSide + rig, `Tests-with`); 1.3; 1.4; 1.5.

### Stage 2: split and publish

**2.1 Visibility.** Make `DeviceSide/ha-rig` public; its history begins at 1.0's audited first commit. DeviceSide's
design.md, CONTRIBUTING (the maintainers' path to a record for hardware the rig lacks) and TODO 9b say the rig kit is
ha-rig.

**2.2 DeviceSide pins it.** DeviceSide's `dev` group pins the `ha-rig` version it was tested with. Each side tests its
half of the contract: the rig's CI runs the engine against a fixture target (manifest, step files, stub commands);
DeviceSide's CI runs `rig_render.py` and the manifest check against fixtures at the pinned `contract_version`.
`Tests-with` and `scripts/ci_deviceside.sh` go.

**2.3 Set up for a stranger.**

- `ha-rig init`: asks for HA's URL and a token (a non-admin user), tests the connection, writes `rig.env` with the
  deploy hooks left to fill in, and drafts a profile from the entities it finds, every role left to confirm. Nothing
  moves during init.
- Safe defaults: a new profile is not `pre_authorized`; with no restricted sensor, restricted work needs a yes at a
  terminal or `HA_RIG_APPROVE_RESTRICTED=1`; `ha-rig recover` is on the docs' first page.
- Docs: quick start (install, init, `check`, `run --dry-run`, a first probe), writing step files, writing a target,
  the hook protocol, what the rig changes and puts back. Only what a run has exercised.

**2.4 Agent probes.** A Claude Code plugin with a skill that probes through `ha-rig probe`, captures HA and Z2M logs,
lets the rig put back what it changed, and writes a dated note where the target keeps research (DeviceSide:
`docs/integrations/<x>/research/`).

**2.5 Release.** `ha-rig` on PyPI from a tagged release workflow; `uv tool install ha-rig`; semver from 0.x.

## Risks

- **Over-generalizing from one household:** the seam is built in place, and the split waits for 1.5.
- **Safety on unknown homes:** 2.3's defaults, a generic and well-tested journal, roaming labeled experimental.
- **Records changing under DeviceSide's gate:** byte-identical checks at 1.2 and 1.5.
- **Decision 3 has a cheaper alternative now:** the record format already has a JSON Schema in DeviceSide, so "the rig
  renders and the schema is the contract" costs less than when decision 3 was taken. The five paired record changes
  since then were changes to what a record says, which a schema contract would still pair; decision 3 stands.
- **Version drift:** one `contract_version`, checked at load and by both CIs; DeviceSide pins the rig version.
- **A wider contract:** the results file is wider than the three record rows DeviceSide parses. If it changes more
  often than the record format did while Stage 1 is built, revisit decision 3 (the rig renders, the record format
  becomes the contract).
- **A fresh repo misses a setting the old one had:** 1.0's setup list.

## Coordination

Intent: Make the rig a standalone, installable framework with DeviceSide as its first target, after v0.1.0, starting
with an in-place tidy.
Touches: (this plan only; each stage's PRs name their own)
