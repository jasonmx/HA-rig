#!/usr/bin/env -S uv run python
"""Run the rig tests against the live HA, behind an authorization gate.

The rig is the household's own Home Assistant and its in-use lights. Nothing changes state before the gate:
preflight reads the profile, the adapter registry and the deployed tree hash, prints what will run and whether it may
go (Authorization, below), then, at a terminal, asks.

The rig tests the deviceside checkout it is run from, or DEVICE_SIDE_REPO (ha_rig/target.py): it deploys that
checkout's code, records into its docs/test-records, and keeps its locks and journals in its git common dir. The
profiles and .env.rig live in this rig's main checkout. Each invocation refreshes deviceside's household terms
(ha_rig/terms.py).

Runs share the rig through locks in deviceside's git common dir, shared by every worktree (ha_rig/locks.py): each holds
the integrations it moves, so runs on different ones go side by side, and HA, shared, or exclusively for a deploy or a
suite with DeviceSide options, which restart or reload DeviceSide under every other run. The plan prints the run's
locks. Each run deploys and restarts HA onto its own checkout's tree when HA hasn't loaded it, so a run queued behind
another tests its own code; it reads the deploy state again once it holds its locks, and takes HA exclusively if it must
deploy after all. Once its deploy is done and HA is back, a run with no option suites steps HA down to shared. Ctrl-C
mid-deploy waits for the deploy and HA's restart to finish before releasing the locks. The tests are the step files in
ha_rig/steps, run by ha_rig/engine.py; a full pass of an adapter stages its test record next to the run's report,
and only a run that ends cleanly, with a result for every case it planned, writes it to the checkout
(ha_rig/staging.py). Any other end prints the one --promote command that does.

Ctrl-C, SIGTERM and SIGHUP stop a run, putting the devices back; a second one leaves them. What a run changes is
journaled first, in a journal of its own next to the locks (ha_rig/journal.py), so whatever a killed run leaves
changed, the next run whose locks cover it puts back before its gate, or --recover, holding every lock, does.

--deploy-only deploys the checkout's branch and restarts HA onto it, holding HA exclusively until HA is back, then
releases the locks: no profile and no tests, for trying a branch's non-device features by hand. Nothing happens when HA
already runs the checkout's tree.

--hold runs a probe's command holding the named integrations and HA (--exclusive: exclusively), with the profile's
automations for them paused, passing signals on to it and returning its exit code; no case journal. --visual declares
what it changes that anyone could see, and --dry-run says whether it may go.

Authorization (ha_rig/authorize.py) classifies the work, not the caller. Unrestricted work goes at any time: a test run
or a recovery on a profile with `pre_authorized: true`, which covers everything that run does; --deploy-only, as HA
restarts are always allowed; a probe declared --visual none, or --visual profile on a pre-authorized profile.
Everything else is restricted, and goes when either RIG_RESTRICTED_ENTITY in .env.rig, an HA binary_sensor that is on
while someone is near enough to see the lights, reads off, or Jason has approved it: DEVICE_SIDE_RIG_RESTRICTED=1, or a
y at the prompt `Restricted: <why>. Proceed anyway?`. Without the key, restricted work needs the approval. At a
terminal the run asks `Proceed?` as a readiness check; an agent's call, with none, isn't asked. An aborted run's
changes are restricted by the profile that left them: one the rig may not put back is left and named, and a run whose
locks cover it stops before its gate. A restricted run or probe reads the sensor every 15 s and aborts as on a SIGTERM,
putting the devices back, when it turns on: two reads in a row of on, unknown or unavailable after a read of off, so an
approved run started while it was on goes on until it goes off and on again. Reads that fail, as during the deploy's
restart, don't count. DEVICE_SIDE_RIG_APPROVED, the variable this replaced, is refused.

Under cmux the run colors its workspace: green holding its locks, amber waiting for them.

A profile (profiles/<name>.yaml) names the entities a run may move and what it pauses; --profile picks
one, and may be left out when there is only one. A run pauses the profile's `all` list and those of the integrations it
holds, through a baseline runs side by side share (ha_rig/baseline.py): the last run out turns each back on. The
baseline also holds the log levels and Z2M device options a run changes, so a crashed run's come back with the next
run's prune, or --recover's.

The run starts with lanes: one per integration, side by side, each running that integration's cases on fixed entities
(engine.in_head). The rest run after, one at a time: cases with DeviceSide options, whose save reloads the entry, and
those on roaming bulbs, rig-made groups and scenes, or powered entities and their supplies. --serial runs every case
one at a time, as before lanes.

A profile with `roam:` has bulbs the run moves between the networks it declares, of Hue, ZHA and Z2M
(ha_rig/roam.py): the lanes run with the bulbs at home, then the rest run in phases, the home's with the other
integrations', then each other declared network's in that order, each only if it has suites, with a direct move before
each and one straight home at the end. The run holds only the networks it visits. --recover, and the replay before the
next run's gate, move them home. --roam-check visits every declared network with no suites.

Before the gate, and alone with --check, the run checks every phase's devices at once: each fixed entity the cases
it will run use is in HA, on its integration and available, and each roaming bulb is on a network. Without that, a
device left unpowered stops the run only when its phase comes, after the bulbs have moved. With
--ignore-unreachables an unavailable fixed entity skips its cases instead (engine.unreachable), and while any is,
those on fixed groups whose members the profile doesn't list and on fixed scenes; their suites write no record.

A run builds coverage (ha_rig/coverage.py): it leaves out each selected suite with a fresh record, one citing a commit
at which the suite's adapter code is unchanged, as deviceside's release gate judges it, or a saved result for every
case, and runs only the cases of the rest with no saved result. It saves each case as it finishes in the profile's
ledger (ha_rig/progress.py), keyed by the suite's code, so one that stops part way, on a signal, the restricted sensor
or --for's budget, exits 75 and, rerun, picks up where it stopped; a suite finished across runs still writes its
record. --for DURATION (30m, 1h15m) starts no case once spent, letting in-flight ones finish; it counts from approval,
the deploy included. A move starts only with MOVE_LEAD_S left and always runs its first case. A suite with a failed
case, or saved on another HA version, reruns whole; --fresh reruns the selected suites whatever is recorded or saved.
--coverage prints each selected suite's coverage at HEAD and the devices its missing cases need, changing nothing.

Every run captures Z2M's log (when it touches Z2M) and HA's next to its report, as <report>.z2m.log and .ha.log
(ha_rig/runlogs.py): deleted after a clean run, kept and printed after any other. A DeviceSide warning or error
in HA's log that no step file expects is listed and fails the run. --debug-logs also raises HA's
DeviceSide and the run's integrations, and Z2M, to debug for the run, keeps the logs, and puts the levels back (the
baseline, so --recover too).

Usage: scripts/rig.py [--profile NAME] [--dry-run] [--no-deploy] [--unattended] [--serial] [--debug-logs]
                      [--integration KEY ...] [--action NAME ...] [-k TEXT] [--for DURATION] [--fresh]
                      [--ignore-unreachables]
       scripts/rig.py [--profile NAME] --check [--ignore-unreachables] [--integration KEY ...] [--action NAME ...]
                      [-k TEXT]
       scripts/rig.py [--profile NAME] --coverage [--unattended] [--integration KEY ...] [--action NAME ...] [-k TEXT]
       scripts/rig.py [--profile NAME] --roam-check [--debug-logs]
       scripts/rig.py --deploy-only [--dry-run]
       scripts/rig.py --recover
       scripts/rig.py --promote DIR
       scripts/rig.py --wait-free [--integration KEY ...]
       scripts/rig.py [--profile NAME] --hold KEY [--hold KEY ...] [--exclusive] [--visual none|profile]
                      [--dry-run] -- COMMAND [ARGS ...]
"""

import argparse
import asyncio
import contextlib
import datetime
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ha_rig.target import COMPONENT, RIG_HOME, deviceside_repo  # noqa: E402

REPO = deviceside_repo()  # the deviceside checkout under test, whose code a run deploys and records
sys.path.append(str(REPO))

from custom_components.deviceside.core.registry import AdapterRegistry  # noqa: E402

from ha_rig import coverage, locks, target, terms  # noqa: E402
from ha_rig.authorize import APPROVAL, OLD_SENSOR_KEY, SENSOR_KEY, Decision, authorize  # noqa: E402
from ha_rig.baseline import NAME as BASELINE_NAME  # noqa: E402
from ha_rig.baseline import Baseline  # noqa: E402
from ha_rig.bridge import BridgeError, HueBridge  # noqa: E402
from ha_rig.client import HAClient, HAError  # noqa: E402
from ha_rig.coverage import SuiteCoverage  # noqa: E402
from ha_rig.engine import (  # noqa: E402
    ADMIN_INTEGRATIONS,
    Budget,
    Case,
    EntryOptions,
    RunResult,
    Snapshot,
    StepFile,
    Suite,
    SuiteResult,
    adapters_for,
    applies,
    cases,
    check_options,
    in_head,
    load_observations,
    load_step_files,
    merge,
    report_path,
    run,
    unfinished,
    write_observed,
    write_records,
    write_report,
)
from ha_rig.home import HomeRecord  # noqa: E402
from ha_rig.journal import LEGACY_NAME, Journal, orphans  # noqa: E402
from ha_rig.locks import Claim, Held  # noqa: E402
from ha_rig.profile import (  # noqa: E402
    EXAMPLE,
    MAIN_CHECKOUT,
    PAUSE_ALL,
    PROFILES_DIR,
    TOLERANCE_S,
    Profile,
    RoamBulb,
    load_env,
    profile_paths,
)
from ha_rig.progress import Done, Progress  # noqa: E402
from ha_rig.roam import BLUETOOTH_RESET, NAMES, Bulb, GroupError, Roamer, RoamError  # noqa: E402
from ha_rig.roam import plan_lines as roam_plan_lines  # noqa: E402
from ha_rig.runlogs import HA_DOMAINS, DebugLevels, RunLogs, ha_domains  # noqa: E402
from ha_rig.staging import Staging, display  # noqa: E402

TEST_RECORDS_DIR = REPO / "docs" / "test-records"
SERVICE = ("deviceside", "turn_on_for")
# HA's bootstrap stage-1 (120 s) and stage-2 (300 s) timeouts, after which a stuck dependency is let go, plus the
# container's start. A restart behind a hung ZHA took about 350 s.
READY_TIMEOUT_S = 450
# A target still unavailable this long after HA is ready is most likely unpowered, so the run stops rather than fail
# every case; the grace covers an integration whose entities come back just after the restart.
TARGETS_TIMEOUT_S = 60
RESTART_ESTIMATE_S = 90
MOVE_LEAD_S = 60  # the budget a move needs left: one move's join, rename and HA wait
ROAM_ESTIMATE_S = 180  # the three moves: the probe's joins took about 10 s each, plus releases, renames and HA
ABORT_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
CLEAR_POLL_S = 15
BLOCKED = ("on", "unknown", "unavailable")  # restricted-sensor states that revoke a run; an unread one doesn't
# Run reports go where the OS clears old files: they are for reviewing a run, not for keeping.
REPORT_DIR = Path(tempfile.gettempdir()) / "deviceside-rig"
RESUMABLE = 75  # EX_TEMPFAIL: the run stopped with cases left, saved to resume from


class Aborted(Exception):
    """A signal before the tests start; the run ends on its message."""


@dataclass(slots=True)
class Deploy:
    commit: str  # HEAD at the plan, whose tree local_tree is
    local_tree: str
    synced_tree: str | None  # on disk in HA's config
    loaded_tree: str | None  # what HA last restarted onto
    branch: str
    blocker: str | None = None

    @property
    def needed(self) -> bool:
        """Also when only the loaded tree differs: a failed restart leaves the synced tree unloaded."""
        return not self.local_tree == self.synced_tree == self.loaded_tree


def git(*args: str, check: bool = True) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], check=check, capture_output=True, text=True).stdout.strip()


def select(
    step_files: list[StepFile], profile: Profile, integrations: list[str] | None, actions: list[str] | None = None
) -> list[Suite]:
    """Every step file on every integration in the profile it has a test for (`only:`), narrowed to --integration
    and --action. A file with no adapter there stays, for the dry-run's `no adapter, will skip`. An action takes
    every step file for it (lower_brightness and lower_brightness-predicted_state), whole, so each still writes its
    record; a file with a `name` (lifecycle) goes by that instead. Dropping the option files an adapter lacks is
    run_suites's job (engine.applies)."""
    return [
        Suite(step_file, integration)
        for step_file in step_files
        if not actions or (step_file.name or step_file.action) in actions
        for integration in profile.integrations
        if not integrations or integration in integrations
        if step_file.runs_on(integration)
    ]


def roams(profile: Profile, suites: list[Suite], roam_check: bool = False) -> bool:
    """Whether the run moves the profile's bulbs: for --roam-check, or a suite on a network the profile lets them
    visit other than their home. The home's suites and the rest run with the bulbs at home, so a run of only those
    stays there."""
    return bool(profile.roam) and (roam_check or any(suite.integration in away(profile) for suite in suites))


def away(profile: Profile) -> list[str]:
    """The networks the profile's bulbs may visit other than their home, in CYCLE order."""
    return [network for network in profile.roam_networks if network != profile.roam_home]


def makes_groups(profile: Profile, suites: list[Suite], roam_check: bool = False) -> bool:
    """Whether the run makes the profile's ds-rig-* groups: on each visit when it roams, or at home for a suite
    there."""
    if not profile.roam_groups:
        return False
    return roams(profile, suites, roam_check) or any(suite.integration == profile.roam_home for suite in suites)


def phases(suites: list[Suite], profile: Profile, roaming: bool) -> list[tuple[str | None, list[Suite]]]:
    """The run's phases, each a network to move the bulbs to (None: no move) and the suites run there. Roaming: the
    home's suites with every other integration's, then those of each network the profile lets the bulbs visit, in
    CYCLE order, each in select's order, skipping a network with no suites; a run with none (--roam-check) visits
    every one."""
    if not roaming:
        return [(None, suites)]
    home, stops = profile.roam_home, away(profile)
    by_network = [
        (network, [suite for suite in suites if (suite.integration if suite.integration in stops else home) == network])
        for network in [home, *stops]
    ]
    return [(network, held) for network, held in by_network if network == home or held or not suites]


def plan_lines(
    suites: list[Suite],
    profile: Profile,
    registry: AdapterRegistry,
    keyword: str | None = None,
    only: Callable[[Case], bool] | None = None,
) -> list[str]:
    """Per adapter about to run: the household entities it will move, then each test case `only` accepts. A suite
    with none is left out: another part of the run has its cases."""
    lines = []
    for suite in suites:
        suite_cases = cases(suite, profile, keyword, adapters_for(suite, registry))
        if only is not None and not (suite_cases := [case for case in suite_cases if only(case)]):
            continue
        if registry.adapter(suite.action, suite.integration) is None:
            lines.append(f"{suite.integration} / {suite.action} (no adapter, will skip)")
            continue
        if only is None:
            entities = [entity for role in suite.file.roles if (entity := profile.entity(suite.integration, role))]
        else:
            entities = [entity for case in suite_cases for entity in case.entities.values()]
        targets = ", ".join(dict.fromkeys(entities)) or "no entity, will skip"
        lines.append(f"{suite.integration} / {suite.name} -> {targets}")
        for case in suite_cases:
            lines.append(f"    {case.id}: {case.skip or ', '.join(dict.fromkeys(case.entities.values()))}")
    return lines


def head(profile: Profile) -> Callable[[Case], bool]:
    """The cases the lanes run: in_head's."""
    return lambda case: in_head(case, profile)


def tail(profile: Profile) -> Callable[[Case], bool]:
    """The cases the phases run after the lanes."""
    return lambda case: not in_head(case, profile)


def lane_order(suites: list[Suite]) -> dict[str, list[Suite]]:
    """The suites by integration, one lane each, in the order select gives them."""
    lanes: dict[str, list[Suite]] = {}
    for suite in suites:
        lanes.setdefault(suite.integration, []).append(suite)
    return lanes


def estimate(
    suites: list[Suite],
    registry: AdapterRegistry,
    restarting: bool,
    roaming: bool = False,
    profile: Profile | None = None,
    done: Done | None = None,
    resumed: Profile | None = None,
) -> int:
    """Each suite's rig_seconds, summed; with `profile`, the run is in lanes, so each suite's seconds are split by its
    share of cases in the lanes, which count as the longest lane, and the phases, which add up. Resuming (`done`, with
    the profile as `resumed`), each suite's seconds shrink to its share of cases left."""
    runnable = [suite for suite in suites if registry.adapter(suite.action, suite.integration)]
    profile_for = profile or resumed

    def seconds_left(suite: Suite) -> float:
        if not done or profile_for is None:
            return float(suite.file.rig_seconds)
        suite_cases = cases(suite, profile_for, None, adapters_for(suite, registry))
        unfinished_cases = [c for c in suite_cases if (suite.integration, suite.name, c.id) not in done]
        return suite.file.rig_seconds * (len(unfinished_cases) / len(suite_cases) if suite_cases else 1.0)

    if profile is None:
        seconds = float(sum(seconds_left(suite) for suite in runnable))
    else:
        lanes: dict[str, float] = {}
        seconds = 0.0
        for suite in runnable:
            suite_cases = cases(suite, profile, None, adapters_for(suite, registry))
            if done:
                suite_cases = [c for c in suite_cases if (suite.integration, suite.name, c.id) not in done]
            share = sum(in_head(case, profile) for case in suite_cases) / len(suite_cases) if suite_cases else 0.0
            lanes[suite.integration] = lanes.get(suite.integration, 0.0) + seconds_left(suite) * share
            seconds += seconds_left(suite) * (1 - share)
        seconds += max(lanes.values(), default=0.0)
    return round(seconds) + (RESTART_ESTIMATE_S if restarting else 0) + (ROAM_ESTIMATE_S if roaming else 0)


def deployed_trees(status_cmd: str) -> tuple[str | None, str | None]:
    """The synced and loaded tree hashes, each None if unknown: RIG_STATUS_CMD unset, never synced (exit 91), or a
    line missing. It prints `synced <tree>` and `loaded <tree>` lines; a bare hash, from older tooling, counts as both.
    """
    if not status_cmd:
        return None, None
    result = subprocess.run(shlex.split(status_cmd), capture_output=True, text=True)
    if result.returncode == 91:
        return None, None
    if result.returncode != 0:
        raise SystemExit(f"{status_cmd} failed ({result.returncode}): {result.stderr.strip()}")
    output = result.stdout.strip()
    if output and " " not in output:
        return output, output
    trees = dict(line.split(maxsplit=1) for line in output.splitlines() if " " in line.strip())
    return trees.get("synced"), trees.get("loaded")


def deploy_state(status_cmd: str) -> Deploy:
    """Whether HA runs this checkout's integration, and if not, whether origin has it to deploy.

    The deploy sync command installs a branch tip from GitHub, never the working tree, so a
    deploy needs the change pushed.
    """
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    commit = git("rev-parse", "HEAD")
    synced, loaded = deployed_trees(status_cmd)
    state = Deploy(
        commit=commit,
        local_tree=git("rev-parse", f"{commit}:{COMPONENT}"),
        synced_tree=synced,
        loaded_tree=loaded,
        branch=branch,
    )
    if git("status", "--porcelain", "--", COMPONENT):
        state.blocker = f"uncommitted changes under {COMPONENT}; commit and push them first"
        return state
    if not state.needed:
        return state
    remote = git("ls-remote", "origin", f"refs/heads/{branch}").split()
    if not remote:
        state.blocker = f"{branch} is not on origin; push it first"
    elif git("rev-parse", "--quiet", "--verify", f"{remote[0]}:{COMPONENT}", check=False) != state.local_tree:
        state.blocker = f"origin/{branch} does not match HEAD under {COMPONENT}; push first"
    return state


def check_deploy(env: dict, deploy: Deploy) -> None:
    """Raise SystemExit if the deploy cannot proceed: missing commands or a git blocker."""
    if not env.get("RIG_SYNC_CMD"):
        raise SystemExit("RIG_SYNC_CMD must be set in .env.rig to deploy (see ha_rig/env.example)")
    if not env.get("RIG_RESTART_CMD"):
        raise SystemExit("RIG_RESTART_CMD must be set in .env.rig to restart (see ha_rig/env.example)")
    if deploy.blocker:
        raise SystemExit(f"Cannot deploy: {deploy.blocker}.")


def approved() -> bool:
    """Whether the caller carries an explicit approval for restricted work, DEVICE_SIDE_RIG_RESTRICTED=1. The
    variable it replaced is refused, so it never quietly means something else."""
    if "DEVICE_SIDE_RIG_APPROVED" in os.environ:
        raise SystemExit(
            f"DEVICE_SIDE_RIG_APPROVED is gone. Unrestricted work needs no variable; restricted work goes while the "
            f"restricted sensor is off, or with {APPROVAL}=1, Jason's yes for that run (docs/rig.md, Authorization)."
        )
    return os.environ.get(APPROVAL) == "1"


def tty() -> bool:
    """Whether a human can be asked: stdin is a terminal."""
    try:
        return sys.stdin.isatty()
    except ValueError, AttributeError:  # closed or replaced
        return False


def proceed(decision: Decision, prompt: Callable[[str], str] = input) -> bool:
    """Whether the work goes on `decision`: asking its question, if any (no terminal answers no), and saying why
    not."""
    if not decision.go:
        print(f"{decision.reason} Nothing changed.")
        return False
    if decision.prompt is not None and not ask(decision.prompt, prompt):
        print("Not approved; nothing changed.")
        return False
    return True


class Authority:
    """What the rig may do now, from authorize: it reads the restricted sensor at most once, and only when restricted
    work asks."""

    def __init__(self, env: dict) -> None:
        self.env = env
        self.entity = restricted_sensor(env)
        self._sensor: tuple[bool, str] | None = None
        self._read = False

    def sensor(self) -> tuple[bool, str] | None:
        """rig_clear's answer on the restricted sensor, None when none is configured."""
        if not self._read:
            self._sensor, self._read = (rig_clear(self.env, self.entity) if self.entity else None), True
        return self._sensor

    def clear(self) -> bool:
        """Whether the sensor read off."""
        return bool((sensor := self.sensor()) and sensor[0])

    def watching(self, decision: Decision) -> contextlib.AbstractContextManager[None]:
        """The watcher `decision` arms, counting the gate's read of off as one since the run started; none for
        unrestricted work, which reads no sensor."""
        if not decision.watch:
            return watching(self.env, None)
        return watching(self.env, self.entity, self.clear())

    def decide(self, restricted: bool) -> Decision:
        return authorize(
            restricted=restricted, sensor=self.sensor() if restricted else None, approved=approved(), tty=tty()
        )

    def withholds(self, journal: Journal) -> str | None:
        """Why an orphan may not be put back now, None when it may: restoring it is restricted unless the profile
        that left it is pre-authorized (the journal from before per-run journals never is), and then it needs the
        sensor off or the approval; never a prompt."""
        name = journal.get("profile")
        if journal.path.name != LEGACY_NAME and pre_authorized(name, self.env):
            return None
        if authorize(restricted=True, sensor=self.sensor(), approved=approved(), tty=False).go:
            return None
        return f"profile {name or 'unknown'}, restricted"


def pre_authorized(name: str | None, env: dict) -> bool:
    """Whether the profile `name` is pre-authorized; one that is gone or won't load isn't."""
    if not name:
        return False
    try:
        return Profile.load(choose_profile(name, env)).pre_authorized
    except SystemExit, ValueError, OSError:
        return False


def ask(question: str, prompt: Callable[[str], str] = input) -> bool:
    """An explicit yes; no terminal (an agent's call) answers no."""
    try:
        return prompt(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _get(url: str, token: str) -> Any:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def restricted_sensor(env: dict) -> str | None:
    """The restricted sensor, RIG_RESTRICTED_ENTITY, or RIG_BLOCKER_ENTITY, its old name, saying so; None when
    neither is set."""
    if entity := env.get(SENSOR_KEY):
        return entity
    if entity := env.get(OLD_SENSOR_KEY):
        print(f"{OLD_SENSOR_KEY} in .env.rig is now {SENSOR_KEY}; rename it.")
        return entity
    return None


def rig_clear(env: dict, sensor: str) -> tuple[bool, str]:
    """Whether the rig may run unattended: `sensor`, an HA binary_sensor the household defines, is off. Any other
    state, the entity missing or HA unreachable is not clear. The reason says which."""
    state, reason = read_sensor(env, sensor)
    return state == "off", reason


def read_sensor(env: dict, entity: str) -> tuple[str | None, str]:
    """The sensor's state, None when it can't be read, and a reason naming it. A state HA restored at startup, before
    the template has evaluated, counts as unread."""
    try:
        reply = _get(f"{env.get('HA_URL', '')}/api/states/{entity}", env.get("HA_TOKEN", ""))
        state, restored = reply["state"], reply.get("attributes", {}).get("restored")
    except urllib.error.HTTPError as err:
        return None, f"{entity} {'is not in HA' if err.code == 404 else f'read failed: HTTP {err.code}'}"
    except (urllib.error.URLError, OSError, ValueError, TypeError, KeyError, AttributeError) as err:
        return None, f"can't read {entity}: {err}"
    if restored:
        return None, f"{entity} is still being restored by HA"
    return state, f"{entity} is {state}"


@contextlib.contextmanager
def watching(env: dict, sensor: str | None, clear: bool = False, interval: float = CLEAR_POLL_S) -> Iterator[None]:
    """Read `sensor` (None: nothing to watch) every `interval` seconds and, when it turns on, stop the run with one
    SIGTERM, which the run's own handlers take from there: the deploy finishes, the tests put the devices back, a
    probe's command gets it. Turning on is two reads in a row of a state in BLOCKED after a read of off since the run
    started (`clear`: the gate's read was off), so a run approved while it was on keeps going until it goes off and
    on again. A read that fails neither counts nor breaks the run of two: HA is unreachable during a deploy's
    restart."""
    stop = threading.Event()

    def watch() -> None:
        misses, seen_off = 0, clear
        while not stop.wait(interval):
            state, reason = read_sensor(env, sensor)
            if state is None:
                continue
            seen_off |= state == "off"
            misses = misses + 1 if state in BLOCKED else 0
            if misses >= 2 and seen_off:
                print(f"\nRig authorization revoked ({reason}); aborting.", flush=True)
                os.kill(os.getpid(), signal.SIGTERM)
                return

    thread = threading.Thread(target=watch, name="rig-clear", daemon=True)
    if sensor:
        thread.start()
    try:
        yield
    finally:
        stop.set()
        if sensor:
            thread.join()


def wait_until_ready(
    url: str,
    token: str,
    admin_token: str | None = None,
    timeout: float = READY_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Until HA reports RUNNING and lists deviceside.turn_on_for; exits the runner on timeout, saying why when HA
    answers without it, still starting or running."""
    deadline = time.monotonic() + timeout
    state = None
    while time.monotonic() < deadline:
        state = None  # only the last poll counts: HA that was RUNNING may have crashed since
        try:
            state = _get(f"{url}/api/config", token).get("state")
            if state == "RUNNING":
                services = {entry["domain"]: entry["services"] for entry in _get(f"{url}/api/services", token)}
                if SERVICE[1] in services.get(SERVICE[0], {}):
                    return
        except urllib.error.URLError, OSError, ValueError, TypeError, KeyError, AttributeError:
            pass
        sleep(3)
    if state == "RUNNING":
        raise SystemExit(f"HA is running but has no {'.'.join(SERVICE)}. {diagnose(url, admin_token)}")
    if state is not None:
        raise SystemExit(f"HA is still starting after {timeout:.0f}s ({state}). {diagnose(url, admin_token)}")
    raise SystemExit(f"HA did not report RUNNING within {timeout:.0f}s; check HA logs for errors.")


def diagnose(url: str, admin_token: str | None) -> str:
    """Why DeviceSide's actions are missing, from HA's config entries, which only an admin may read."""
    manifest = json.loads((REPO / COMPONENT / "manifest.json").read_text())
    waits_for = manifest.get("after_dependencies", [])
    if not admin_token:
        return (
            "Without HA_ADMIN_TOKEN in .env.rig the runner can't read config entries to say why; check DeviceSide "
            f"and the integrations it waits for ({', '.join(waits_for)}) in Settings > Devices & services."
        )
    try:
        entries = asyncio.run(config_entries(url, admin_token))
    except Exception as err:  # noqa: BLE001 - the diagnosis is a hint; the run is already stopping
        return f"Could not read config entries to say why: {err!r}."
    return explain(entries, waits_for)


async def config_entries(url: str, admin_token: str) -> list[dict]:
    admin = HAClient(url, admin_token)
    try:
        await admin.connect()
        return await admin.command({"type": "config_entries/get"})
    finally:
        await admin.close()


def explain(entries: list[dict], waits_for: list[str]) -> str:
    """DeviceSide's entry state and each integration it waits for whose enabled entry isn't loaded: HA sets up
    DeviceSide only after their entries finish setting up, so one stuck in setup holds it back."""
    own = [entry["state"] for entry in entries if entry["domain"] == SERVICE[0]]
    if not own:
        return (
            "On a first load, add the integration (Settings > Devices & services > Add integration > DeviceSide), "
            "then rerun."
        )
    stuck = [
        f"{entry['domain']} ({entry['state']})"
        for entry in entries
        if entry["domain"] in waits_for and entry["state"] != "loaded" and not entry.get("disabled_by")
    ]
    why = (
        f"it waits for {', '.join(stuck)}"
        if stuck
        else f"none of the integrations it waits for ({', '.join(waits_for)}) is stuck; check HA's logs"
    )
    return (
        f"DeviceSide's entry is {', '.join(own)}; {why}. "
        "DeviceSide actions are unavailable in the house until this is fixed."
    )


@dataclass(slots=True)
class Setup:
    """What a run reads before it takes its locks, none of which changes anything; take() reads `deploy` again once it
    holds them."""

    env: dict
    profile: Profile
    registry: AdapterRegistry
    suites: list[Suite]
    roaming: bool
    grouping: bool
    deploy: Deploy | None = None  # None for --check, which deploys nothing
    progress: Progress | None = None  # None where no record is possible: --no-deploy, --roam-check, --check
    selected: list[Suite] = field(default_factory=list)  # every suite selected; `suites` are those with cases left
    coverage: list[SuiteCoverage] = field(default_factory=list)  # each selected suite's, with progress
    planned_tree: str | None = None  # the integration tree the coverage was planned at

    @property
    def done(self) -> Done:
        return self.progress.done() if self.progress is not None else {}

    def deploying(self, args: argparse.Namespace) -> bool:
        """--roam-check deploys nothing, as it runs none of our code."""
        return self.deploy is not None and self.deploy.needed and not args.no_deploy and not args.roam_check


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    command: list[str] = []
    if "--" in argv:
        argv, command = argv[: argv.index("--")], argv[argv.index("--") + 1 :]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", help="the profile to run (profiles/NAME.yaml); needed if there are two")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and stop before the gate")
    parser.add_argument(
        "--check",
        action="store_true",
        help="check the run's devices are in HA, on their integration and available, and where its roaming bulbs "
        "are, then stop: no lock, no deploy, nothing changed",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="say which selected suites are covered at this checkout's HEAD, by a fresh record or saved cases, and "
        "which devices the missing cases need, then stop: no lock, no deploy, nothing changed",
    )
    parser.add_argument("--no-deploy", action="store_true", help="test whatever HA already runs")
    parser.add_argument(
        "--integration",
        action="append",
        choices=sorted(TOLERANCE_S),
        help="limit to an integration key (repeatable); with --wait-free, wait for it",
    )
    parser.add_argument("--action", action="append", help="limit to an action's step files (repeatable)")
    parser.add_argument("-k", dest="keyword", help="run only test cases whose id contains this text")
    parser.add_argument(
        "--unattended",
        action="store_true",
        help="ask no one: an observed test whose calls no observed pass covers is reported not observed",
    )
    parser.add_argument(
        "--reobserve",
        action="store_true",
        help="run observed tests even where an observed pass covers their calls, as after a major firmware upgrade",
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="run every case one at a time, with no lanes: to tell a case that fails in a lane from one that fails",
    )
    parser.add_argument(
        "--debug-logs",
        action="store_true",
        help="run HA's DeviceSide and the run's integrations, and Z2M, at debug, and keep the run's captured logs "
        "(they are kept after a failure whatever this says)",
    )
    parser.add_argument(
        "--deploy-only",
        action="store_true",
        help="deploy this checkout's branch and restart HA, then release the locks: no profile, no tests; unrestricted",
    )
    parser.add_argument(
        "--for",
        dest="budget",
        type=duration,
        metavar="DURATION",
        help="start no case after this long (30m, 90s, 1h15m), let in-flight ones finish, put back and exit 75; "
        "rerun the same way to resume",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="rerun the selected suites whole, whatever their records and saved cases say",
    )
    parser.add_argument(
        "--ignore-unreachables",
        action="store_true",
        help="run what the devices that are up allow: skip the cases of an unavailable fixed entity, and while any "
        "is, of fixed groups whose members the profile doesn't list and of fixed scenes; their suites write no record",
    )
    parser.add_argument("--recover", action="store_true", help="put back what an aborted run left changed, then stop")
    parser.add_argument(
        "--promote",
        type=Path,
        metavar="DIR",
        help="copy the test records a run staged in DIR (it prints the command) into the checkout's docs/test-records "
        "and take their suites out of the ledger; no lock, no HA",
    )
    parser.add_argument(
        "--roam-check", action="store_true", help="move the profile's roam bulbs to every network and home, no suites"
    )
    parser.add_argument(
        "--wait-free",
        action="store_true",
        help="wait until no rig run holds the locks (with --integration, those integrations' and HA exclusively), "
        "without taking them; another run may take them first",
    )
    parser.add_argument(
        "--hold",
        action="append",
        metavar="INTEGRATION",
        choices=sorted(TOLERANCE_S),
        help="hold this integration's lock (repeatable) and HA shared while running the command after --, for a probe",
    )
    parser.add_argument(
        "--exclusive",
        action="store_true",
        help="with a command after --: hold HA exclusively, for a probe that restarts HA or reloads DeviceSide",
    )
    parser.add_argument(
        "--visual",
        choices=("none", "profile"),
        help="with a command after --: declare what the probe changes that anyone could see, making it unrestricted: "
        "none, nothing; profile, only what the pre-authorized profile covers. Left out, the probe is restricted",
    )
    args = parser.parse_args(argv)
    args.command = command
    approved()  # refuses the variable it replaced, in every mode
    refuse_conflicts(parser, args, command)
    args.dry_run |= args.check  # like a dry run, it holds no lock and changes nothing
    # A dry run or a promote holds no lock and sets no color, so it clears none.
    colors = args.wait_free or not (args.dry_run or args.promote)
    try:
        with handling_signals(leave):  # a run killed while it waits still clears its color
            return wait_or_run(args)
    finally:
        if colors:
            locks.workspace_color(None)


def refuse_conflicts(parser: argparse.ArgumentParser, args: argparse.Namespace, command: list[str]) -> None:
    """Stop, through argparse, on flags that don't go together."""
    if args.promote and any(value for name, value in vars(args).items() if name != "promote"):  # command included
        parser.error("--promote takes only the staged directory")
    refuse_mode_conflicts(parser, args, command)


def refuse_mode_conflicts(parser: argparse.ArgumentParser, args: argparse.Namespace, command: list[str]) -> None:
    """refuse_conflicts, for the test-run modes and probes."""
    if args.deploy_only and (
        args.profile
        or args.no_deploy
        or args.check
        or args.coverage
        or args.roam_check
        or args.recover
        or args.wait_free
        or args.hold
        or args.exclusive
        or command
        or args.integration
        or args.action
        or args.keyword
        or args.serial
        or args.unattended
        or args.debug_logs
        or args.budget
        or args.fresh
        or args.ignore_unreachables
    ):
        parser.error("--deploy-only takes only --dry-run: it needs no profile and runs no test")
    if (args.budget or args.fresh) and (args.check or args.roam_check or args.recover or args.wait_free or command):
        parser.error("--for and --fresh are for a test run")
    if args.ignore_unreachables and (args.roam_check or args.recover or args.wait_free or command):
        parser.error("--ignore-unreachables is for a test run or --check")
    if args.check and (args.recover or args.roam_check or args.wait_free):
        parser.error("--check can't be combined with --recover, --roam-check or --wait-free")
    if args.coverage and (
        args.check
        or args.dry_run
        or args.no_deploy
        or args.roam_check
        or args.recover
        or args.wait_free
        or command
        or args.serial
        or args.debug_logs
        or args.budget
        or args.fresh
        or args.ignore_unreachables
    ):
        parser.error("--coverage takes only --profile, --integration, --action, -k and --unattended")
    if args.debug_logs and (args.check or args.recover or args.wait_free or command):
        parser.error("--debug-logs is for a run or --roam-check")
    if args.visual and not command:
        parser.error("--visual is for a probe, with a command after --")
    if bool(args.hold or args.exclusive) != bool(command):
        parser.error("--hold and --exclusive need a command after --, and a command after -- needs one of them")
    if command and (args.check or args.recover or args.roam_check or args.wait_free):
        parser.error("a command after -- can't be combined with a run's modes; --dry-run only says if it may go")


def duration(text: str) -> float:
    """--for's DURATION, in seconds: hours, minutes and seconds, each optional, in that order (1h15m, 30m, 90s)."""
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text.strip())
    if not text.strip() or match is None or not any(match.groups()):
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (try 30m, 90s or 1h15m)")
    hours, minutes, seconds = (int(group or 0) for group in match.groups())
    if not (total := hours * 3600 + minutes * 60 + seconds):
        raise argparse.ArgumentTypeError("the duration must be more than 0")
    return float(total)


def leave(signum: int, _frame: object) -> None:
    """Stop a run that holds nothing yet, with the shell's exit code for the signal."""
    raise SystemExit(128 + signum)


def refuse_checkout(args: argparse.Namespace) -> None:
    """Refuse a checkout the run may not use. --check, --dry-run and --recover deploy and record nothing in it, so
    whose code it is doesn't matter to them."""
    if not (args.check or args.dry_run or args.recover):
        target.refuse_unowned_checkout(REPO)
    target.refuse_moved_files(REPO, MAIN_CHECKOUT)


def wait_or_run(args: argparse.Namespace) -> int:
    refuse_checkout(args)
    try:
        terms.write(REPO)  # deviceside's household check reads the profiles' terms from here
    except ValueError as err:  # a bad profile must not stop --recover; a run's own refuses it in prepare
        print(f"deviceside's household terms not refreshed: {err}")
    if args.promote:
        return promote_staged(args.promote)
    directory = common_dir()
    if args.wait_free:
        locks.wait_free(directory, args.integration)
        return 0
    if args.command:
        return hold_command(args, directory)
    if args.recover:
        return recover_all(args, directory)
    if args.deploy_only:
        return deploy_only(args, directory)
    if args.coverage:
        return coverage_only(args, directory)
    setup = prepare(args, directory)
    held = None if args.dry_run else take(args, setup, directory)  # waits for the locks, then holds them until exit
    try:
        with end_on_signals():
            return run_held(args, setup, held, directory)
    except Aborted as err:
        print(err)
        return 1
    finally:
        if held is not None:
            held.close()


def promote_staged(directory: Path) -> int:
    """--promote: the records a run staged in `directory` into the checkout it staged them for; 0 once all are."""
    staging = Staging.load(directory.expanduser().resolve())
    if not target.is_deviceside(staging.repo):
        raise SystemExit(f"{staging.repo}, where these records were staged for, is no longer a deviceside checkout")
    left = len(staging.records) - len(staging.promote())
    return 1 if left else 0


def deploy_only(args: argparse.Namespace, directory: Path) -> int:
    """--deploy-only: deploy the checkout's branch and wait for HA, holding HA exclusively with no integration, then
    let go. No profile, journal or baseline: it moves no device, so it is unrestricted and leaves orphans to the next
    test run's replay."""
    env = load_env()
    if not (env.get("HA_URL") and env.get("HA_TOKEN")):
        raise SystemExit("HA_URL and HA_TOKEN must be set in .env.rig (see ha_rig/env.example)")
    status = env.get("RIG_STATUS_CMD", "")
    deploy = deploy_state(status)
    print_deploy(deploy, deploy.needed)
    if not deploy.needed:
        print("HA already runs this checkout's tree; nothing to deploy.")
        return 0
    check_deploy(env, deploy)
    if args.dry_run:
        return 0
    held = locks.hold(Claim("exclusive", frozenset()), deploy.branch, directory)
    try:
        deploy = deploy_state(status)  # another run may have deployed this tree while this one waited
        if not deploy.needed:
            print("Another run deployed this checkout's tree meanwhile; nothing to deploy.")
            return 0
        check_deploy(env, deploy)
        with end_on_signals():
            deploy_and_wait(env, deploy.branch)
    except Aborted as err:
        print(err)
        return 1
    finally:
        held.close()
    print(f"Deployed {deploy.branch} ({git('rev-parse', '--short', deploy.commit)}); HA is back. Locks released.")
    return 0


def prepare(args: argparse.Namespace, directory: Path | None = None) -> Setup:
    """The profile, the suites, and, but for --check, the deploy state and the saved progress (in `directory`, for a
    run that can write records), which narrows the suites to those with cases left; a run also checks .env.rig has
    its tokens."""
    env = load_env()
    try:
        profile = Profile.load(choose_profile(args.profile, env))
    except ValueError as err:
        raise SystemExit(f"Fix the profile: {err}") from err
    registry = AdapterRegistry.discover()
    suites = run_suites(args, profile, registry)
    setup = Setup(
        env,
        profile,
        registry,
        suites,
        roams(profile, suites, args.roam_check),
        makes_groups(profile, suites, args.roam_check),
        selected=suites,
    )
    if args.check:
        return setup
    setup.deploy = deploy_state(env.get("RIG_STATUS_CMD", ""))
    if directory is not None and not args.no_deploy and not args.roam_check:
        plan_coverage(args, setup, directory)
    if not args.dry_run:
        check_tokens(env, setup.suites, registry, setup.roaming or setup.grouping)
    return setup


def plan_coverage(args: argparse.Namespace, setup: Setup, directory: Path) -> None:
    """Load the ledger and plan only what isn't covered (ha_rig/coverage.py): a selected suite with a fresh record, or
    a saved result for every case, is left out, and the rest run only their cases with no saved result that holds,
    those the keyword keeps. What the run holds, moves, checks and estimates shrinks with it. --fresh ignores the
    records and drops the saved cases of the selected suites."""
    progress = Progress.load(directory, setup.profile.name)
    # A record names one HA version, so cases saved on another rerun, and new ones note it from the first.
    if (version := ha_version(setup.env)) is not None:
        progress.check_ha(version)
    elif progress.entries:
        if not args.dry_run:
            raise SystemExit("Can't read HA's version, which the saved cases are checked against; is HA up?")
        print("HA's version can't be read, so saved cases on an older HA may still count below.")
    setup.progress = progress
    setup.planned_tree = setup.deploy.local_tree
    setup.coverage = coverage.of(
        setup.selected,
        setup.profile,
        setup.registry,
        REPO,
        setup.deploy.commit,
        progress,
        fresh=args.fresh,
        unattended=args.unattended,
    )
    covered = {(c.suite.integration, c.suite.name) for c in setup.coverage if c.covered}
    done, profile, registry = setup.done, setup.profile, setup.registry
    remaining = [
        suite
        for suite in setup.selected
        if (suite.integration, suite.name) not in covered
        and (not runnable_suite(suite, registry) or left(suite, profile, registry, done, args.keyword))
    ]
    setup.suites = remaining
    setup.roaming = roams(setup.profile, remaining)
    setup.grouping = makes_groups(setup.profile, remaining)


def left(suite: Suite, profile: Profile, registry: AdapterRegistry, done: Done, keyword: str | None) -> list[str]:
    """The ids of the suite's cases (those `keyword` keeps) with no saved result; none for a suite with no adapter,
    which runs nothing."""
    if not runnable_suite(suite, registry):
        return []
    suite_cases = cases(suite, profile, keyword, adapters_for(suite, registry))
    return [case.id for case in suite_cases if (suite.integration, suite.name, case.id) not in done]


def ha_version(env: dict) -> str | None:
    """HA's version, or None when HA can't be read."""
    try:
        return _get(f"{env.get('HA_URL', '').rstrip('/')}/api/config", env.get("HA_TOKEN", "")).get("version")
    except urllib.error.URLError, OSError, ValueError, TypeError, AttributeError:
        return None


def claim(args: argparse.Namespace, setup: Setup) -> Claim:
    """What the run must hold: each integration with a suite that has an adapter, the integrations their entities draw
    power through, and the networks its roaming bulbs visit, home included; HA exclusively when it deploys, which
    restarts HA, or runs a suite that reloads the entry (DeviceSide options, whose save does, or entry steps); HA
    shared otherwise."""
    integrations = {suite.integration for suite in runnable(setup)}
    integrations |= supplies(setup.profile, integrations)
    if setup.roaming:
        integrations |= {network for network, _ in phases(setup.suites, setup.profile, roaming=True) if network}
    if setup.grouping:
        integrations.add(setup.profile.roam_home)
    exclusive = setup.deploying(args) or reloads(setup)
    return Claim("exclusive" if exclusive else "shared", frozenset(integrations))


def runnable(setup: Setup) -> list[Suite]:
    """The suites with an adapter, which the run can run."""
    return runnable_suites(setup.suites, setup.registry)


def runnable_suites(suites: list[Suite], registry: AdapterRegistry) -> list[Suite]:
    return [suite for suite in suites if runnable_suite(suite, registry)]


def runnable_suite(suite: Suite, registry: AdapterRegistry) -> bool:
    return registry.adapter(suite.action, suite.integration) is not None


def reloads(setup: Setup) -> bool:
    """Whether a suite the run can run reloads the entry under every other run: it has DeviceSide options, whose
    save does, or entry steps."""
    return any(suite.file.reloads for suite in runnable(setup))


def supplies(profile: Profile, integrations: set[str]) -> set[str]:
    """The integrations whose switches power an entity of `integrations`: a case cuts or needs that power."""
    where = {entity.entity_id: key for key, entities in profile.integrations.items() for entity in entities}
    return {
        where[entity.powered_by]
        for integration in integrations
        for entity in profile.integrations.get(integration, ())
        if entity.powered_by in where
    }


def take(args: argparse.Namespace, setup: Setup, directory: Path) -> Held:
    """Wait for the run's claim and hold it. Another run may have deployed meanwhile, so the deploy state is read again;
    if the run now needs more (it must deploy after all), it lets go and waits for that, once: HA held exclusively,
    nothing can change it again."""
    holder = git("rev-parse", "--abbrev-ref", "HEAD")
    held = locks.hold(claim(args, setup) | orphaned(setup, directory), holder, directory)
    setup.deploy = deploy_state(setup.env.get("RIG_STATUS_CMD", ""))
    if not held.claim.covers(needed := claim(args, setup)):
        held.close()
        print(f"HA changed while this run waited; it now needs {needed}.")
        held = locks.hold(held.claim | needed, holder, directory)
        setup.deploy = deploy_state(setup.env.get("RIG_STATUS_CMD", ""))
    if setup.planned_tree is not None and setup.deploy.local_tree != setup.planned_tree:
        held.close()  # the coverage was planned, and the suites fingerprinted, at the tree the plan read
        raise SystemExit("The checkout's integration tree changed while this run waited; run it again.")
    return held


def orphaned(setup: Setup, directory: Path) -> Claim:
    """What a resumed run must also hold to put back the aborted runs of its own profile: the stopped run that saved
    its progress left changes the remaining suites may not touch, and replay only puts back what the run's claim
    covers. Nothing for a fresh run. The journals are read, then released at once."""
    wanted = Claim("shared", frozenset())
    if not setup.progress:
        return wanted
    for orphan in orphans(directory):
        try:
            if orphan.get("profile") == setup.profile.name:
                wanted |= needs(orphan)
        finally:
            orphan.release()
    return wanted


def run_held(args: argparse.Namespace, setup: Setup, held: Held | None, directory: Path) -> int:
    """The run, once its locks are held (none for a dry run or --check)."""
    env, profile, registry, suites = setup.env, setup.profile, setup.registry, setup.suites
    authority = Authority(env)
    # Decided before the replay, which restores only what the rig may put back now; asked after the plan.
    decision = None if args.check else authority.decide(restricted=not profile.pre_authorized)
    homes = replay(env, held, directory, authority)
    if args.check:
        return check_only(setup, args.keyword, homes, claim(args, setup), directory, args.ignore_unreachables)
    deploy = setup.deploy
    deploying = setup.deploying(args)
    lanes = not args.serial
    print_plan(
        profile, suites, registry, args.keyword, deploy, deploying, setup.roaming, setup.grouping, lanes, setup.done
    )
    print_resume(args, setup)
    if setup.progress is not None and not runnable(setup):
        print("Nothing to run: every selected suite is covered (--fresh reruns them).")
        if unwritten := pending(setup):
            print(
                f"{len(unwritten)} of them have no record yet: promote theirs with the --promote command the run that "
                "finished them printed, or rerun them with --fresh."
            )
        return 0
    print(f"Locks: {held.claim if held else claim(args, setup)}")
    if args.debug_logs:
        print_debug_logs(env, suites, setup.roaming)

    if deploying:
        check_deploy(env, deploy)
    if not args.roam_check:
        print()
        problems = asyncio.run(
            check_devices(
                env, suites, profile, registry, args.keyword, setup.roaming, homes, setup.done, args.ignore_unreachables
            )
        )
        print_unreachable(suites, profile, registry, args.keyword, setup.done)
        if problems and not args.dry_run:
            raise SystemExit("Power or fix the devices above, then run again.")
    whose = f"profile {profile.name} is pre-authorized. " if profile.pre_authorized else ""
    print(f"Authorization: {whose}{decision.reason}")
    if args.dry_run:
        return 0
    if not proceed(decision):
        return 1

    journal = Journal.start(directory)
    try:
        with authority.watching(decision):
            return run_approved(args, setup, held, journal, deploying, lanes)
    finally:
        journal.release()


def print_resume(args: argparse.Namespace, setup: Setup) -> None:
    """The selected suites left out as covered, how many cases of the rest are saved, and the budget."""
    for suite_coverage in setup.coverage:
        if suite_coverage.covered:
            print(f"{suite_coverage.suite.integration} / {suite_coverage.suite.name}: {suite_coverage}, skipped")
    running = {(suite.integration, suite.name) for suite in setup.suites}
    if done := [key for key in setup.done if key[:2] in running]:
        registry = setup.registry
        total = sum(
            len(cases(suite, setup.profile, None, adapters_for(suite, registry)))
            for suite in setup.suites
            if runnable_suite(suite, registry)
        )
        print(f"Resuming: {len(done)} of the {total} cases of the suites to run are saved; --fresh to start over.")
    if args.budget:
        print(f"Budget: no case starts after {args.budget:.0f} s from approval, deploy included; rerun to resume.")


def print_debug_logs(env: dict, suites: list[Suite], roaming: bool) -> None:
    """The brief's note that --debug-logs raises the run's logs to debug, which may shift timing cases, and, for a
    run that touches Z2M, a warning when Z2M won't send its debug lines to MQTT."""
    z2m = touches_z2m(suites, roaming)
    print(
        f"Debug logs: {', '.join(ha_domains({s.integration for s in suites}, roaming))} in HA"
        f"{' and Z2M' if z2m else ''} at debug for the run. Debug logging costs CPU, so timing cases may read "
        "differently."
    )
    if z2m and env.get("HA_ADMIN_TOKEN"):
        try:
            asyncio.run(with_admin(env, warn_z2m_debug))
        except Exception as err:  # noqa: BLE001 - the warning is a courtesy: the run goes on without it
            print(f"Couldn't read Z2M's settings to check its debug lines reach MQTT: {err}")


def touches_z2m(suites: list[Suite], roaming: bool) -> bool:
    """Whether the run's logs include Z2M's: a Z2M suite, or roaming."""
    return roaming or any(suite.integration == "zigbee2mqtt" for suite in suites)


async def warn_z2m_debug(admin: HAClient) -> None:
    config = (await admin.z2m_info()).get("config") or {}
    if not (config.get("advanced") or {}).get("log_debug_to_mqtt_frontend"):
        print(
            "Warning: Z2M's advanced.log_debug_to_mqtt_frontend is off, so its debug lines won't reach the captured "
            "log. Turn it on and restart Z2M; the rig never sets it."
        )


async def with_admin(env: dict, call: Callable[[HAClient], Awaitable[None]]) -> None:
    """`call` on an admin connection to HA, closed after."""
    admin = HAClient(env["HA_URL"], env["HA_ADMIN_TOKEN"])
    await admin.connect()
    try:
        await call(admin)
    finally:
        await admin.close()


def run_approved(
    args: argparse.Namespace, setup: Setup, held: Held, journal: Journal, deploying: bool, lanes: bool
) -> int:
    """The deploy and the tests, once the run is approved."""
    env, deploy = setup.env, setup.deploy
    budget = Budget(time.monotonic() + args.budget) if args.budget else None
    deploy_and_wait(env, deploy.branch if deploying else None)
    if deploying and not reloads(setup):  # HA is back, and nothing left restarts or reloads it under other runs
        held.share_ha()
    commit = tested_commit(env.get("RIG_STATUS_CMD", ""), deploy, deploying)
    return run_tests(
        run_rig(
            env,
            setup.suites,
            setup.profile,
            setup.registry,
            args.keyword,
            args.unattended,
            commit,
            journal,
            roaming=setup.roaming,
            grouping=setup.grouping,
            lanes=lanes,
            claim=held.claim,
            debug_logs=args.debug_logs,
            reobserve=args.reobserve,
            progress=setup.progress,
            selected=setup.selected,
            budget=budget,
            pending=pending(setup),
        ),
        aborted=aborted(setup, args.keyword) if setup.progress is not None else None,
    )


def pending(setup: Setup) -> set[tuple[str, str]]:
    """The selected suites with every case saved and no fresh record: an earlier run finished them but didn't end
    cleanly, so their records were staged and never promoted. A run that ends cleanly writes theirs too."""
    return {
        (c.suite.integration, c.suite.name)
        for c in setup.coverage
        if c.complete and not c.freshness.fresh and not c.narrows  # a narrowing record is kept, never rewritten
    }


def aborted(setup: Setup, keyword: str | None) -> Callable[[], int]:
    """What a run with progress does once a signal stopped it: the report of every case saved so far (no records,
    as it is unfinished), what is left, and RESUMABLE."""

    def stop() -> int:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        report_saved(setup.progress, setup.selected, setup.registry, setup.profile, stamp)
        return say_stopped(setup.progress, setup.suites, setup.profile, setup.registry, keyword, "aborted")

    return stop


def say_stopped(
    progress: Progress | None,
    suites: list[Suite],
    profile: Profile,
    registry: AdapterRegistry,
    keyword: str | None,
    why: str,
) -> int:
    """Say the run stopped with cases left, and how to resume; RESUMABLE. Its saved failures are dropped first, to
    rerun whole."""
    if progress is None:
        print(f"Stopped ({why}); with --no-deploy nothing is saved to resume from.")
        return RESUMABLE
    progress.drop_failed()
    done = progress.done()
    count = sum(len(left(suite, profile, registry, done, keyword)) for suite in suites)
    print(f"Stopped with {count} cases left ({why}); rerun the same command to resume.")
    return RESUMABLE


def check_only(
    setup: Setup,
    keyword: str | None,
    homes: set[str],
    wanted: Claim,
    directory: Path,
    ignore_unreachables: bool = False,
) -> int:
    """--check: the device check alone, naming the runs that hold locks this run would claim, whose moves and pauses
    it would see."""
    if held := locks.holders(directory, wanted):
        print("Runs hold locks this run needs; what they have moved or paused shows below as it is now:")
        for line in held:
            print(f"  {line}")
    problems = asyncio.run(
        check_devices(
            setup.env,
            setup.suites,
            setup.profile,
            setup.registry,
            keyword,
            setup.roaming,
            homes,
            {},
            ignore_unreachables,
        )
    )
    print_unreachable(setup.suites, setup.profile, setup.registry, keyword)
    return 1 if problems else 0


def coverage_only(args: argparse.Namespace, directory: Path) -> int:
    """--coverage: each selected suite's coverage at the checkout's HEAD, as a run would plan it, and the devices its
    missing cases need, which is what to power up for the next run. Reads the records, the ledger and HA's version;
    changes nothing, the ledger included. With no selection it is the whole profile: the release candidate's view.
    Returns 0 when every suite is covered."""
    env = load_env()
    try:
        profile = Profile.load(choose_profile(args.profile, env))
    except ValueError as err:
        raise SystemExit(f"Fix the profile: {err}") from err
    registry = AdapterRegistry.discover()
    suites = run_suites(args, profile, registry)
    commit = git("rev-parse", "HEAD")
    print(f"Coverage of profile {profile.name} at {git('rev-parse', '--short', commit)} (HEAD):")
    if git("status", "--porcelain", "--", COMPONENT):
        print(f"  Uncommitted changes under {COMPONENT} aren't counted: a run deploys only what is committed.")
    progress = Progress.load(directory, profile.name)
    if progress.entries:
        if (version := ha_version(env)) is None:
            print("  HA's version can't be read, so saved cases on an older HA may still count below.")
        else:
            progress.check_ha(version)
    plan = coverage.of(suites, profile, registry, REPO, commit, progress, unattended=args.unattended)
    observations = load_observations(TEST_RECORDS_DIR, {suite.integration for suite in suites})
    hints = coverage.observed_hints(plan, progress, observations)
    for line in coverage_lines(plan, profile, args.keyword, hints):
        print(line)
    return 0 if all(suite_coverage.covered for suite_coverage in plan) else 1


def coverage_lines(
    plan: list[SuiteCoverage],
    profile: Profile,
    keyword: str | None = None,
    hints: dict[tuple[str, str, str], str] | None = None,
) -> list[str]:
    """One line per suite, its missing cases (those `keyword` keeps) under a partial one, and any with a hint
    (coverage.observed_hints), then the total and the devices every missing case needs, their supplies included."""
    hints = hints or {}
    lines, needed = [], {}
    power = profile.power()
    for suite_coverage in plan:
        suite = suite_coverage.suite
        lines.append(f"  {suite.integration} / {suite.name}: {suite_coverage}")
        if suite_coverage.covered:
            continue
        missing = [case for case in suite_coverage.missing if not keyword or keyword in case.id]
        for case in missing:
            devices = [] if case.skip else list(dict.fromkeys([*case.entities.values(), *case.members]))
            devices += [power[e] for e in devices if e in power and power[e] not in devices]
            needed.update(dict.fromkeys(devices))
            hint = hints.get((suite.integration, suite.name, case.id))
            if suite_coverage.saved or hint:
                lines.append(f"      {case.id}: {', '.join(devices) or case.skip}" + (f" ({hint})" if hint else ""))
    covered = sum(suite_coverage.covered for suite_coverage in plan)
    left = sum(len(suite_coverage.missing) for suite_coverage in plan if not suite_coverage.covered)
    lines.append(f"{covered} of {len(plan)} suites covered; {left} cases missing.")
    if needed:
        lines.append(f"The missing cases need: {', '.join(sorted(needed))}")
    return lines


def hold_command(args: argparse.Namespace, directory: Path) -> int:
    """A command after --: run a probe holding the integrations --hold names and HA, shared or with --exclusive
    exclusively, with the profile's automations for them paused, and return its exit code. No case journal: the
    probe puts back what it changes. It is restricted unless --visual declares it isn't (probe_restricted), and a
    restricted probe that went because the sensor was off is checked again once it holds its locks. Holding them, it
    puts back what aborted runs left under them, as replay does, and only warns of an orphan it may not. A journal is
    started all the same, never written, as the name the baseline knows the probe by, alive while it runs: a crashed
    probe's pauses are pruned like a run's."""
    wanted = Claim("exclusive" if args.exclusive else "shared", frozenset(args.hold or ()))
    env = load_env()
    try:
        profile = Profile.load(choose_profile(args.profile, env))
    except ValueError as err:
        raise SystemExit(f"Fix the profile: {err}") from err
    restricted = probe_restricted(args.visual, profile)
    pausing = profile.paused_by(wanted.integrations)
    if pausing and not (env.get("HA_URL") and env.get("HA_TOKEN")):
        raise SystemExit("HA_URL and HA_TOKEN must be set in .env.rig to pause the profile's automations")
    holder = f"{git('rev-parse', '--abbrev-ref', 'HEAD')} (probe: {shlex.join(args.command)})"
    authority = Authority(env)
    decision = authority.decide(restricted)
    if args.dry_run:
        print(f"Authorization: {decision.reason}")
        return 0
    if not proceed(decision):
        return 1
    with locks.hold(wanted, holder, directory) as held:
        # A probe may wait long for its locks. One that went on the sensor alone reads it again; one approved, by the
        # variable or a human's yes, isn't asked again.
        if restricted and authority.clear() and not approved():
            authority = Authority(env)
            if not authority.clear() and not proceed(decision := authority.decide(restricted)):
                return 1
        # Put back what the locks cover and the rig may; warn of the rest, which the probe starts over all the same.
        replay(env, held, directory, authority, warn=True)
        journal = Journal.start(directory)
        try:
            with authority.watching(decision):
                if pausing:
                    asyncio.run(with_ha(env, lambda ha: Baseline.of(ha, journal, "pause").pause(pausing)))
                return run_command(args.command)
        finally:
            if pausing:
                asyncio.run(with_ha(env, lambda ha: Baseline.of(ha, journal, "pause").release()))
            journal.release()


def probe_restricted(visual: str | None, profile: Profile) -> bool:
    """Whether a probe is restricted work, as its --visual declares, trusted: `none` moves nothing anyone sees,
    `profile` only what the pre-authorized profile covers; undeclared, it is restricted."""
    if visual == "none":
        return False
    if visual == "profile":
        if not profile.pre_authorized:
            raise SystemExit(f"--visual profile needs a pre-authorized profile, and {profile.name} isn't")
        return False
    return True


async def with_ha(env: dict, call: Callable[[HAClient], Awaitable[None]]) -> None:
    """`call` on a connection to HA, the admin one where .env.rig has it (the baseline's prune may set Z2M's level
    or options, which only an admin may), closed after."""
    ha, admin = await connect(env)
    try:
        await call(admin or ha)
    finally:
        await ha.close()
        if admin:
            await admin.close()


def run_command(command: list[str]) -> int:
    """Run `command` in its own session, passing on Ctrl-C, SIGTERM and SIGHUP, so it gets each once, through here;
    its exit code, or 128 plus the number of the signal that ended it."""
    try:
        process = subprocess.Popen(command, start_new_session=True)
    except OSError as err:
        raise SystemExit(f"Can't run {command[0]}: {err}") from None
    with handling_signals(lambda signum, _frame: process.send_signal(signum)):
        code = process.wait()
    return 128 - code if code < 0 else code


def recover_all(args: argparse.Namespace, directory: Path) -> int:
    """--recover: hold everything, so no run is going, and put back what every aborted run left, the journal from
    before per-run journals included, then what the baseline holds for no live run. Each orphan is restricted work
    unless the profile that left it is pre-authorized (Authority.withholds); one the rig may not put back now is left,
    and named. Nonzero while something is left."""
    env = load_env()
    authority = Authority(env)
    held = None if args.dry_run else locks.hold(Claim.everything(), git("rev-parse", "--abbrev-ref", "HEAD"), directory)
    try:
        with end_on_signals():
            # The journal from before per-run journals is older than any of them; orphans come oldest first.
            journals = [*filter(None, [Journal(directory / LEGACY_NAME)]), *orphans(directory)]
            if not journals:
                print("No aborted run left anything to put back.")
            for journal in journals:
                try:
                    if why := authority.withholds(journal):
                        print(left_restricted(journal, why))
                    elif args.dry_run:
                        print(f"An aborted run left changes in {journal.path}; --recover puts them back.")
                    else:
                        asyncio.run(recover_rig(env, journal))
                finally:
                    journal.release()
            if not args.dry_run and (directory / BASELINE_NAME).exists():
                asyncio.run(prune_baseline(env, directory))
            return 1 if any(journals) or (directory / BASELINE_NAME).exists() else 0
    except Aborted as err:
        print(err)
        return 1
    finally:
        if held is not None:
            held.close()


def run_suites(args: argparse.Namespace, profile: Profile, registry: AdapterRegistry) -> list[Suite]:
    """The suites the arguments select: none for --roam-check, which needs a profile that roams."""
    if args.roam_check:
        if not profile.roam:
            raise SystemExit(f"--roam-check needs a profile with roam:, and {profile.name} has none")
        return []
    step_files = load_step_files()
    check_options(step_files, registry)
    # No adapter stays, so the dry-run still says `no adapter, will skip`; an option file the adapter lacks goes.
    suites = [
        suite
        for suite in select(step_files, profile, args.integration, args.action)
        if registry.adapter(suite.action, suite.integration) is None or applies(suite, registry)
    ]
    if not any(registry.adapter(suite.action, suite.integration) for suite in suites):
        raise SystemExit("No rig test matches the profile's integrations (and --integration/--action, if given).")
    return suites


def choose_profile(
    name: str | None,
    env: dict,
    directory: Path = PROFILES_DIR,
    example: Path = EXAMPLE,
) -> Path:
    """The profile --profile names, else the only one. Refuses while .env.rig still has the RIG_PAUSE that profiles
    replaced, so nothing runs against half a migration."""
    if env.get("RIG_PAUSE"):
        raise SystemExit("RIG_PAUSE has moved from .env.rig into the profile, as `pause:` (see profiles)")
    paths = {path.stem: path for path in profile_paths(directory)}
    if name is not None:
        if name not in paths:
            raise SystemExit(f"No profile {name!r} in {directory}; there are: {', '.join(paths) or 'none'}")
        return paths[name]
    if not paths:
        raise SystemExit(f"No profile in {directory}; copy {example} to <name>.yaml there and fill it in")
    if len(paths) > 1:
        raise SystemExit(f"Choose a profile with --profile: {', '.join(paths)}")
    return next(iter(paths.values()))


def tested_commit(status_cmd: str, deploy: Deploy, deploying: bool) -> str | None:
    """The commit whose integration tree HA loaded for the tests, or None if none can be named: under --no-deploy HA
    runs whatever it last loaded, and a deploy may install a different tip if origin moved past the plan (trusted when
    HA can't say what it loaded). Read before the tests, since HEAD moves when the session commits mid-run."""
    if not deploying:
        return None if deploy.needed else deploy.commit
    loaded = deployed_trees(status_cmd)[1]
    return deploy.commit if loaded in (None, deploy.local_tree) else None


def deploy_and_wait(env: dict, branch: str | None) -> None:
    """Deploy `branch` (None: don't), then wait for HA; a deploy holds off a signal until HA is back."""
    with finish_first() if branch is not None else contextlib.nullcontext():
        if branch is not None:
            deploy_env = {**os.environ, "RIG_SYNC_CMD": env["RIG_SYNC_CMD"], "RIG_RESTART_CMD": env["RIG_RESTART_CMD"]}
            try:
                # Its own session, so a Ctrl-C at the terminal reaches only the runner, which holds it.
                subprocess.run(
                    ["sh", str(RIG_HOME / "scripts" / "deploy.sh"), branch],
                    check=True,
                    env=deploy_env,
                    start_new_session=True,
                )
            except subprocess.CalledProcessError as err:
                raise SystemExit(
                    f"Deploy failed (exit {err.returncode}); HA may be mid-restart, so check it before the next run."
                ) from None
        timeout = READY_TIMEOUT_S if branch is not None else 15
        wait_until_ready(env["HA_URL"].rstrip("/"), env["HA_TOKEN"], env.get("HA_ADMIN_TOKEN"), timeout)


@contextlib.contextmanager
def end_on_signals() -> Iterator[None]:
    """Until the tests take over (cancel_on_signals), have Ctrl-C, SIGTERM and SIGHUP end the run on one line rather
    than a traceback. What a recovery had yet to put back stays in the journal."""

    def abort(_signum: int, _frame: object) -> None:
        raise Aborted("Aborted.")

    with handling_signals(abort):
        yield


@contextlib.contextmanager
def finish_first() -> Iterator[None]:
    """Hold off a signal until the block ends, then stop: the deploy and HA's restart finish under the lock, so the next
    run doesn't sync into a booting HA. A second signal stops at once."""
    held: list[int] = []

    def hold(signum: int, _frame: object) -> None:
        if held:
            raise Aborted("Aborted mid-deploy; HA may be mid-restart, so check it before the next run.")
        held.append(signum)
        print("\nStopping once the deploy and HA's restart finish; again to stop now.")

    with handling_signals(hold):
        yield
    if held:
        raise Aborted("Aborted after the deploy.")


@contextlib.contextmanager
def handling_signals(handler: Callable[[int, object], None]) -> Iterator[None]:
    previous = {signum: signal.signal(signum, handler) for signum in ABORT_SIGNALS}
    try:
        yield
    finally:
        for signum, before in previous.items():
            signal.signal(signum, before)


def common_dir() -> Path:
    """The git common dir, shared by every worktree, where the locks and the journals live."""
    return Path(git("rev-parse", "--path-format=absolute", "--git-common-dir"))


def run_tests(rig_run: Coroutine[Any, Any, int], aborted: Callable[[], int] | None = None) -> int:
    """Run run_rig, ending on one line rather than a traceback when a signal stops it (cancel_on_signals); `aborted`
    then says what is left and gives the exit code, for a run that saved its progress."""
    try:
        return asyncio.run(rig_run)
    except asyncio.CancelledError:
        print("Aborted.")
        if aborted is not None:
            return aborted()
    except KeyboardInterrupt:  # a second Ctrl-C, mid-cleanup
        print("Aborted before putting everything back; the next run puts back the rest.")
    return 1


def left_restricted(journal: Journal, why: str) -> str:
    return f"left {journal.path} ({why}): sensor off or {APPROVAL}=1 restores it"


def replay(env: dict, held: Held | None, directory: Path, authority: Authority, warn: bool = False) -> set[str]:
    """Put back what aborted runs left changed, each whose claim the run holds (needs) and that `authority` lets the
    rig put back now; say how to put back the rest. A run whose claim covers an orphan it may not put back stops
    here, before its gate, rather than test on top of what that orphan left. A dry run holds nothing, so it only says
    what is there. Returns the roaming bulbs whose Hue home the home record or a journal from before it still holds,
    which a run puts back. With `warn` (a probe), an orphan it may not put back is only printed, and it goes on."""
    homes: set[str] = set()
    stale: list[str] = []
    for orphan in orphans(directory):
        try:
            needed = needs(orphan)
            covered = held is None or held.claim.covers(needed)
            if covered and (why := authority.withholds(orphan)):
                print(left_restricted(orphan, why))
                if held is not None and not warn:
                    stale.append(str(orphan.path))
            elif held is not None and covered:
                asyncio.run(recover_rig(env, orphan))
            elif held is None:
                print(f"An aborted run left changes in {orphan.path}; a run holding {needed} puts them back first.")
            else:
                print(
                    f"An aborted run left changes in {orphan.path} that need {needed}, more than this run holds; "
                    "a run holding that, or --recover, puts them back."
                )
            # What is left in a journal from before the home record: a recovery that brought the bulbs home cleared it.
            homes |= set((orphan.get("roam") or {}).get("home", {}))
        finally:
            orphan.release()
    if stale:
        raise SystemExit(
            f"This run's locks cover what {', '.join(stale)} left changed, which it may not put back now, so it would "
            "test on top of it. Not started."
        )
    if legacy := Journal(directory / LEGACY_NAME):
        print(f"A run from before per-run journals left changes in {legacy.path}; --recover puts them back.")
        homes |= set((legacy.get("roam") or {}).get("home", {}))
    return homes | {entry["entity_id"] for entry in HomeRecord(directory).all().values() if entry.get("hue")}


def needs(journal: Journal) -> Claim:
    """What a run must hold to put back an orphan: the integrations its run held, and HA exclusively when it left
    DeviceSide's options changed or its entry disabled, as setting them or enabling it reloads the entry."""
    held = Claim.of(journal.get("claim"))
    reloads = journal.get("options") is not None or journal.get("entry") is not None
    return Claim("exclusive" if reloads else "shared", held.integrations)


def check_tokens(env: dict, suites: list[Suite], registry: AdapterRegistry, roaming: bool = False) -> None:
    """Stop before the gate when .env.rig lacks a token the run needs: ADMIN_INTEGRATIONS, any adapter with options
    (they are set through DeviceSide's options flow) and any file with entry steps need HA_ADMIN_TOKEN; a Hue file
    with a bridge step needs the bridge's host and key; roaming, or making the lab's groups, needs both, for the
    registries, MQTT and the bridge."""
    if not (env.get("HA_URL") and env.get("HA_TOKEN")):
        raise SystemExit("HA_URL and HA_TOKEN must be set in .env.rig (see ha_rig/env.example)")
    bridge = env.get("HUE_BRIDGE_HOST") and env.get("HUE_APP_KEY")
    if any(suite.integration == "hue" and suite.file.uses_bridge for suite in suites) and not bridge:
        raise SystemExit("HUE_BRIDGE_HOST and HUE_APP_KEY must be set in .env.rig for the bridge step")
    if roaming and not (bridge and env.get("HA_ADMIN_TOKEN")):
        raise SystemExit("Roaming needs HA_ADMIN_TOKEN, HUE_BRIDGE_HOST and HUE_APP_KEY in .env.rig")
    if env.get("HA_ADMIN_TOKEN"):
        return
    with_options = {
        suite.integration
        for suite in suites
        if (adapter := registry.adapter(suite.action, suite.integration)) is not None
        and (adapter.scope.options or suite.file.moves_entry)
    }
    if admin_only := sorted(({suite.integration for suite in suites} & ADMIN_INTEGRATIONS) | with_options):
        raise SystemExit(
            f"{', '.join(admin_only)} needs HA_ADMIN_TOKEN in .env.rig (an admin user's token), "
            "or leave it out with --integration"
        )


def print_plan(
    profile: Profile,
    suites: list[Suite],
    registry: AdapterRegistry,
    keyword: str | None,
    deploy: Deploy,
    deploying: bool,
    roaming: bool = False,
    grouping: bool = False,
    lanes: bool = False,
    done: Done | None = None,
) -> None:
    print_profile(profile)
    print_phases(profile, suites, registry, keyword, roaming, grouping, lanes, done)
    print_deploy(deploy, deploying)
    if not deploying and deploy.needed and suites:
        print("Deploy: skipped (--no-deploy); tests run the deployed tree, not this checkout, and write no record")
    elif not deploying:
        print("Deploy: not needed, no restart")
    seconds = estimate(suites, registry, deploying, roaming, profile if lanes else None, done, resumed=profile)
    print(f"Estimated duration: {seconds} s. These are in-use household devices; they will visibly change state.")


def print_deploy(deploy: Deploy, deploying: bool) -> None:
    """The integration's trees, local, synced and loaded, and, when `deploying`, the deploy."""
    print(
        f"Integration: local {deploy.local_tree[:12]}, synced {(deploy.synced_tree or 'unknown')[:12]}, "
        f"loaded {(deploy.loaded_tree or 'unknown')[:12]}"
    )
    if deploying:
        print(f"Deploy: sync {deploy.branch}, then restart Home Assistant")


def print_profile(profile: Profile) -> None:
    """The profile's entities by integration, its roaming bulbs once each, and its pause list."""
    print(f"Profile {profile.name}:")
    roaming_ids = {bulb.entity_id for bulb in profile.roam}  # the groups print below, and the bulbs once
    for integration, entities in profile.integrations.items():
        for entity in entities:
            if entity.entity_id not in roaming_ids:
                print(f"  {integration:8} {entity.entity_id:40} {', '.join(sorted(entity.roles))}")
    for bulb in profile.roam:
        print(f"  {'roams':8} {bulb.entity_id:40} {', '.join(sorted(bulb.roles))}")
    for group in profile.roam_groups:
        print(f"  {'ds-rig':8} {group.entity_id:40} {', '.join(sorted(group.roles))}")
        if group.scene:
            print(f"  {'ds-rig':8} {group.scene:40} scene")
    for key, entity_ids in profile.pause.items():
        if entity_ids:
            whose = "every run" if key == PAUSE_ALL else f"a run holding {key}"
            print(f"Paused for {whose}: {', '.join(entity_ids)}")


def print_phases(
    profile: Profile,
    suites: list[Suite],
    registry: AdapterRegistry,
    keyword: str | None,
    roaming: bool,
    grouping: bool = False,
    lanes: bool = False,
    done: Done | None = None,
) -> None:
    """The moves, if the run roams, and the groups it makes, then the lanes' cases, then each phase's adapters and
    the cases left for it; resuming, only the cases `done` has no result for."""
    if not roaming and profile.roam:
        where = " or ".join(NAMES[network] for network in away(profile)) or "another network"
        print(f"Roaming: none, as no {where} suite runs; the bulbs stay on {NAMES[profile.roam_home]}.")
    if roaming or grouping:
        print(
            "\n".join(
                roam_plan_lines(
                    profile.roam,
                    profile.roam_groups if grouping else (),
                    roaming,
                    [network for network, _ in phases(suites, profile, roaming) if network],
                )
            )
        )
    if lanes and suites:
        print("In parallel, one lane per integration:")
        for integration, lane in lane_order(suites).items():
            lines = plan_lines(lane, profile, registry, keyword, unfinished(done, head(profile)))
            print(f"  {integration}:" + ("" if lines else " none"))
            for line in lines:
                print(f"    {line}")
    only = unfinished(done, tail(profile) if lanes else None)
    for network, phase_suites in phases(suites, profile, roaming):
        print(f"Adapters on {NAMES[network]}:" if network else "Adapters:")
        for line in plan_lines(phase_suites, profile, registry, keyword, only) or ["none"]:
            print(f"  {line}")


async def run_rig(
    env: dict,
    suites: list[Suite],
    profile: Profile,
    registry: AdapterRegistry,
    keyword: str | None,
    unattended: bool,
    commit: str | None,
    journal: Journal | None = None,
    roaming: bool = False,
    grouping: bool = False,
    lanes: bool = False,
    claim: Claim | None = None,
    debug_logs: bool = False,
    logs_dir: Path | None = None,
    progress: Progress | None = None,
    selected: list[Suite] | None = None,
    budget: Budget | None = None,
    pending: set[tuple[str, str]] | None = None,
    reobserve: bool = False,
) -> int:
    """Run the step files, in lanes first when `lanes` (run_phases), then phase by phase, moving the roaming bulbs
    before each and home after the last, and making the ds-rig-* groups in each phase when `grouping`, then stage a
    test record for each adapter whose every test passed, citing `commit`; with no commit, none: a record nobody can
    trace to a commit proves nothing. Roaming and groups need the admin connection and the bridge (check_tokens).

    The records are staged next to the report (ha_rig/staging.py) and promoted into the checkout only when the run
    ends cleanly (settle_records); any other end leaves the checkout untouched and prints the command that promotes
    them. The `pending` suites, finished by an earlier run whose records were never promoted, are staged too.

    Z2M's log (for a run that touches it) and HA's go next to the report as `<report>.z2m.log` and `.ha.log`: deleted
    after a clean run, kept and printed after any other, or with `debug_logs`, which also runs HA's DeviceSide and the
    run's integrations, and Z2M, at debug until the run ends (RunLogs, DebugLevels). A DeviceSide warning or error in
    HA's log that no step file's `expected_log` matches is printed and fails the run; the records already written
    stand.

    With `progress`, each case is saved as it finishes and the report joins the saved results of every `selected`
    suite to this session's, so a suite finished across sessions writes its record, which takes it out of the ledger;
    a suite that failed a case leaves it when the run ends. A run the `budget` stopped says what is left and returns
    RESUMABLE, unless the logs flag it."""
    lab = roaming or grouping
    selected = selected or suites
    if lab and not (env.get("HA_ADMIN_TOKEN") and env.get("HUE_BRIDGE_HOST") and env.get("HUE_APP_KEY")):
        raise SystemExit("Roaming needs HA_ADMIN_TOKEN, HUE_BRIDGE_HOST and HUE_APP_KEY in .env.rig")
    cancel_on_signals()
    ha, admin = await connect(env)
    bridge = None
    if env.get("HUE_BRIDGE_HOST") and env.get("HUE_APP_KEY"):
        bridge = HueBridge(env["HUE_BRIDGE_HOST"], env["HUE_APP_KEY"])
    groups = profile.roam_groups if grouping else ()
    roamer = Roamer(admin, bridge, map(Bulb.of, profile.roam), journal, groups=groups) if lab else None
    # The pauses are the run's, by its journal's name: a run with no journal (a test's) pauses nothing.
    pauses = Baseline.of(admin or ha, journal, "pause") if journal is not None else None
    pausing = profile.paused_by(claim.integrations if claim else None)
    stopped: Roamer | None = None  # the roamer, once a RoamError stopped the run
    home = True  # whether the roaming bulbs ended at home, with no rig-made group left
    if journal is not None:  # the claim says what a run must hold to put back what this one leaves
        journal.update(profile=profile.name, **({"claim": claim.as_json()} if claim else {}))
    results: list[SuiteResult] = []  # each phase's, as it finishes: a failed move keeps the phases before it
    started = datetime.datetime.now()
    stamp = started.strftime("%Y-%m-%d %H:%M:%S")
    expected = dict.fromkeys(pattern for suite in suites for pattern in suite.file.expected_log)
    logs = RunLogs(logs_dir or REPORT_DIR, f"rig run {stamp}", started, keep=debug_logs, expected=expected)
    # The wire trace needs the admin connection, which alone may subscribe to it, and lends observed passes.
    levels = DebugLevels(Baseline.of(admin or ha, journal, "debug")) if debug_logs or admin else None
    staging = staging_for(stamp, profile, progress)
    code = 1  # only a run that returns 0 is clean; any other end keeps the logs and promotes no record
    unrun = 0  # the cases the run planned with no result, once it ends 0
    try:
        try:
            await begin_logging(logs, levels, admin, suites, roaming, debug_logs)
            result = await run_phases(
                ha,
                admin,
                bridge,
                roamer,
                roaming,
                suites,
                profile,
                registry,
                keyword,
                unattended,
                (lambda: pauses.pause(pausing)) if pauses and pausing else None,
                journal,
                results,
                lanes,
                reloads=claim is None or claim.ha == "exclusive",  # holding HA shared, it may not reload the entry
                progress=progress,
                budget=budget,
                reobserve=reobserve,
            )
            result, touched = joined(result, progress, selected)
            if suites:
                await report(result, registry, commit, profile, staging, stamp, touched | (pending or set()), progress)
        except RoamError as err:
            # A move that failed isn't retried here; the journal keeps where the bulbs are, for --recover. The groups
            # made so far still go, and after a group that couldn't be made, the bulbs still go home. The phases that
            # ran still get their report and records.
            if not isinstance(err, GroupError):
                stopped, roamer = roamer, None
            await report_ran(results, registry, commit, profile, staging, stamp)
            if progress is not None:
                progress.drop_failed()  # the run ends here, so its failed suites rerun whole
            raise SystemExit(f"Roaming stopped: {err}") from None
        finally:
            home, signalled = await wind_down(roamer, stopped, logs, levels, pauses, journal, (bridge, ha, admin))
            end_abort(home, signalled, sys.exception())
        ending = end_code(result, home, budget, progress, suites, profile, registry, keyword)
        if ending == 0:
            unrun = unfinished_cases(result, progress, suites, profile, registry, keyword)
        code = ending  # only now: anything raised before leaves the run unclean
    finally:
        flagged = True
        try:
            flagged = logs.settle(code == 0)
        finally:
            settle_records(staging, 1 if flagged else code, unrun, progress)
    return 1 if flagged else code


def staging_for(stamp: str, profile: Profile, progress: Progress | None) -> Staging:
    """Where the run titled `rig run <stamp>` stages its records: next to its report, as `<report>.<pid>.records/`,
    so runs started the same second never share one."""
    return Staging(
        report_path(REPORT_DIR, f"rig run {stamp}").with_suffix(f".{os.getpid()}.records"),
        REPO,
        TEST_RECORDS_DIR,
        progress.path.parents[1] if progress is not None else None,
        profile.name,
    )


def unfinished_cases(
    result: RunResult,
    progress: Progress | None,
    suites: list[Suite],
    profile: Profile,
    registry: AdapterRegistry,
    keyword: str | None,
) -> int:
    """How many cases the run planned (`keyword`'s, of `suites`) have no result: with the ledger, those it holds none
    for; without (--no-deploy), the unreachable skips, which never count as one."""
    if progress is None:
        return sum(case.unreachable for part in result.suites for case in part.cases)
    done = progress.done()
    return sum(len(left(suite, profile, registry, done, keyword)) for suite in suites)


def settle_records(staging: Staging, code: int, unrun: int, progress: Progress | None) -> None:
    """At the run's end, its staged records: promoted into the checkout when the run ended cleanly (`code` 0) with a
    result for every case it planned (`unrun` 0); else left staged, with the one command that promotes them. Never
    raises: it runs as the run ends, whatever ended it."""
    if not staging.records:
        return
    if code == 0 and not unrun:
        try:
            staging.promote(progress)
            return
        except Exception as err:  # noqa: BLE001 - say so, and leave the command that tries again
            why = f"promoting them failed: {err}"
    else:
        why = f"{unrun} planned cases have no result" if code == 0 else f"the run didn't end cleanly (exit {code})"
    count = len(staging.records)
    print(
        f"\nNo test records written to the checkout: {why}. {count} staged in {staging.directory}; promote them with:"
    )
    print(f"  {staging.command()}")


async def wind_down(
    roamer: Roamer | None,
    stopped: Roamer | None,
    logs: RunLogs,
    levels: DebugLevels | None,
    pauses: Baseline | None,
    journal: Journal | None,
    clients: tuple[HueBridge | None, HAClient, HAClient | None],
) -> tuple[bool, bool]:
    """After a run: the bulbs home (leave_lab), the logs ended, the pauses released, the journal's profile dropped, the
    clients closed. Each step runs whatever the one before did; the first error is raised once all have. Whether the
    bulbs are home, and whether a signal came on the way."""
    errors: list[BaseException] = []
    home, signalled = True, False

    async def step(call: Awaitable[Any]) -> None:
        try:
            await call
        except BaseException as err:  # noqa: BLE001 - the rest still runs; the first error is raised after
            errors.append(err)

    try:
        home, signalled = await sheltered(leave_lab(roamer, stopped))
    except BaseException as err:  # noqa: BLE001 - as step
        errors.append(err)
    admin = clients[2]
    await step(end_logging(logs, levels, admin))
    if pauses is not None:
        await step(pauses.release())
    try:
        drop_profile(journal)
    except Exception as err:  # noqa: BLE001 - as step
        errors.append(err)
    for client in clients:
        if client is not None:
            await step(client.close())
    if errors:
        raise errors[0]
    return home, signalled


def joined(
    result: RunResult, progress: Progress | None, selected: list[Suite]
) -> tuple[RunResult, set[tuple[str, str]]]:
    """The run's result to report, and the suites this session ran a case of: with progress, every case saved for the
    `selected` suites, this session's among them."""
    touched = {(part.suite.integration, part.suite.name) for part in result.suites}
    return (result if progress is None else RunResult(progress.parts(selected))), touched


def end_code(
    result: RunResult,
    home: bool,
    budget: Budget | None,
    progress: Progress | None,
    suites: list[Suite],
    profile: Profile,
    registry: AdapterRegistry,
    keyword: str | None = None,
) -> int:
    """The run's exit code before its logs are read: 1 with the bulbs not home; RESUMABLE when the budget left cases,
    saying so; else 1 on a failure, saved or new, and 0 otherwise. The ledger stays, but for the suites that failed a
    case, which rerun whole."""
    if not home:
        return 1
    if budget is not None and budget.stopped:
        return say_stopped(progress, suites, profile, registry, keyword, "budget spent")
    if progress is not None:
        progress.drop_failed()
    return 1 if result.failed else 0


async def begin_logging(
    logs: RunLogs,
    levels: DebugLevels | None,
    admin: HAClient | None,
    suites: list[Suite],
    roaming: bool,
    debug_logs: bool = False,
) -> None:
    """Start Z2M's capture, when the run touches Z2M, then raise the debug levels, when asked: the raise's own lines
    are captured. With the admin connection, turn DeviceSide's wire trace on and subscribe to it, for every run: it
    lends observed passes (engine.run). Without, observed cases run as before, and a run unattended reports them not
    observed."""
    z2m = touches_z2m(suites, roaming)
    if z2m:
        await logs.start_z2m(admin)
    if admin is not None and levels is not None:
        await admin.subscribe_wire()
        await levels.raise_wire()
    elif any(test.observe for suite in suites for test in suite.file.tests):
        print("No HA_ADMIN_TOKEN, so no wire trace: no observed case is lent a pass its calls earned before.")
    if debug_logs and levels is not None:
        await levels.raise_levels(ha_domains({suite.integration for suite in suites}, roaming), z2m)


async def end_logging(logs: RunLogs, levels: DebugLevels | None, admin: HAClient | None) -> None:
    """Put the levels back, then stop Z2M's capture and save HA's log."""
    if levels is not None:
        await levels.restore()
    await logs.stop_z2m()
    await logs.save_ha(admin)


async def run_phases(
    ha: HAClient,
    admin: HAClient | None,
    bridge: HueBridge | None,
    roamer: Roamer | None,
    roaming: bool,
    suites: list[Suite],
    profile: Profile,
    registry: AdapterRegistry,
    keyword: str | None,
    unattended: bool,
    pause: Callable[[], Awaitable[None]] | None,
    journal: Journal | None,
    results: list[SuiteResult] | None = None,
    lanes: bool = False,
    reloads: bool = True,
    progress: Progress | None = None,
    budget: Budget | None = None,
    reobserve: bool = False,
) -> RunResult:
    """Each phase: move the bulbs there and make its groups, wait for its targets, then run its cases, automated then
    observed. `pause` pauses the automations, once, after the first phase's targets are all there. A run with no suites
    is --roam-check, which makes and removes the groups on every visit as a probe. Each phase's results go into
    `results` as it finishes, so a caller keeps them when a later move raises.

    With `lanes`, the first phase starts with every suite's in_head cases, one lane per integration side by side,
    with the bulbs at home; the phases then run only the rest (see merge).

    With `progress`, the cases it has saved are left out and each new one is saved as it finishes. Once the `budget`
    is spent no phase starts, so the bulbs move no further than home. A move starts only with MOVE_LEAD_S of it left,
    and buys its phase one case, whatever the budget."""
    results = [] if results is None else results
    lanes = lanes and bool(suites)
    ask = None if unattended else input
    # One busy map for every run: the phases go on from the lanes, on some of the same lights.
    common = {
        "admin": admin,
        "keyword": keyword,
        "ask": ask,
        "bridge": bridge,
        "journal": journal,
        "busy": {},
        "reloads": reloads,
        "budget": budget,
        "observations": load_observations(TEST_RECORDS_DIR, {suite.integration for suite in suites}),
        "reobserve": reobserve,
        **saving(progress),
    }
    only = tail(profile) if lanes else None
    for number, (network, phase_suites) in enumerate(phases(suites, profile, roaming)):
        moving = phase_moves(roamer, network)
        if budget is not None and phase_stopped(budget, moving):
            break
        await visit(roamer, network, bool(phase_suites or not suites))
        if moving and budget is not None:
            budget.grant()
        done = common.get("done")
        if number == 0 and lanes:
            await wait_for_targets(ha, suites, profile, registry, keyword, only=unfinished(done, head(profile)))
        if phase_suites:
            await wait_for_targets(ha, phase_suites, profile, registry, keyword, only=unfinished(done, only))
        if number == 0 and pause is not None:
            await pause()
        if number == 0 and lanes:
            results += (await run(suites, profile, registry, ha, only=head(profile), lanes=True, **common)).suites
        if phase_suites:
            results += (await run(phase_suites, profile, registry, ha, only=only, **common)).suites
    if roamer is not None:
        await roamer.go(roamer.home_network)
    return RunResult(results)


def phase_moves(roamer: Roamer | None, network: str | None) -> bool:
    """Whether the phase for `network` (None: home) moves the bulbs."""
    return roamer is not None and roamer.moves(network or roamer.home_network)


def phase_stopped(budget: Budget, moving: bool) -> bool:
    """Whether the budget stops the phase: a move needs MOVE_LEAD_S left, any other phase only some budget left."""
    if moving and budget.left < MOVE_LEAD_S:
        budget.stopped = True
    return budget.stopped if moving else budget.stops()


async def visit(roamer: Roamer | None, network: str | None, grouping: bool) -> None:
    """Move the bulbs to the phase's network (None: home), removing the last phase's groups first, and make this
    one's when `grouping`."""
    if roamer is None:
        return
    await roamer.go(network or roamer.home_network)
    if grouping:
        await roamer.make_groups(network or roamer.home_network)


def saving(progress: Progress | None) -> dict:
    """run's arguments that leave out the cases `progress` saved and save each new one."""
    if progress is None:
        return {}
    return {"done": progress.done(), "on_result": progress.record, "on_facts": progress.facts}


async def report_ran(
    results: list[SuiteResult],
    registry: AdapterRegistry,
    commit: str | None,
    profile: Profile,
    staging: Staging,
    stamp: str | None = None,
) -> None:
    """After a failed move, report the phases that ran before it, staging their records. A report that fails is
    printed, never raised: the move's error, with its recovery steps, must still show."""
    if not results:
        return
    try:
        await report(RunResult(results), registry, commit, profile, staging, stamp)
    except Exception as err:  # noqa: BLE001
        print(f"The phases that ran couldn't be reported: {err}")


async def report(
    result: RunResult,
    registry: AdapterRegistry,
    commit: str | None,
    profile: Profile,
    staging: Staging,
    stamp: str | None = None,
    touched: set[tuple[str, str]] | None = None,
    progress: Progress | None = None,
):
    """The run's report, and a test record per fully passed suite when a commit names the tree HA ran, staged in
    `staging` with its suite's ledger entry (`progress`), which leaves the ledger only when the record is promoted. A
    suite whose cases ran in parts (lanes, then phases, or runs) is joined first; one missing a part writes no record,
    and says how far it got. With `touched`, only those suites write theirs: those with a case in this session, and
    those an earlier one finished whose records were never promoted (pending)."""
    result = merge(result.suites, profile, registry)
    stamp = stamp or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = datetime.date.today().isoformat()
    for suite_result in result.suites:
        if partial := suite_result.partial:
            print(f"{suite_result.suite.integration} / {suite_result.suite.name}: {partial}")
    print(f"\nReport: {write_report(result, REPORT_DIR, f'rig run {stamp}')}")
    # What a person saw stands whatever the code (ha_rig/observed.py), so it is kept even with no commit.
    for path in write_observed(result, registry, TEST_RECORDS_DIR, today, into=staging.directory):
        staging.add(path, None)
    if commit is None:
        print("No test records: no commit names the tree HA ran (--no-deploy, or origin moved before the deploy).")
        return
    source = f"rig run (`scripts/rig.py`), commit `{git('rev-parse', '--short', commit)}`"
    if touched is not None:
        result = RunResult([part for part in result.suites if (part.suite.integration, part.suite.name) in touched])
    written, kept = await write_records(
        result, registry, TEST_RECORDS_DIR, today, source, profile.name, into=staging.directory
    )
    by_path = {
        staging.directory / part.suite.integration / f"{part.suite.name}.md": part.suite for part in result.suites
    }
    for path in written:
        suite = by_path.get(path)
        staging.add(path, progress.key(suite) if progress is not None and suite is not None else None)
    for path, narrowed in kept:
        print(
            f"Kept {display(path, REPO)}: profile {profile.name} skips {', '.join(narrowed)}, which it passed; "
            "run a profile with those roles, or delete the record to narrow it."
        )


def report_saved(
    progress: Progress | None,
    suites: list[Suite],
    registry: AdapterRegistry,
    profile: Profile,
    stamp: str,
) -> None:
    """The report of every case `progress` has saved, after a signal: no records, as the run is unfinished. Nothing
    without progress."""
    if progress is None:
        return
    try:
        result = merge(progress.parts(suites), profile, registry)
        print(f"\nReport: {write_report(result, REPORT_DIR, f'rig run {stamp}')}")
    except Exception as err:  # noqa: BLE001 - the report is a courtesy; the devices go back regardless
        print(f"The cases that ran couldn't be reported: {err}")


async def sheltered[T](coro: Coroutine[Any, Any, T]) -> tuple[T, bool]:
    """Run `coro` to its end in a task of its own, and say whether a signal came meanwhile. A signal cancels the run's
    task (cancel_on_signals), never this one, so the way home neither stops at a signal that comes during it nor at
    an await that heeds the cancel the run's task still carries from one before it (issue #11): either would leave a
    bulb a move released off its home network. A second signal still stops it: the first restored the default
    handlers."""
    task = asyncio.current_task()
    inner = asyncio.create_task(coro)
    signalled = False
    while not inner.done():
        try:
            await asyncio.wait({inner})
        except asyncio.CancelledError:
            signalled = True
            task.uncancel()
    return inner.result(), signalled


def end_abort(home: bool, signalled: bool, propagating: BaseException | None) -> None:
    """Once all is put back: a run a signal stopped, before the way home (a CancelledError `propagating`) or during it
    (`signalled`), with the bulbs not home exits saying so, not "Aborted." as though all is well; the journal keeps
    where they are. A signal that came on the way home stops the run now, unless something else already is."""
    if not home and (signalled or isinstance(propagating, asyncio.CancelledError)):
        raise SystemExit(
            "Aborted with the roaming bulbs not home: the journal keeps where they are, and "
            "scripts/rig.py --recover tries again."
        )
    if signalled and propagating is None:
        raise asyncio.CancelledError


async def leave_lab(roamer: Roamer | None, stopped: Roamer | None) -> bool:
    """After the run: the bulbs home and the groups removed (go_home), or, after a RoamError, only the groups removed:
    a move that failed isn't retried here. Whether the bulbs are home with no group left."""
    if roamer is not None:
        return await go_home(roamer)
    if stopped is not None:
        await stopped.remove_groups()
    return True


async def go_home(roamer: Roamer) -> bool:
    """Remove the rig-made groups and move the roaming bulbs home after a run that stopped part way (a no-op after
    one that finished); whether that is all done, their homes put back included. A failure is printed: the journal
    keeps where they are, and --recover tries again. It runs sheltered from signals, so a cancel here is no signal's
    but a stray one, or asyncio.run's after a second signal: a move that didn't finish, said as such."""
    try:
        await roamer.go(roamer.home_network)
    except asyncio.CancelledError:
        print("Could not move the roaming bulbs home: the move was cancelled. scripts/rig.py --recover tries again.")
        return False
    except Exception as err:  # noqa: BLE001 - as resume: say so, and go on putting the rest back
        print(f"Could not move the roaming bulbs home: {err}. scripts/rig.py --recover tries again.")
        return False
    return roamer.done


async def wait_for_targets(
    ha: HAClient,
    suites: list[Suite],
    profile: Profile,
    registry: AdapterRegistry,
    keyword: str | None,
    timeout: float = TARGETS_TIMEOUT_S,
    only: Callable[[Case], bool] | None = None,
) -> None:
    """Until every entity a case `only` accepts will move, a group's listed members included, is available; exits the
    runner, naming the rest, after `timeout`, or at once for one HA doesn't have."""
    entities = targets(suites, profile, registry, keyword, only)
    states = dict(zip(entities, await asyncio.gather(*map(ha.get_state, entities)), strict=True))
    if missing := [entity for entity, state in states.items() if state is None]:
        raise SystemExit(f"Not in HA: {', '.join(missing)}. Check profile {profile.name}; nothing was changed.")
    waiting = [entity for entity, state in states.items() if not available(state)]
    if not waiting:
        return
    print(f"Waiting up to {timeout:.0f} s for {', '.join(waiting)} to become available.")
    results = await asyncio.gather(
        *(ha.wait_for(entity, available, timeout) for entity in waiting), return_exceptions=True
    )
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, TimeoutError):
            raise result
    if still := [entity for entity, result in zip(waiting, results, strict=True) if isinstance(result, TimeoutError)]:
        raise SystemExit(
            f"Still unavailable after {timeout:.0f} s: {', '.join(still)}. Check it is powered; nothing was changed."
        )


def available(state: dict | None) -> bool:
    return state is not None and state["state"] != "unavailable"


def targets(
    suites: list[Suite],
    profile: Profile,
    registry: AdapterRegistry,
    keyword: str | None,
    only: Callable[[Case], bool] | None = None,
    done: Done | None = None,
) -> list[str]:
    """Every entity a case `only` accepts will move, a group's listed members included; none of a case `done` has a
    result for, which the run leaves out."""
    only = unfinished(done, only)
    return sorted(
        {
            entity
            for suite in suites
            if registry.adapter(suite.action, suite.integration)
            for case in cases(suite, profile, keyword, adapters_for(suite, registry))
            if not case.skip and (only is None or only(case))
            for entity in (*case.entities.values(), *case.members)
        }
    )


def platforms(
    suites: list[Suite], profile: Profile, registry: AdapterRegistry, keyword: str | None, done: Done | None = None
) -> dict[str, tuple[str, str]]:
    """Each entity the cases will move (targets), with its integration and the platform HA must register it on: the
    integration's HA domain (runlogs.HA_DOMAINS), or switch_as_x for a light_wrapped; a group's members count as the
    group's integration."""
    only = unfinished(done)
    found: dict[str, tuple[str, str]] = {}
    for suite in suites:
        if not registry.adapter(suite.action, suite.integration):
            continue
        domain = HA_DOMAINS.get(suite.integration, suite.integration)
        for case in cases(suite, profile, keyword, adapters_for(suite, registry)):
            if case.skip or (only is not None and not only(case)):
                continue
            for role, entity in case.entities.items():
                found.setdefault(entity, (suite.integration, "switch_as_x" if role == "light_wrapped" else domain))
            for entity in case.members:
                found.setdefault(entity, (suite.integration, domain))
    return found


async def check_platforms(
    ha: HAClient, expected: dict[str, tuple[str, str]], entities: list[str], profile: Profile
) -> list[str]:
    """The problems among `entities` (already in HA) from the entity registry: one missing from it (YAML-defined, which
    the run can't trust) or registered on another platform, as a bulb paired to another network is."""
    registered = {entry["entity_id"]: entry["platform"] for entry in await ha.entity_registry()}
    problems = []
    for entity in entities:
        integration, platform = expected[entity]
        if entity not in registered:
            problems.append(f"{entity}: not in the entity registry (YAML-defined?); the run can't trust it")
        elif registered[entity] != platform:
            now = registered[entity]
            problems.append(f"{entity}: a {now} entity, not {integration}; pair it back or fix profile {profile.name}")
    return problems


async def check_fixed(
    ha: HAClient,
    admin: HAClient | None,
    fixed: list[str],
    suites: list[Suite],
    profile: Profile,
    registry: AdapterRegistry,
    keyword: str | None,
    done: Done | None = None,
    ignore_unreachables: bool = False,
) -> tuple[list[str], set[str]]:
    """The problems among the fixed entities, and those offline: one not in HA, or of those in HA, one on the wrong
    platform, is a problem; one unavailable is too, unless `ignore_unreachables`, which counts it offline instead."""
    problems, present, offline = [], [], set()
    for entity, state in zip(fixed, await asyncio.gather(*map(ha.get_state, fixed)), strict=True):
        if state is None:
            problems.append(f"{entity}: not in HA; check profile {profile.name}")
            continue
        present.append(entity)
        if available(state):
            continue
        if ignore_unreachables:
            offline.add(entity)
        else:
            problems.append(f"{entity}: unavailable; check it is powered")
    if present:
        try:
            expected = platforms(suites, profile, registry, keyword, done)
            problems += await check_platforms(admin or ha, expected, present, profile)
        except HAError as err:  # a token without admin rights can't list the registry
            print(f"The platforms weren't checked: HA refused the entity registry ({err}).")
    return problems, offline


async def check_devices(
    env: dict,
    suites: list[Suite],
    profile: Profile,
    registry: AdapterRegistry,
    keyword: str | None,
    roaming: bool,
    homes: set[str] = frozenset(),
    done: Done | None = None,
    ignore_unreachables: bool = False,
) -> list[str]:
    """Before anything changes, for every phase at once: each fixed entity the cases still to run use (none `done`
    has a result for) is in HA, on its integration and available, and, when the run roams, each roaming bulb is on a
    network with its light available. Prints what it found; returns the problems, which would otherwise stop the run
    part way, after moves. Rig-made groups and scenes don't exist yet, and a roaming bulb's light may still be under
    another ID, so neither is looked up as an entity.
    With `ignore_unreachables`, an unavailable fixed entity is no problem: it becomes `profile.offline`, and its cases
    skip as unreachable (engine.unreachable). A roaming bulb is still checked strictly.
    `homes` are the bulbs whose Hue home the home record holds, which a run puts back."""
    fixed = [e for e in targets(suites, profile, registry, keyword, done=done) if e not in profile.made_entities]
    bulbs = list(profile.roam) if roaming else []
    print(f"Checking profile {profile.name}: {len(fixed)} entities" + (f", {len(bulbs)} roaming." if bulbs else "."))
    if not (env.get("HA_URL") and env.get("HA_TOKEN")):
        return ["Can't check the devices: HA_URL and HA_TOKEN must be set in .env.rig"]
    problems: list[str] = []
    try:
        ha, admin = await connect(env)
    except (OSError, aiohttp.ClientError, RuntimeError) as err:
        return [f"Can't reach HA at {env.get('HA_URL')}: {err or type(err).__name__}"]
    try:
        found, offline = await check_fixed(
            ha, admin, fixed, suites, profile, registry, keyword, done, ignore_unreachables
        )
        problems += found
        if bulbs:
            problems += await check_roaming(env, admin, bulbs, ha, homes)
    finally:
        await ha.close()
        if admin:
            await admin.close()
    for problem in problems:
        print(f"  {problem}")
    for entity in sorted(offline):
        print(f"  {entity}: unavailable; its cases skip (--ignore-unreachables)")
    print(f"{len(problems)} problem(s): nothing was changed." if problems else "Every device is ready.")
    profile.offline = frozenset(offline)
    return problems


def print_unreachable(
    suites: list[Suite], profile: Profile, registry: AdapterRegistry, keyword: str | None, done: Done | None = None
) -> None:
    """The cases still to run that skip as unreachable, by suite, once the check has found what is offline."""
    if not profile.offline:
        return
    only = unfinished(done)
    print("Unreachable, skipped (their suites write no record this run):")
    for suite in runnable_suites(suites, registry):
        for case in cases(suite, profile, keyword, adapters_for(suite, registry)):
            if case.unreachable and (only is None or only(case)):
                print(f"  {suite.integration} / {suite.name} {case.id}: {case.skip}")


async def check_roaming(
    env: dict, admin: HAClient | None, bulbs: list[RoamBulb], ha: HAClient, homes: set[str] = frozenset()
) -> list[str]:
    """Where each roaming bulb is, printed, and the problems: one on no network, or whose light is unavailable, or
    one the networks couldn't be asked about."""
    if admin is None or not (env.get("HUE_BRIDGE_HOST") and env.get("HUE_APP_KEY")):
        return ["Can't find the roaming bulbs: that needs HA_ADMIN_TOKEN, HUE_BRIDGE_HOST and HUE_APP_KEY in .env.rig"]
    bridge = HueBridge(env["HUE_BRIDGE_HOST"], env["HUE_APP_KEY"])
    problems = []
    try:
        finder = Roamer(admin, bridge, map(Bulb.of, bulbs), out=lambda _: None)
        home = finder.home_network
        for bulb in finder.bulbs:
            try:
                network = await finder.locate(bulb)
            except (BridgeError, HAError, OSError, aiohttp.ClientError, TimeoutError) as err:
                problems.append(f"{bulb.entity_id}: can't tell which network it is on: {err or type(err).__name__}")
                continue
            state = await ha.get_state(bulb.entity_id)
            if network is None:
                problems.append(f"{bulb.entity_id}: not on any Zigbee network. {BLUETOOTH_RESET}")
            elif state is not None and not available(state):
                problems.append(f"{bulb.entity_id}: on {NAMES[network]}, unavailable; check it is powered")
            elif network != home and bulb.entity_id in homes:
                print(f"  {bulb.entity_id}: on {NAMES[network]}; the home record has its Hue home for a run to restore")
            elif network != home:
                print(
                    f"  {bulb.entity_id}: on {NAMES[network]}. The run moves it from there; it has no saved Hue home, "
                    "so put its rooms and scenes back by hand after its first visit."
                )
            else:
                print(f"  {bulb.entity_id}: on {NAMES[network]}")
    finally:
        await bridge.close()
    return problems


def cancel_on_signals() -> None:
    """Have Ctrl-C, SIGTERM and SIGHUP cancel the running task, so its finally blocks put the devices back. The first
    one restores the default handlers: a second kills the process (Ctrl-C: interrupts it), leaving the journal."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()

    def abort() -> None:
        for signum in ABORT_SIGNALS:
            loop.remove_signal_handler(signum)
        print("\nAborting: putting devices back.")
        task.cancel()

    for signum in ABORT_SIGNALS:
        loop.add_signal_handler(signum, abort)


async def connect(env: dict) -> tuple[HAClient, HAClient | None]:
    """The HA connection, and the admin one when .env.rig has HA_ADMIN_TOKEN."""
    ha = HAClient(env["HA_URL"], env["HA_TOKEN"])
    admin = HAClient(env["HA_URL"], env["HA_ADMIN_TOKEN"]) if env.get("HA_ADMIN_TOKEN") else None
    await ha.connect()
    if admin:
        try:
            await admin.connect()
        except BaseException:
            await ha.close()
            raise
    return ha, admin


async def recover_rig(env: dict, journal: Journal) -> None:
    if not (env.get("HA_URL") and env.get("HA_TOKEN")):
        raise SystemExit("HA_URL and HA_TOKEN must be set in .env.rig to put back what an aborted run left changed")
    ha, admin = await connect(env)
    bridge = None
    if env.get("HUE_BRIDGE_HOST") and env.get("HUE_APP_KEY"):
        bridge = HueBridge(env["HUE_BRIDGE_HOST"], env["HUE_APP_KEY"])
    try:
        await recover(ha, admin, journal, bridge=bridge)
    finally:
        if bridge:
            await bridge.close()
        await ha.close()
        if admin:
            await admin.close()


async def recover(
    ha: HAClient,
    admin: HAClient | None,
    journal: Journal,
    out: Callable[[str], None] = print,
    bridge: HueBridge | None = None,
) -> None:
    """Put back what an aborted run left changed, in the order its finally blocks would have: DeviceSide's entry
    enabled, the case's entities (whose stored attributes go back through it), DeviceSide's options, the roaming
    bulbs (the ds-rig-* groups removed, the bulbs moved home, their Hue rooms and scenes put back), what it holds in
    the baseline (its pauses, log levels and Z2M options) and the automations a journal from before the baseline
    paused.
    A step that fails is printed and dropped from the journal, except an entity that couldn't be put back, which stays
    for the next try; options, and a disabled entry, stay in it while there is no admin connection. The bulbs can't
    be left half done, so a failure there stops recovery, keeping the rest journaled."""
    run_of = f" (profile {name})" if (name := journal.get("profile")) else ""
    out(f"An aborted rig run{run_of} left changes in {journal.path}; putting them back.")
    await recover_entry(admin, journal, out)
    for key, entry in dict(journal.get("cases") or {}).items():
        await recover_case(ha, entry, out, journal=journal, journal_key=key).restore()
    if taken := journal.get("taken"):  # a journal from before lanes, with one case at the top level
        entry = {"taken": taken, "stored": journal.get("stored", {}), "power": journal.get("power", {})}
        await recover_case(ha, entry, out).restore()
        journal.clear("taken", "stored", "power")
    if (options := journal.get("options")) is not None:
        if admin is None:
            out("Putting DeviceSide's options back needs HA_ADMIN_TOKEN in .env.rig; they stay in the journal.")
        else:
            try:
                await EntryOptions(admin).apply(options)
                out("Put DeviceSide's options back.")
            except Exception as err:  # noqa: BLE001 - as resume: say so, and go on to the automations
                out(f"Could not put DeviceSide's options back; set them in HA: {err}")
            journal.clear("options")
    if journal.get("roam") is not None:
        await recover_roam(admin, bridge, journal, out)
    await Baseline(admin or ha, journal.path.parent, journal.name, out).release()
    if paused := journal.get("paused"):  # a run from before the pause ledger kept its pauses here
        for entity_id in paused:
            try:
                await ha.call_service(entity_id.partition(".")[0], "turn_on", {"entity_id": entity_id})
                out(f"Resumed {entity_id}.")
            except Exception as err:  # noqa: BLE001 - try every one, and say which stayed off
                out(f"Could not turn {entity_id} back on: {err}. Turn it on by hand.")
        journal.clear("paused")
    drop_profile(journal)


async def recover_entry(admin: HAClient | None, journal: Journal, out: Callable[[str], None]) -> None:
    """Enable DeviceSide's entry, if an aborted run left it disabled; it stays journaled without an admin."""
    if journal.get("entry") is None:
        return
    if admin is None:
        out("Enabling DeviceSide's entry again needs HA_ADMIN_TOKEN in .env.rig; it stays in the journal.")
        return
    try:
        await EntryOptions(admin).move("enable")
        out("Enabled DeviceSide's entry again.")
    except Exception as err:  # noqa: BLE001 - as the options: say so, and go on
        out(f"Could not enable DeviceSide's entry again; enable it in HA: {err}")
    journal.clear("entry")


async def prune_baseline(env: dict, directory: Path) -> None:
    """Put back what the baseline holds for no live run, on the admin connection where there is one: Z2M's level
    and options need it."""
    if not (env.get("HA_URL") and env.get("HA_TOKEN")):
        raise SystemExit("HA_URL and HA_TOKEN must be set in .env.rig to put back what the baseline holds")
    ha, admin = await connect(env)
    try:
        await Baseline(admin or ha, directory, "recover").prune()
    finally:
        await ha.close()
        if admin:
            await admin.close()


def recover_case(ha: HAClient, entry: dict, out: Callable[[str], None], **journaled: Any) -> Snapshot:
    """The snapshot that puts back one case's entities from its journal entry, saying which go back how; `journaled`
    (journal, journal_key) has its restore drop the entry."""
    for entity_id, state in entry["taken"].items():
        if state is not None and state["state"] in ("on", "off"):
            out(f"Putting {entity_id} back {state['state']}.")
    for entity_id, rows in entry.get("auto", {}).items():
        shown = ", ".join(f"{name} {'off' if row is None else f'{row['duration']:g} s'}" for name, row in rows.items())
        out(f"Putting {entity_id}'s {shown} back, waiting out the entry reload a change makes.")
    return Snapshot(
        ha,
        entry.get("power", {}),
        taken=entry["taken"],
        stored=entry.get("stored", {}),
        integration=entry.get("integration"),
        auto=entry.get("auto", {}),
        **journaled,
    )


async def recover_roam(
    admin: HAClient | None, bridge: HueBridge | None, journal: Journal, out: Callable[[str], None]
) -> None:
    """Remove the journal's ds-rig-* groups, move its roaming bulbs straight home and put back their Hue rooms and
    scenes; stop on a failure."""
    if admin is None or bridge is None:
        raise SystemExit(
            "Moving the roaming bulbs home needs HA_ADMIN_TOKEN, HUE_BRIDGE_HOST and HUE_APP_KEY in .env.rig; "
            f"the journal ({journal.path}) keeps where they are."
        )
    roamer = Roamer.from_journal(admin, bridge, journal, out)
    out(f"Moving the roaming bulbs home to {NAMES[roamer.home_network]}.")
    try:
        # A group that can't be removed is printed and stays journaled; the rest of recovery goes on.
        await roamer.go(roamer.home_network)
    except RoamError as err:
        raise SystemExit(f"Recovery stopped: {err}") from None


def drop_profile(journal: Journal | None) -> None:
    """Clear the journal's `profile` and `claim` once nothing they describe a run for is left, so an empty journal is
    deleted; while a change is left, they stay, saying which profile's run left it and what putting it back needs."""
    if journal is not None and set(journal.sections) <= {"profile", "claim"}:
        journal.clear("profile", "claim")


if __name__ == "__main__":
    # Line by line even when stdout is a file, so a background caller sees "waiting for the lock" and each result
    # as they happen rather than when the buffer fills.
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
