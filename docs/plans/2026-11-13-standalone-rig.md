# The rig as a standalone framework, with DeviceSide as its first target

## Context

The rig drives a live Home Assistant and its real devices: it runs repeatable step-file tests, holds locks for probes,
puts back what it changed, and captures HA's and Z2M's logs. Today it serves only DeviceSide, and is built into it:

- `scripts/rig.py` imports deviceside's `AdapterRegistry`, whose discovery imports every adapter and so
  `homeassistant`: the rig runs only inside deviceside's environment.
- `ha_rig/target.py` loads deviceside's `scripts/check_release.py` for record paths and adapter code, finds the
  target by `custom_components/deviceside`, and refuses a checkout that is not a `wt/*` worktree with an open PR.
- The step language calls `deviceside.*`, samples `source: deviceside`, and sets DeviceSide's options; records,
  observed tests and the coverage ledger are deviceside's release-gate rules.
- Profiles and `.env.rig` sit in the rig's main checkout; locks, journals and the baseline in deviceside's git common
  dir; household terms are written into deviceside as `.private-terms.rig`.
- The step files have two homes that have drifted: the rig runs `ha_rig/steps/` (`engine.py` `STEPS_DIR`), while
  deviceside's `CLAUDE.md` names `tests/rig/steps/` and `tests/integrations/<x>/rig/` as their home; 8 of the 20
  files differ. A maintainer editing deviceside's copy changes nothing that runs.
- `scripts/rig.py` has 22 flags on one command, several meaningful only together (`--hold`, `--exclusive`, `--visual`)
  or alone (`--promote`, `--deploy-only`, `--recover`).

**Goal.** A framework other people can install and use on their own HA: power users, and developers and maintainers
of custom (non-core) integrations. It probes devices, runs repeatable tests, and retrieves HA and Z2M logs, safely, on
a household's real devices. It works out of the box with DeviceSide. The architecture is option B of the review:
a generic engine, with targets as plugins; DeviceSide owns its target and its step files, next to the evidence rules
they carry out.

**Shape.** A CLI run from a machine on the household's LAN, as today. No daemon, add-on or container: a
household-side agent (the engine running next to HA, the CLI as its client) would make cloud sessions and shared
rigs possible, but costs a second versioned contract, a network service that can install code into HA, and an
engine development loop through a deploy. The seams below keep that door open without building it. A cloud session
that needs the rig uses Remote Control to a LAN machine.

**Non-goals.** Core HA integrations (their test culture is mocks and snapshots); a hosted or multi-user service;
the household agent; deploy backends beyond the shell hooks; supporting every Zigbee stack's roaming (roaming stays
Hue-home, as today).

## Seams

Each seam names the failure it prevents now and what it keeps possible. Code across a seam talks only through it.

1. **The target is data, across a process boundary.** The rig never imports target code. A target gives it a
   generated manifest and its step files, and the rig hands back a results file; whatever the target does with
   results (records, coverage) runs as the target's own commands, in the target's own environment, named in the
   manifest. Manifest, results file and those commands' JSON form one contract with one `contract_version`, the only
   versioned contract between the repos. Prevents: the rig breaking when deviceside's imports, Python version or
   `homeassistant` change, the rig needing deviceside's environment, and version drift with more than one place to
   check.
2. **The executor takes a plan and emits results.** A run is: select (manifest + step files + profile → a plan),
   execute (plan → a results file, JSON lines of cases, calls, samples and outcomes), render (results → records, by
   the target's command). The executor writes nothing in the checkout. Prevents: a crash mid-render losing a run's
   evidence, and record-format changes needing a rerun (re-render from the saved results). Keeps possible: the
   executor moving next to HA later, behind a transport.
3. **One home for household state.** A single `household` module owns every path: config (`rig.env`, profiles) and
   state (locks, journals, home record, baseline, ledger), keyed by HA instance. Nothing else builds a path to them.
   Prevents: household data leaking into a checkout, and two checkouts on one HA missing each other's locks.
4. **Every change is journaled before it is made.** The HA, Hue and MQTT clients' mutating calls are reachable only
   through the journal's write-ahead wrapper; the raw clients are read-only outside it. Prevents: a change the next
   run can't put back because a new code path forgot to journal it.
5. **Credentials load in one place.** `rig.env` is read once, refused unless it is mode 0600, and tokens go to the
   clients that need them: the non-admin token by default, the admin token only for suites that declare the need.
   No token, host or entity ID reaches a log line, run report or record. Prevents: a token or household detail in
   a public record or a shared log.
6. **Deploy is one interface with one implementation.** `status`, `sync(tree)`, `restart`, behind the existing
   shell-hook protocol (`RIG_SYNC_CMD`, `RIG_STATUS_CMD`, `RIG_RESTART_CMD`). The docs describe the protocol; the rig
   ships no deploy backends it would have to maintain.

**Keeping maintenance low.** Single user until Stage 2, so renames break cleanly: the old name is refused with the
new one named, and no compatibility shim outlives one rig release. No feature lands before a second user needs it
(generic roles, an init wizard beyond the essentials, deploy backends). The docs describe only what a run has
exercised: no example setup, hook or install path goes in until the rig has run on it.

## Stages

Each stage leaves the rig fully working for DeviceSide. Stage 0 is worth doing even if the split never happens.
The split (Stage 2) waits until a second, real target has run through the seam (Stage 1's exit criterion), so the
contract is designed from two users rather than guessed from one.

### Stage 0: tidy in place

The repo stays `jasonmx/ha-rig` through this stage; its move is 1.0.

**0.1 One home for step files: the target checkout.** Deviceside's `CLAUDE.md` already puts them there, and under B
they belong with the target.

- Reconcile the 8 differing files: the rig's copy is what has run, so it wins on content; deviceside's later
  comment-only edits (citation drops) are reapplied on top. Land the result in deviceside's `tests/rig/steps/`.
- The engine loads step files from the target: `tests/rig/steps/*.yaml` plus every
  `tests/integrations/<x>/rig/*.yaml` (run on `<x>` alone, as deviceside's `CLAUDE.md` says). `ha_rig/steps/` is
  deleted; the rig's tests load fixtures from `tests/fixtures/steps/`.
- Rig `CLAUDE.md`: "a new deviceside action needs its step file here" becomes "in deviceside, landed with its action";
  the `Tests-with` pairing stays for engine changes a step file needs.

**0.2 Data out of the repos.** All config, state and results in per-user directories, following XDG:

| What | Now | After |
|---|---|---|
| `.env.rig` | rig main checkout | `~/.config/ha-rig/rig.env` |
| `profiles/*.yaml` | rig main checkout | `~/.config/ha-rig/profiles/` |
| locks, journals, home record, baseline, ledger | deviceside git common dir | `~/.local/state/ha-rig/<ha>/` |
| run reports, run logs, staged records | (as now) | `~/.local/share/ha-rig/runs/` |

- `<ha>` is a short hash of `HA_URL`: the locks guard an HA, not a checkout, so two checkouts (or a deviceside
  clone and another target later) on the same HA share them. This also fixes runs from two separate deviceside
  clones not seeing each other's locks today.
- All of it goes through the `household` module (Seam 3).
- `HA_RIG_CONFIG` (a directory) overrides `~/.config/ha-rig` for a second HA or for tests.
- `ha-rig migrate` moves the files from their old homes, once `--wait-free` shows no run holds a lock and no journal
  is pending; until then any command refuses, naming it (replacing `refuse_moved_files`, whose earlier move is done).
- `terms.py` reads the profiles from the new home; it still writes `.private-terms.rig` into deviceside (a manifest
  entry from Stage 1).

**0.3 Take the personal workflow out of the code.**

- `DEVICE_SIDE_RIG_RESTRICTED` becomes `HA_RIG_APPROVE_RESTRICTED`; the old name is refused with the new one named, as
  `DEVICE_SIDE_RIG_APPROVED` was. Docs say "the rig's owner", not a name.
- The worktree-ownership check (`refuse_unowned_checkout`) becomes a setting, `HA_RIG_REQUIRE_PR=1` in `rig.env`,
  set in Jason's; `HA_RIG_UNOWNED_CHECKOUT` goes. It prevents a record citing code nobody owns; that is one
  maintainer's process, not something a new user's first run should stop on.
- `RIG_BLOCKER_ENTITY`, the restricted sensor's old name, stops being read.
- Credentials load in one place, with the 0600 check and the admin token only for suites that need it (Seam 5).

**0.4 Subcommands for the 22 flags.** One mode per subcommand, so flags valid only in one mode can't be combined
wrongly, and `--help` per subcommand stays short. Run options keep their names.

| Now | After |
|---|---|
| (default), with `--profile --integration --action -k --unattended --reobserve --serial --debug-logs --for --fresh --ignore-unreachables --no-deploy` | `ha-rig run …` (same options) |
| `--dry-run` | `--dry-run` on `run`, `deploy`, `probe` and `recover` |
| `--check` | `ha-rig check` |
| `--coverage` | `ha-rig coverage` |
| `--deploy-only` | `ha-rig deploy` |
| `--recover` | `ha-rig recover` |
| `--promote DIR` | `ha-rig promote DIR` |
| `--roam-check` | `ha-rig roam` |
| `--wait-free` | `ha-rig wait [--integration …]` |
| `--hold X --exclusive --visual V -- cmd` | `ha-rig probe [--hold X] [--exclusive] [--visual V] -- cmd` |

- `--integration` takes the target's integration keys instead of a hardcoded list (the registry supplies them now,
  the manifest from Stage 1).
- `scripts/rig.py` becomes the `ha-rig` console script in `pyproject.toml`; `scripts/rig.py` stays as a shim for one
  release of the rig's own habits (agents' skills, notes), then goes.
- README's help block is generated per subcommand; `tests/test_rig.py` checks each.

**Stage 0 PRs**, in order: 0.1 (rig + deviceside, `Tests-with`); 0.2; 0.3; 0.4. 0.2–0.4 touch only the rig. Each
updates `docs/rig.md`.

### Stage 1: move the repo, then build the seam inside it

The engine stops importing or loading anything from the target; the target's data and commands supply what differs.
While the seam is built, DeviceSide's commands may live in the rig (`ha_rig/targets/deviceside/`, run as
subprocesses in deviceside's environment); they move into deviceside in 2.2.

**1.0 Move the repo to `DeviceSide/ha-rig`, private, and decide how its history goes public.**

- History first, since the transfer carries it: audit the full history for household details (the household check
  runs on today's tree, not on old commits), then choose between transferring as-is (keeps issues and PRs) and
  starting the org repo from fresh history (open question 1). Decide too whether the public engine belongs under
  the `DeviceSide` org or a neutral one (open question 2).
- Transfer, keeping the name: `gh api -X POST repos/jasonmx/ha-rig/transfer -f new_owner=DeviceSide`. GitHub
  redirects the old URL and keeps issues, PRs, branch protection and repo secrets.
- Check after: the Claude GitHub App is installed on `DeviceSide/ha-rig` (sessions see it); the Tests workflow's
  token can still read `DeviceSide/DeviceSide` PRs (`scripts/ci_deviceside.sh`); Renovate is enabled for the org
  repo; branch protection and `ci-green` still apply.
- Local: `git remote set-url origin https://github.com/DeviceSide/ha-rig.git` in every checkout and worktree.
- PR: README, `docs/rig.md` and `CLAUDE.md` name `DeviceSide/ha-rig`; deviceside's `TODO.md` and docs that say
  "ha-rig" link it.

**1.1 The manifest instead of the registry import.** Deviceside gains `scripts/rig_manifest.py`, which writes JSON
from its registry, with its `contract_version`:

- for each action and integration: whether an adapter exists, its expected status, tolerance, options in scope and
  code files;
- the step-file directories, and each integration's records directory;
- the target's step extensions, as data: the service a `source: <target>` sample reads (`deviceside.get_attributes`),
  the config entry domain whose options a suite sets, the default domain for `call`;
- the deploy unit (the component directory a deploy ships) and where household terms go (`.private-terms.rig`);
- the target's commands (1.2).

The rig reads that JSON and never imports `homeassistant`, so it installs with `uv tool install` and runs in its own
environment. Deviceside's CI checks the committed manifest matches (`--check`), like its other generated files; the
rig refuses a stale manifest or an unknown `contract_version`, naming the regenerate command.

**1.2 The target's commands.** What only the target can judge runs as its commands, JSON in and out, named in the
manifest and run in the target's environment:

- `render`: a results file → records, observed tests included (Seam 2), run after the run, never during it;
- `coverage`: the selected suites → which are covered at HEAD and which cases are missing, from the target's
  records and ledger.

The generic engine keeps: the HA client, locks, journal and restore, baseline and pauses, authorization, log
capture, deploy, profiles, roaming, suite selection from the manifest, the step runner and its generic steps (`set`,
`call` on any service, `expect_state`, `sample` on any attribute from HA, `wait`).

**1.3 Generic profiles.** Roles become tags a target or step file declares, not a fixed list in `profile.py`;
DeviceSide's manifest declares today's roles, so existing profiles stay valid. Per-integration lists key on the
target's integration keys.

**1.4 A second target, to prove the seam.** A plain `ha` target: step files that call any HA service on any domain,
with no records gate and no commands, its pass/fail reports read from the results file in the data directory. It is
run on one real non-DeviceSide custom integration on the household HA (Jason picks it; open question 3), with a
handful of step files and at least one probe through `ha-rig probe`. **Exit criterion for Stage 1:** that target
runs without any engine change specific to it, and the DeviceSide target's records come out byte-identical to Stage
0's for the same run.

**Stage 1 PRs:** 1.0 (after the transfer); 1.1 (deviceside + rig, `Tests-with`); the executor's results file and
render-after-run (Seam 2); mutations only through the journal (Seam 4); 1.2, moving deviceside's record and coverage
logic behind its commands without changing a record; 1.3; 1.4.

### Stage 2: split and publish

**2.1 Visibility.** Make the engine's repo public, with the history chosen in 1.0.

**2.2 DeviceSide's target moves into deviceside**: its manifest generator, its `render` and `coverage` commands and
its step files, all in deviceside, which pins the `ha-rig` version it was tested with in its `dev` group. The rig
imports none of it, so there is no plugin to register. Each side tests its half of the one contract (Seam 1): the
rig's CI runs the engine against a fixture target (a manifest, step files and stub commands); deviceside's CI runs
its commands against a fixture results file at the pinned version's `contract_version`. `Tests-with` and
`scripts/ci_deviceside.sh` go. The rule "a change here that alters what a record proves needs a matching change
there" leaves the rig's `CLAUDE.md`: what a record proves now lives in deviceside's own code.

**2.3 Set up for a stranger.**

- `ha-rig init`: asks for HA's URL and a token (and tells you to create a non-admin user for it), tests the
  connection, writes `rig.env` with the deploy hooks left for the user to fill in, and drafts a profile from the
  entities it finds per integration, with every entity's roles left for the user to confirm. Nothing moves during
  init.
- Deploy stays the shell hooks (Seam 6); the docs describe the protocol.
- Safe defaults: a new profile is not `pre_authorized`; with no restricted sensor, restricted work needs a yes at a
  terminal or `HA_RIG_APPROVE_RESTRICTED=1`; every change is journaled before it is made; `ha-rig recover` is in the
  first page of the docs.
- Docs: a quick start (install, init, `check`, a first `run --dry-run`, a first probe), writing step files for a new
  domain, writing a target, and what the rig changes and puts back.

**2.4 Agent probes.** A Claude Code plugin in the repo with a skill that probes through `ha-rig probe`: takes the
locks, runs the probe, captures HA and Z2M logs, puts back what it changed, and writes a dated research note in the
target's chosen place (DeviceSide: `docs/integrations/<x>/research/`).

**2.5 Release.** Publish `ha-rig` to PyPI from a tagged release workflow; `uv tool install ha-rig` is the install
path; versions are semver from 0.x, and deviceside pins one.

## Risks

- **Over-generalizing from one household.** Mitigated by building the seam in place and gating the split on a
  second real target (1.4).
- **Safety on homes we've never seen.** A bug can leave someone's lights or HA in a bad state. Mitigated by the
  defaults in 2.3, by keeping journal and restore generic and well tested, and by labeling roaming experimental.
- **Competing with DeviceSide v0.1.0.** Stage 0 helps both and is small; Stages 1–2 should not block a deviceside
  release, and the release gate's records must stay unchanged through them (1.4's byte-identical check).
- **Version drift between rig and target.** One contract, with one `contract_version` the rig checks at load and
  both sides' CI test against (Seam 1, 2.2); deviceside pins the rig version it was tested with.
- **Repo move breaks automation.** The checks listed in 1.0 cover CI, the GitHub App and Renovate.

## Open questions

1. History: transfer the repo as-is, or start `DeviceSide/ha-rig` from fresh history (1.0)?
2. Does the `DeviceSide` org want the public engine under its name, or a neutral org (1.0)?
3. Which non-DeviceSide custom integration proves the seam in 1.4?

## Coordination

Intent: Make the rig a standalone, installable framework with DeviceSide as its first target, starting with an
in-place tidy.
Touches: (this plan only; each stage's PRs name their own)
