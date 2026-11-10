"""Runs the rig's YAML step files against the live HA and renders test records from the results.

One file per action (`steps/<action>.yaml`) runs on every integration in the profile that has that action's adapter; a
file with `options` (`steps/<action>-<option>.yaml`) runs it again with those DeviceSide options on, only on adapters
whose scope declares them. The registry supplies what differs per integration: the expected status, the tolerance,
and the options in scope.
No pytest here: scripts/rig.py calls run().
"""

import ast
import asyncio
import contextlib
import datetime
import hashlib
import json
import logging
import operator
import re
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from .baseline import Baseline
from .bridge import BridgeError, HueBridge
from .client import HAClient, HAError, device_ieee
from .journal import Journal
from .observed import FILE as OBSERVED_FILE
from .observed import Device as Maker
from .observed import Observation, ObservedFile, Trace, render_call, sequence, short_hash
from .observed import merge as merge_row
from .profile import ENTRY_RELOADS, GROUP_ROLES, RIG_DIR, ROLES, SETTLE_S, TOLERANCE_S, Profile

_LOGGER = logging.getLogger(__name__)

STEPS_DIR = RIG_DIR / "steps"

# Light attributes a restore puts back, in the order light.turn_on would accept one color at a time.
_RESTORE_COLOR = ("color_temp_kelvin", "hs_color", "xy_color", "rgb_color")

# Integrations whose adapter calls an admin-only action with the caller's context, so their suites run on
# HA_ADMIN_TOKEN. ZHA: issue_zigbee_cluster_command is registered with async_register_admin_service.
# Zigbee2MQTT: the z2m_option steps publish and subscribe over MQTT, which only an admin may.
ADMIN_INTEGRATIONS = frozenset({"zha", "zigbee2mqtt"})

# An entity whose supply a case switched rejoins its network after power returns. ZHA keeps showing the last state
# of a mains device for hours, so the restore waits, then retries until the device takes the command.
POWER_SETTLE_S = 5.0
POWER_REJOIN_S = 60.0
POWER_RETRY_S = 3.0

# After a case puts lights back, Zigbee2MQTT still publishes what it reads back from them about 0.5 s later, and
# republishes a group's state onto every member, so a late `on` from one light overwrites another's `off` in the next
# case. The restore waits this long before the next case starts.
RESTORE_SETTLE_S = 1.5

# A light reports off as its fade starts: an observed case waits this long after setup, so the brief comes once the
# lights have reached the start state it describes.
SETUP_SETTLE_S = 2.0
NOT_SEEN = "not seen: "  # an observed case's failure detail when the person saw something else
TAKEN_BACK = "a fail seen this run blocks its lend"  # a lent case's detail once a fail takes its pass back

# ZCL Color Control's hue attributes, which bulb_hue reads on ZHA.
_COLOR_CLUSTER, _CURRENT_HUE, _ENHANCED_CURRENT_HUE = 0x0300, 0x0000, 0x4000

# The journal key of a case that runs alone (Snapshot.journal_key); a case in a lane uses its integration.
SERIAL = "serial"
UNRESTORED = "+unrestored"  # appended to a case's journal key for the entities its restore couldn't put back

# How long after saving options the engine waits before checking the entry has reloaded.
OPTIONS_RELOAD_S = 1.0
# The step of DeviceSide's options menu that holds its options.
SETTINGS_STEP = "settings"

# Actions whose calls leave a prediction behind (SETTLE_S): the raises and lowers, which run for up to their
# sweep_time (default: actions/ramp.py DEFAULT_SWEEP_TIME), and stop.
_RAMPS = frozenset(
    f"{direction}_{attribute}" for direction in ("raise", "lower") for attribute in ("brightness", "saturation", "hue")
)
_PREDICTED = _RAMPS | {"stop"}
SWEEP_TIME_S = 5.0

# The statuses of a call that wrote to the device; `unchanged` and `rejected` sent nothing.
_WRITES = frozenset({"confirmed", "sent"})
# The rows of get_attributes a snapshot keeps for a case that calls an ENTRY_RELOADS action, and their set and clear.
AUTO_ROWS = ("auto_off", "auto_on")


class SchemaError(ValueError):
    """A step file that does not parse; the message names the file, test and step."""


class StepFailure(AssertionError):
    """A step whose expectation did not hold."""


# --- Expressions: numbers, `duration`, `tol`, and + - * --------------------------------------------------------

_OPS: dict[type, Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
}
NAMES = frozenset({"duration", "tol"})


@dataclass(frozen=True, slots=True)
class Expr:
    """A duration in seconds, such as `6`, `tol` or `duration - tol`, checked when the file loads."""

    node: ast.expr

    @classmethod
    def parse(cls, value: Any) -> Expr:
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            raise ValueError(f"expected a number or an expression, got {value!r}")
        try:
            node = ast.parse(str(value), mode="eval").body
        except SyntaxError:
            raise ValueError(f"cannot parse {value!r}") from None
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                if child.id not in NAMES:
                    raise ValueError(f"unknown name {child.id!r} in {value!r}; use {', '.join(sorted(NAMES))}")
            elif isinstance(child, ast.Constant):
                if isinstance(child.value, bool) or not isinstance(child.value, int | float):
                    raise ValueError(f"{child.value!r} in {value!r} is not a number")
            elif not isinstance(child, (ast.BinOp, ast.UnaryOp, ast.USub, ast.Load, *_OPS)):
                raise ValueError(f"{value!r} uses something other than numbers, names and + - *")
        return cls(node)

    def eval(self, names: dict[str, float]) -> float:
        return _eval(self.node, names)


def _eval(node: ast.expr, names: dict[str, float]) -> float:
    match node:
        case ast.Constant(value=value):
            return float(value)
        case ast.Name(id=name):
            if name not in names:
                raise StepFailure(f"{name} is unset: no call has set a duration yet")
            return names[name]
        case ast.UnaryOp(operand=operand):
            return -_eval(operand, names)
        case ast.BinOp(left=left, op=op, right=right):
            return _OPS[type(op)](_eval(left, names), _eval(right, names))
    raise TypeError(node)  # unreachable: Expr.parse admits nothing else


# --- Step files ----------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Step:
    kind: str
    args: Any
    label: str
    only: frozenset[str] | None = None  # the integrations it runs on; None for every one the test runs on


@dataclass(frozen=True, slots=True)
class Observe:
    """What an observed test asks of the person: what the lights will do, what to watch for, and the question."""

    see: str
    watch: str
    ask: str
    roles: frozenset[str] | None = None  # the roles watched; None for every role the test lists
    only: frozenset[str] | None = None  # the integrations watched; None for every one the test runs on


# How far a steady sample may spread, in its source's units: a Zigbee bulb can settle one level step (1/254) after a
# stop. HA's brightness is 0-255; DeviceSide's is in percent, rounded to 0.1, which can widen the step by 0.1.
STEADY_SPREAD = {("brightness", "ha"): 255 / 254 + 1e-9, ("brightness", "deviceside"): 100 / 254 + 0.1 + 1e-9}

# Steps that check something; an observed run turns them into the waits they imply.
CHECKS = frozenset(
    {"expect_response", "expect_error", "expect_state", "expect_matches_read", "sample", "expect_no_reload"}
)


@dataclass(frozen=True, slots=True)
class RigTest:
    name: str
    roles: tuple[str, ...]
    only: frozenset[str] | None
    steps: tuple[Step, ...]
    setup: tuple[Step, ...] = ()
    observe: Observe | None = None
    # A device behavior (precedence, bounds, timing) rather than the adapter's code: its result stands across code
    # changes while the device's model and firmware are those it ran at (ha_rig/progress.py, Firmware facts).
    firmware_fact: bool = False
    # A hash of the test as written, so a firmware fact's saved result goes when its test changes.
    digest: str = field(default="", compare=False)

    @property
    def automated(self) -> bool:
        """Whether the test has an automated case: always without `observe`, else when its steps check something
        and its setup needs no one."""
        if self.observe is None:
            return True
        return any(step.kind in CHECKS for step in self.steps) and not any(step.kind == "do" for step in self.setup)

    def runs_on(self, integration: str) -> bool:
        return self.only is None or integration in self.only

    def observed_on(self, role: str, integration: str) -> bool:
        """Whether the test has an observed case on `role` in `integration`'s suite."""
        if self.observe is None:
            return False
        return (self.observe.roles is None or role in self.observe.roles) and (
            self.observe.only is None or integration in self.observe.only
        )

    @property
    def calls(self) -> tuple[str, ...]:
        """The other actions its `call` steps name (`call: {action: turn_on_for}`), in first-use order."""
        steps = (*self.setup, *self.steps)
        named = (step.args.get("action") for step in steps if step.kind == "call" and isinstance(step.args, dict))
        return tuple(dict.fromkeys(filter(None, named)))

    def roles_needed(self, role: str) -> list[str]:
        """The case's own role and every role a step targets, in first-use order."""
        steps = (*self.setup, *self.steps)
        targets = (step.args.get("target") for step in steps if isinstance(step.args, dict))
        return list(dict.fromkeys([role, *filter(None, targets)]))


@dataclass(frozen=True, slots=True)
class StepFile:
    path: Path
    action: str
    rig_seconds: int
    tests: tuple[RigTest, ...]
    # The DeviceSide options its suites run with on; every other option in the adapter's scope runs off.
    options: frozenset[str] = frozenset()
    # Patterns for the DeviceSide warnings and errors in HA's log its tests mean to cause (runlogs.RunLogs).
    expected_log: tuple[re.Pattern[str], ...] = ()
    # The name its record takes in place of the action's, for a file that tests something else through it (lifecycle).
    name: str | None = None

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(role for test in self.tests for role in test.roles))

    def runs_on(self, integration: str) -> bool:
        """Whether any test runs on `integration`. A file with none has no cases there, so it isn't selected;
        cases() filters its tests with the same RigTest.runs_on."""
        return any(test.runs_on(integration) for test in self.tests)

    @property
    def uses_bridge(self) -> bool:
        return any(step.kind == "bridge" for test in self.tests for step in (*test.setup, *test.steps))

    @property
    def moves_entry(self) -> bool:
        """Whether a step reloads, disables or enables DeviceSide's entry: like saving options, that cuts off every
        other run's and lane's calls, so the file runs alone, holding HA exclusively, on the admin token."""
        return any(step.kind == "entry" for test in self.tests for step in (*test.setup, *test.steps))

    @property
    def reloads(self) -> bool:
        """Whether its suites reload DeviceSide's entry: they set options, or move the entry."""
        return bool(self.options) or self.moves_entry


def _state(value: Any) -> Any:
    # YAML 1.1 reads a bare on/off as a boolean.
    return {True: "on", False: "off"}.get(value, value) if isinstance(value, bool) else value


def _mapping(args: Any, required: Iterable[str] = (), allowed: Iterable[str] | None = None) -> dict:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError(f"expected a mapping, got {args!r}")
    if missing := [key for key in required if key not in args]:
        raise ValueError(f"missing {', '.join(missing)}")
    if allowed is not None and (unknown := sorted(set(args) - set(allowed) - set(required))):
        raise ValueError(f"unknown key(s) {', '.join(unknown)}")
    if (target := args.get("target")) is not None and target not in ROLES:
        raise ValueError(f"target {target!r} is not a profile role")
    return dict(args)


def _expected(value: Any) -> Any:
    """A literal, or a {min, max} range for a number that drifts (brightness, color temperature)."""
    if isinstance(value, dict):
        if not value or set(value) - {"min", "max"}:
            raise ValueError(f"a range takes min and/or max, got {value!r}")
        if not all(isinstance(bound, int | float) for bound in value.values()):
            raise ValueError(f"range bounds must be numbers, got {value!r}")
    return value


def _parse_set(args: Any) -> dict:
    args = _mapping(args, required=["state"])
    args["state"] = _state(args["state"])
    if args["state"] not in ("on", "off"):
        raise ValueError(f"state must be on or off, got {args['state']!r}")
    return args


def _parse_call(args: Any) -> dict:
    args = _mapping(args)
    if "duration" in args:
        args["duration"] = Expr.parse(args["duration"])
    return args


def _parse_expect_response(args: Any) -> dict:
    args = _mapping(args, allowed=["target", "status", "integration", "detail", "clamped_to", "attributes"])
    if not isinstance(attributes := args.get("attributes", {}), dict):
        raise ValueError("attributes must be a mapping")
    args["attributes"] = {name: _expected(value) for name, value in attributes.items()}
    return args


def _parse_expect_error(args: Any) -> dict:
    args = _mapping(args, allowed=["key", "summary"])
    if not args:
        raise ValueError("name a key, a summary or both")
    return args


def _parse_expect_state(args: Any) -> dict:
    args = _mapping(args)
    if "within" in args:
        args["within"] = Expr.parse(args["within"])
    for name, value in args.items():
        if name not in ("target", "within"):
            args[name] = _state(value) if name == "state" else _expected(value)
    if len(set(args) - {"target", "within"}) == 0:
        raise ValueError("name a state or an attribute to expect")
    return args


def _parse_expect_matches_read(args: Any) -> dict:
    args = _mapping(args, required=["attributes", "tolerance"], allowed=["target", "within"])
    if not isinstance(args["attributes"], list) or not args["attributes"]:
        raise ValueError("attributes must be a non-empty list of attribute names")
    args["within"] = Expr.parse(args.get("within", 0))
    return args


def _parse_sample(args: Any) -> dict:
    args = _mapping(args, required=["attribute", "for"], allowed=["target", "every", "expect", "min", "max", "source"])
    args["for"] = Expr.parse(args["for"])
    args["every"] = Expr.parse(args.get("every", 0.5))
    if args.get("expect", "rising") not in ("rising", "falling", "steady"):
        raise ValueError(f"expect must be rising, falling or steady, got {args['expect']!r}")
    if args.get("source", "ha") not in ("ha", "deviceside"):
        raise ValueError(f"source must be ha or deviceside, got {args['source']!r}")
    return args


def _parse_expect_no_reload(args: Any) -> dict:
    args = _mapping(args, required=["for"], allowed=["target"])
    args["for"] = Expr.parse(args["for"])
    return args


def _parse_mark_hue(args: Any) -> dict:
    return _mapping(args, allowed=["target"])


def _parse_expect_hue_travel(args: Any) -> dict:
    args = _mapping(args, required=["by", "tolerance"], allowed=["target"])
    for name in ("by", "tolerance"):
        if isinstance(args[name], bool) or not isinstance(args[name], int | float):
            raise ValueError(f"{name} must be a number of degrees, got {args[name]!r}")
    return args


def _parse_do(args: Any) -> str:
    if not isinstance(args, str) or not args.strip():
        raise ValueError("do takes the instruction for the person, as a string")
    return args.strip()


def _parse_z2m_option(args: Any) -> dict:
    args = _mapping(args)
    if not set(args) - {"target"}:
        raise ValueError("name an option")
    return args


def _body_keys(value: Any) -> Any:
    """A body with YAML 1.1's boolean keys put back: a bare `on:` key reads as True, and Hue's is `on`."""
    if isinstance(value, dict):
        return {_state(key): _body_keys(inner) for key, inner in value.items()}
    return value


def _parse_bridge(args: Any) -> dict:
    args = _mapping(args, allowed=["target", "put", "recall"])
    if ("put" in args) == ("recall" in args):
        raise ValueError("give put or recall, one of them")
    if "put" in args and (not isinstance(args["put"], dict) or not args["put"]):
        raise ValueError("put takes the body to write, as a mapping")
    if "put" in args:
        args["put"] = _body_keys(args["put"])
    if "recall" in args and args["recall"] is not True:
        raise ValueError("recall takes true")
    return args


ENTRY_MOVES = ("reload", "disable", "enable")


def _parse_entry(args: Any) -> str:
    if args not in ENTRY_MOVES:
        raise ValueError(f"entry takes {', '.join(ENTRY_MOVES)}, got {args!r}")
    return args


def _parse_restore(args: Any) -> None:
    if args not in (None, {}):
        raise ValueError("restore takes no arguments")


PARSERS: dict[str, Callable[[Any], Any]] = {
    "set": _parse_set,
    "call": _parse_call,
    "expect_response": _parse_expect_response,
    "expect_error": _parse_expect_error,
    "expect_state": _parse_expect_state,
    # HA's state matches a live deviceside.get_attributes read: what HA shows is where the light really is.
    "expect_matches_read": _parse_expect_matches_read,
    "wait": Expr.parse,
    # The target doesn't go unavailable (an entry reload) for `for` seconds.
    "expect_no_reload": _parse_expect_no_reload,
    "sample": _parse_sample,
    # The bulb's own hue now, and how far round its own wheel it has turned since: a hue move's rate is in its units.
    "mark_hue": _parse_mark_hue,
    "expect_hue_travel": _parse_expect_hue_travel,
    "do": _parse_do,
    "restore": _parse_restore,
    # DeviceSide's config entry reloaded, disabled or enabled again; a disabled entry is enabled when the case ends.
    "entry": _parse_entry,
    # A write to the Hue bridge that bypasses HA; fails in any other integration's suite.
    "bridge": _parse_bridge,
    # Zigbee2MQTT device options; both steps do nothing in another integration's suite.
    "z2m_option": _parse_z2m_option,
    "expect_z2m_option": _parse_z2m_option,
}


def _parse_step(raw: Any, label: str) -> Step:
    """One `kind: args` mapping, with an optional `only: [<integration>]` beside it limiting the step to those."""
    only = raw.get("only") if isinstance(raw, dict) else None
    if isinstance(raw, dict) and "only" in raw:
        raw = {key: value for key, value in raw.items() if key != "only"}
        if not isinstance(only, list) or not only or not all(isinstance(i, str) and i for i in only):
            raise ValueError(f"{label}: only must be a non-empty list of integration keys")
    if not isinstance(raw, dict) or len(raw) != 1:
        raise ValueError(f"{label}: expected one `kind: args` mapping, got {raw!r}")
    ((kind, args),) = raw.items()
    label = f"{label} ({kind})"
    if kind == "observe":
        raise ValueError(f"{label}: the observe step is gone; give the test observe: {{see, watch, ask}}")
    if kind not in PARSERS:
        raise ValueError(f"{label}: unknown step; use one of {', '.join(PARSERS)}")
    try:
        return Step(kind, PARSERS[kind](args), label, frozenset(only) if only else None)
    except ValueError as err:
        raise ValueError(f"{label}: {err}") from None


def _parse_steps(raw: Any, key: str) -> tuple[Step, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{key} must be a non-empty list")
    prefix = "setup step" if key == "setup" else "step"
    return tuple(_parse_step(step, f"{prefix} {index}") for index, step in enumerate(raw, 1))


def _parse_observe(raw: Any) -> Observe:
    try:
        args = _mapping(raw, required=("see", "watch", "ask"), allowed=("roles", "only"))
        roles = args.pop("roles", None)
        only = args.pop("only", None)
        if not all(isinstance(value, str) and value.strip() for value in args.values()):
            raise ValueError("see, watch and ask must be non-empty strings")
        if roles is not None and (
            not isinstance(roles, list) or not roles or not all(isinstance(r, str) for r in roles)
        ):
            raise ValueError("roles must be a non-empty list of roles")
        if only is not None and (not isinstance(only, list) or not only or not all(isinstance(i, str) for i in only)):
            raise ValueError("only must be a non-empty list of integration keys")
    except ValueError as err:
        raise ValueError(f"observe: {err}") from None
    return Observe(
        args["see"].strip(),
        args["watch"].strip(),
        args["ask"].strip(),
        frozenset(roles) if roles else None,
        frozenset(only) if only else None,
    )


def _parse_test(raw: Any) -> RigTest:
    if not isinstance(raw, dict) or not isinstance(name := raw.get("name"), str):
        raise ValueError(f"a test needs a name, got {raw!r}")
    try:
        if "brief" in raw:
            raise ValueError("brief is gone; give the test observe: {see, watch, ask}")
        if unknown := sorted(set(raw) - {"name", "roles", "only", "setup", "steps", "observe", "firmware_fact"}):
            raise ValueError(f"unknown key(s) {', '.join(unknown)}")
        roles = raw.get("roles")
        if not isinstance(roles, list) or not roles:
            raise ValueError("roles must be a non-empty list")
        if unknown := sorted(set(roles) - ROLES):
            raise ValueError(f"unknown role(s) {', '.join(unknown)}")
        only = raw.get("only")
        if only is not None and (not isinstance(only, list) or not only):
            raise ValueError("only must be a non-empty list of integration keys")
        if not isinstance(firmware_fact := raw.get("firmware_fact", False), bool):
            raise ValueError("firmware_fact must be true or false")
        steps, setup, observe = _parse_phases(raw)
        test = RigTest(
            name, tuple(roles), frozenset(only) if only else None, steps, setup, observe, firmware_fact, _digest(raw)
        )
        _check_observe_scope(test)
        _check_step_scope(test)
    except ValueError as err:
        raise ValueError(f"test {name}, {err}") from None
    return test


def _check_observe_scope(test: RigTest) -> None:
    """`observe: roles` and `only` name some of the test's roles and integrations, and the ones they leave out still
    get an automated run."""
    if test.observe is None or (test.observe.roles is None and test.observe.only is None):
        return
    if outside := sorted((test.observe.roles or frozenset()) - set(test.roles)):
        raise ValueError(f"observe roles {', '.join(outside)} are not the test's roles")
    if test.only is not None and (outside := sorted((test.observe.only or frozenset()) - test.only)):
        raise ValueError(f"observe only {', '.join(outside)} are not in the test's only")
    if not test.automated:
        raise ValueError("observe roles or only needs an automated run for what it leaves out")


def _digest(raw: dict) -> str:
    return hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _check_step_scope(test: RigTest) -> None:
    """A step's `only` names integrations the test runs on: one outside them would never run."""
    if test.only is None:
        return
    for step in (*test.setup, *test.steps):
        if step.only is not None and (outside := sorted(step.only - test.only)):
            raise ValueError(f"{step.label}: only {', '.join(outside)} are not in the test's only")


def _parse_phases(raw: dict) -> tuple[tuple[Step, ...], tuple[Step, ...], Observe | None]:
    """A test's steps, setup and observe block, with `do` only in the setup of an observed test."""
    steps = _parse_steps(raw.get("steps"), "steps")
    setup = _parse_steps(raw["setup"], "setup") if "setup" in raw else ()
    observe = _parse_observe(raw["observe"]) if "observe" in raw else None
    if observe is not None and not setup:
        raise ValueError("a test with observe needs setup: the steps that bring the lights to where see begins")
    if do := next((step for step in steps if step.kind == "do"), None):
        raise ValueError(f"{do.label}: do belongs in setup, before the brief")
    if observe is None and (do := next((step for step in setup if step.kind == "do"), None)):
        raise ValueError(f"{do.label}: do is only for a test with observe")
    return steps, setup, observe


def load_step_file(path: Path) -> StepFile:
    try:
        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("expected a mapping with action, rig_seconds and tests")
        if unknown := sorted(set(raw) - {"action", "name", "rig_seconds", "options", "expected_log", "tests"}):
            raise ValueError(f"unknown key(s) {', '.join(unknown)}")
        if not isinstance(action := raw.get("action"), str):
            raise ValueError("action must name the DeviceSide action under test")
        options = raw.get("options", [])
        if not isinstance(options, list) or not all(isinstance(name, str) and name for name in options):
            raise ValueError("options must be a list of DeviceSide option names")
        expected_log = _parse_expected_log(raw.get("expected_log", []))
        if isinstance(seconds := raw.get("rig_seconds"), bool) or not isinstance(seconds, int) or seconds <= 0:
            raise ValueError("rig_seconds must be a positive whole number")
        if not isinstance(raw_tests := raw.get("tests"), list) or not raw_tests:
            raise ValueError("tests must be a non-empty list")
        tests = tuple(_parse_test(test) for test in raw_tests)
        if duplicates := sorted({t.name for t in tests if [u.name for u in tests].count(t.name) > 1}):
            raise ValueError(f"duplicate test name(s) {', '.join(duplicates)}")
        step_file = StepFile(path, action, seconds, tests, frozenset(options), expected_log)
        step_file = _named(step_file, raw.get("name"))
    except (ValueError, yaml.YAMLError) as err:
        raise SchemaError(f"{path.name}: {err}") from None
    return step_file


def _parse_expected_log(raw: Any) -> tuple[re.Pattern[str], ...]:
    """`expected_log`: regular expressions, each searched for in a DeviceSide warning or error line of HA's log."""
    if not isinstance(raw, list) or not all(isinstance(pattern, str) and pattern for pattern in raw):
        raise ValueError("expected_log must be a list of regular expressions")
    patterns = []
    for pattern in raw:
        try:
            patterns.append(re.compile(pattern))
        except re.error as err:
            raise ValueError(f"expected_log {pattern!r}: {err}") from None
    return tuple(patterns)


def _named(step_file: StepFile, name: Any) -> StepFile:
    """The file with its record's `name`, which a file with entry steps needs: its record would replace the
    action's."""
    if name is None:
        if step_file.moves_entry:
            raise ValueError(f"a file with entry steps needs a name, or its record would replace {step_file.action}'s")
        return step_file
    if not isinstance(name, str) or not name or name == step_file.action or step_file.options:
        raise ValueError("name must name the record, other than the action's, of a file without options")
    return replace(step_file, name=name)


def load_step_files(steps_dir: Path = STEPS_DIR) -> list[StepFile]:
    """Every `<action>.yaml` directly in steps_dir."""
    return [load_step_file(path) for path in sorted(steps_dir.glob("*.yaml"))]


# --- Planning ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Suite:
    """One step file on one integration."""

    file: StepFile
    integration: str

    @property
    def action(self) -> str:
        return self.file.action

    @property
    def name(self) -> str:
        """The name its record takes: the file's `name`, else the action, then any options it runs with on
        (`move-predicted_state`)."""
        return self.file.name or "-".join([self.action, *sorted(self.file.options)])

    def test(self, case_id: str) -> RigTest | None:
        """The test a case id (`name[role]`, maybe ` observed`) is a run of."""
        name = case_id.partition("[")[0]
        return next((test for test in self.file.tests if test.name == name), None)


@dataclass(frozen=True, slots=True)
class Case:
    """One run of a test on one role, automated or observed: the entities it moves, or why it is skipped."""

    suite: Suite
    test: RigTest
    role: str
    entities: dict[str, str]
    skip: str | None = None
    observed: bool = False
    members: tuple[str, ...] = ()  # the profile's members for the group role, if it lists them
    # The skip is for a device that is offline now (--ignore-unreachables), not for the profile: never saved, and the
    # suite stays incomplete.
    unreachable: bool = False
    # The skip is for a role the profile lacks (`no <role> entity in the profile`), so a record written from it would
    # narrow what an earlier profile proved.
    absent: bool = False

    @property
    def id(self) -> str:
        return f"{self.test.name}[{self.role}]" + (" observed" if self.observed else "")

    @property
    def twin(self) -> str | None:
        """The id of the test's other case on this role, when it has both: either proves nothing alone."""
        if not self.test.observed_on(self.role, self.suite.integration) or not self.test.automated:
            return None
        return replace(self, observed=not self.observed).id


def adapters_for(suite: Suite, registry: Any) -> frozenset[str]:
    """The actions the suite's tests call that have an adapter on its integration, for `cases`."""
    called = {action for test in suite.file.tests for action in test.calls}
    return frozenset(action for action in called if registry.adapter(action, suite.integration) is not None)


def applies(suite: Suite, registry: Any) -> bool:
    """Whether the suite runs on its integration: the adapter exists and its scope has every option the file sets (a
    file with none applies wherever there is an adapter). An option file that doesn't apply is not part of the run."""
    adapter = registry.adapter(suite.action, suite.integration)
    return adapter is not None and suite.file.options <= adapter.scope.options


def check_options(step_files: Iterable[StepFile], registry: Any) -> None:
    """Raise SchemaError for an option that no adapter of the file's action, on any integration, declares: a typo."""
    for step_file in step_files:
        declared = {
            option
            for action, _, adapter in registry.matrix()
            if action == step_file.action and adapter is not None
            for option in adapter.scope.options
        }
        if unknown := sorted(step_file.options - declared):
            raise SchemaError(f"{step_file.path.name}: no {step_file.action} adapter has option {unknown[0]}")


def cases(
    suite: Suite, profile: Profile, keyword: str | None = None, adapters: frozenset[str] | None = None
) -> list[Case]:
    """The suite's cases in file order, a test's automated case before its observed one; `keyword` keeps only case
    ids containing it. With `adapters` (from adapters_for), a test calling another action without one is skipped."""
    result = []
    for test in suite.file.tests:
        if not test.runs_on(suite.integration):
            continue
        lacking = next((a for a in test.calls if adapters is not None and a not in adapters), None)
        for role in test.roles:
            for observed in [False] * test.automated + [True] * test.observed_on(role, suite.integration):
                case = Case(suite, test, role, {}, observed=observed)
                if keyword and keyword not in case.id:
                    continue
                if lacking:
                    result.append(replace(case, skip=f"no {lacking} adapter on {suite.integration}"))
                    continue
                needed = test.roles_needed(role)
                entities = {name: profile.entity(suite.integration, name) for name in needed}
                if missing := [name for name, entity_id in entities.items() if entity_id is None]:
                    case = Case(suite, test, role, {}, f"no {missing[0]} entity in the profile", observed, absent=True)
                else:
                    group = next((name for name in entities if name in GROUP_ROLES), None)
                    members = profile.members(suite.integration, entities[group]) if group else ()
                    # A group case that also targets a light means one of the group's: where the profile lists them.
                    lights = [name for name in entities if name.startswith("light_")]
                    outside = [name for name in lights if members and entities[name] not in members]
                    skip = f"{outside[0]} is not one of the group's members" if outside else None
                    case = Case(suite, test, role, entities, skip, observed, members)
                    if skip is None and (why := unreachable(case, profile)):
                        case = replace(case, skip=why, unreachable=True)
                result.append(case)
    return result


def unreachable(case: Case, profile: Profile) -> str | None:
    """Why the case can't run while some of the profile's fixed entities are offline (--ignore-unreachables), or None:
    one of its entities, a group member or a supply is offline; or, as the profile can't say which devices they
    hold, it targets a fixed group whose members it doesn't list, or a fixed scene. Rig-made groups and scenes are
    made of roaming bulbs, which a run checks strictly, so they stay in scope."""
    if not profile.offline:
        return None
    power = profile.power()
    devices = [*case.entities.values(), *case.members]
    if down := next((e for e in [*devices, *filter(None, map(power.get, devices))] if e in profile.offline), None):
        return f"{down} unreachable"
    fixed = {name: entity for name, entity in case.entities.items() if entity not in profile.made_entities}
    if any(name in GROUP_ROLES for name in fixed) and not case.members:
        return "group: members not listed"
    if "scene" in fixed:
        return "scene: contents unknown"
    return None


def in_head(case: Case, profile: Profile) -> bool:
    """Whether the case can run in its integration's lane, alongside the other lanes: its file neither sets
    DeviceSide options nor moves the entry (either reloads it, cutting off every lane's fades), and it touches no
    roaming bulb or rig-made group or scene (they are on one network at a time) and no powered entity or supply (a
    supply cut in one lane would kill another's light). The same for both twins, so an observed case goes with its
    automated one."""
    if case.suite.file.reloads:
        return False
    power = profile.power()
    shared = profile.made_entities | power.keys() | set(power.values())
    return shared.isdisjoint({*case.entities.values(), *case.members})


# --- Running -------------------------------------------------------------------------------------------------


class EntryOptions:
    """DeviceSide's config entry on the rig, on the admin connection: its options, set for each case through the
    entry's options flow and put back when the run ends, and the reloads, disables and enables of entry steps. Saving
    a change reloads the entry. The original options are journaled before the first save, and the entry as `entry`
    before it is disabled, until it is enabled again."""

    def __init__(
        self, admin: HAClient, reload_s: float | None = None, journal: Journal | None = None, reloads: bool = True
    ):
        self.admin = admin
        self.reloads = reloads  # False: a save would reload the entry under other runs' cases, so it is refused
        self.reload_s = OPTIONS_RELOAD_S if reload_s is None else reload_s
        self.journal = journal
        self.entry_id: str | None = None
        self.original: dict[str, Any] | None = None
        self.current: dict[str, Any] = {}
        self.disabled = False

    async def find(self) -> str:
        if self.entry_id is None:
            entries = await self.admin.command({"type": "config_entries/get", "domain": "deviceside"})
            if not entries:
                raise RuntimeError("no DeviceSide config entry on the rig")
            self.entry_id = entries[0]["entry_id"]
        return self.entry_id

    async def move(self, how: str) -> None:
        """An entry step: reload the entry, disable it or enable it, and wait until it has."""
        if not self.reloads:
            raise RuntimeError(f"an entry {how} would cut off other runs' calls; this run holds HA shared")
        entry_id = await self.find()
        if how == "reload":
            await self.admin.rest("post", f"/api/config/config_entries/entry/{entry_id}/reload")
            await self.reloaded()
            return
        if how == "disable":
            # Before the command, so a disable that fails partway is still enabled again, and put back after a crash.
            self.disabled = True
            if self.journal is not None:
                self.journal.update(entry=entry_id)
        await self.admin.command(
            {
                "type": "config_entries/disable",
                "entry_id": entry_id,
                "disabled_by": "user" if how == "disable" else None,
            }
        )
        if how == "disable":
            await self.reloaded("not_loaded")
            return
        self.disabled = False
        if self.journal is not None:
            self.journal.clear("entry")
        await self.reloaded()

    async def apply(self, wanted: dict[str, bool]) -> None:
        if self.original is not None and all(self.current.get(name) == on for name, on in wanted.items()):
            return
        flow_path, values = await self.options_form()
        if self.original is None:
            self.original = dict(values)
            if self.journal is not None:
                self.journal.update(options=self.original)
        if all(values.get(name) == on for name, on in wanted.items()):
            # Saving would still reload an entry whose options were never saved: they go from none to these.
            await self.admin.rest("delete", flow_path)
            self.current = values
            return
        if not self.reloads:
            with contextlib.suppress(Exception):
                await self.admin.rest("delete", flow_path)
            raise RuntimeError(
                f"DeviceSide's options in HA differ from this case's ({wanted}), and saving them reloads the entry "
                "under other runs; this run holds HA shared. Set them back in HA, or run the suite alone with an "
                "option step file, which takes HA exclusively"
            )
        values |= wanted
        try:
            result = await self.admin.rest("post", flow_path, values)
            if result.get("type") != "create_entry":
                raise RuntimeError(f"DeviceSide's options flow did not save: {result}")
        except Exception:
            with contextlib.suppress(Exception):  # don't leave the flow open in HA; the save's error is the one raised
                await self.admin.rest("delete", flow_path)
            raise
        self.current = values
        await self.reloaded()

    async def options_form(self) -> tuple[str, dict]:
        """Open DeviceSide's options flow at its options form: its path, and the options in force (the form's
        defaults). The flow is closed again if it shows no such form."""
        flow = await self.admin.rest("post", "/api/config/config_entries/options/flow", {"handler": await self.find()})
        flow_path = f"/api/config/config_entries/options/flow/{flow['flow_id']}"
        try:
            if flow.get("type") == "menu":
                # DeviceSide's options open on a menu (settings and the issue forms); the options are its settings step.
                if SETTINGS_STEP not in flow.get("menu_options", []):
                    raise RuntimeError(f"DeviceSide's options menu has no {SETTINGS_STEP!r} step: {flow}")
                flow = await self.admin.rest("post", flow_path, {"next_step_id": SETTINGS_STEP})
            # Never read options from anything but the form: no fields would pass for no options, and be put back.
            if flow.get("type") != "form" or not flow.get("data_schema"):
                raise RuntimeError(f"DeviceSide's options flow showed no options form: {flow}")
        except Exception:
            with contextlib.suppress(Exception):  # don't leave the flow open in HA
                await self.admin.rest("delete", flow_path)
            raise
        return flow_path, {field["name"]: field.get("default") for field in flow["data_schema"]}

    async def reloaded(self, state: str = "loaded") -> None:
        """Wait for the reload a saved change schedules, which starts after the save returns, or for a disable or an
        enable: until the entry is in `state`."""
        await asyncio.sleep(self.reload_s)
        query = {"type": "config_entries/get", "domain": "deviceside"}
        async with asyncio.timeout(30):
            while not (entries := await self.admin.command(query)) or entries[0]["state"] != state:
                await asyncio.sleep(0.5)

    async def restore(self) -> None:
        if self.original is not None and self.current != self.original:
            await self.apply(self.original)
        if self.journal is not None:
            self.journal.clear("options")


class Outcome(StrEnum):
    PASS = "pass"
    NOT_OBSERVED = "not observed"
    FAIL = "fail"
    SKIPPED = "skipped"


@dataclass(slots=True)
class CaseResult:
    id: str
    role: str
    domain: str | None
    outcome: Outcome
    detail: str = ""
    entity_ids: tuple[str, ...] = ()
    twin: str | None = None  # Case.twin
    unreachable: bool = False  # Case.unreachable
    absent: bool = False  # Case.absent
    trace: Trace | None = None  # what the case sent, when DeviceSide's wire trace was on
    lent: str | None = None  # an observed case not run: the date of the _observed.md pass that covers its twin's calls


@dataclass(slots=True)
class SuiteResult:
    suite: Suite
    cases: list[CaseResult]
    # False when a case it plans has no result, or only an unreachable skip: a partial run writes no record.
    complete: bool
    # Taken when the suite ends, while its bulbs are on the integration under test: a roaming profile moves them
    # home before the records are written. None when the registry lookup failed.
    ha_version: str | None = None
    devices: list[Device] = field(default_factory=list)
    planned: int | None = None  # how many cases the suite plans, once merge has joined it

    @property
    def unreachable(self) -> int:
        return sum(case.unreachable for case in self.cases)

    @property
    def partial(self) -> str | None:
        """`partial: N of M cases; K unreachable` for a joined suite short of cases, else None."""
        if self.planned is None or self.complete:
            return None
        ran = sum(not case.unreachable for case in self.cases)
        unreachable = f"; {self.unreachable} unreachable" if self.unreachable else ""
        return f"partial: {ran} of {self.planned} cases{unreachable}"

    @property
    def recordable(self) -> bool:
        """A complete run that proved something and failed nothing; an all-unobserved run would erase a record."""
        return self.complete and bool(self.proven) and all(case.outcome is not Outcome.FAIL for case in self.cases)

    @property
    def proven(self) -> list[CaseResult]:
        """Passed cases whose twin passed too: a case with a twin proves nothing alone."""
        passed = {case.id for case in self.cases if case.outcome is Outcome.PASS}
        return [case for case in self.cases if case.id in passed and (case.twin is None or case.twin in passed)]


@dataclass(slots=True)
class Budget:
    """--for: the time a run may start cases until. Once spent, no case starts and in-flight ones finish; `stopped`
    says a case or phase was left for that."""

    deadline: float  # on time.monotonic()
    stopped: bool = False
    granted: bool = False  # a move's one case, whatever the budget

    @property
    def spent(self) -> bool:
        return time.monotonic() >= self.deadline

    @property
    def left(self) -> float:
        """Seconds to the deadline, never below 0."""
        return max(0.0, self.deadline - time.monotonic())

    def grant(self) -> None:
        """Let the next device case start though the budget is spent: a move has bought it. Used once."""
        self.granted = True

    def stops(self, device_case: bool = False) -> bool:
        """Whether to start nothing more, noting that something is left. A `device_case` spends a grant instead."""
        if device_case and self.granted:
            self.granted = False
            return False
        if self.spent:
            self.stopped = True
        return self.stopped


@dataclass(slots=True)
class RunResult:
    suites: list[SuiteResult]

    @property
    def failed(self) -> bool:
        return any(case.outcome is Outcome.FAIL for suite in self.suites for case in suite.cases)


@dataclass(slots=True)
class Snapshot:
    """Each touched entity's state, put back after the case.

    HA shows no brightness or color for a light that is off, so with `deviceside` set (the integration has
    get_attributes and set_attributes adapters) an off light's deviceside attributes are read when it is first
    touched and written back after it is turned off again. A group is skipped there: its members are touched too.
    What it takes is journaled under `cases[journal_key]` before the case moves the entity, and cleared once
    restored: cases running at once in lanes each have their own key, the integration. An entity that couldn't be put
    back moves to `cases[<key>+unrestored]`, which the lane's next case doesn't overwrite, so --recover tries again;
    an entity already there keeps the state it had first.

    For an entity in `reads_auto` (a case calling one of its integration's ENTRY_RELOADS actions), the device's
    persistent auto_off and auto_on are read too, journaled under `auto`, and put back before its on/off: else a
    lingering auto-on would undo the restore's turn-off. A write to them reloads the device's entry, which `reloading`
    waits out (watch_reload); the restore waits for its own before it goes on.
    """

    ha: HAClient
    power: dict[str, str] = field(default_factory=dict)  # Profile.power(): entity -> its supply
    deviceside: bool = False
    taken: dict[str, dict | None] = field(default_factory=dict)
    stored: dict[str, dict] = field(default_factory=dict)
    journal: Journal | None = None
    journal_key: str = SERIAL
    integration: str | None = None  # whose ENTRY_RELOADS a write to `auto` waits for
    reads_auto: frozenset[str] = frozenset()
    auto: dict[str, dict] = field(default_factory=dict)  # entity -> {auto_off, auto_on}, as get_attributes reads them
    reloading: dict[str, asyncio.Task] = field(default_factory=dict)  # entity -> its entry reload's watcher

    def entry(self) -> dict:
        """What the journal keeps for the case: `auto` and `integration` only where a case read auto config."""
        entry = {"taken": self.taken, "stored": self.stored, "power": self.power}
        return entry | ({"auto": self.auto, "integration": self.integration} if self.auto else {})

    async def take(self, entity_id: str, group: bool = False) -> dict | None:
        """The entity's state before the case moved it, and that of every entity it powers."""
        if entity_id not in self.taken:
            state = self.taken[entity_id] = await self.ha.get_state(entity_id)
            if self.deviceside and not group and _is_off_light(entity_id, state):
                response = await self.ha.call_service(
                    "deviceside", "get_attributes", {"entity_id": entity_id}, return_response=True
                )
                if attributes := (response or {}).get(entity_id):
                    self.stored[entity_id] = attributes
            if entity_id in self.reads_auto:
                self.auto[entity_id] = await self.read_auto(entity_id)
            if self.journal is not None:
                self.journal.put("cases", self.journal_key, self.entry())
            for powered, supply in self.power.items():
                if supply == entity_id:
                    await self.take(powered)
        return self.taken[entity_id]

    async def read_auto(self, entity_id: str) -> dict:
        """The entity's auto_off and auto_on rows; raises if the read lacks either, as the case couldn't put it back."""
        response = await self.ha.call_service(
            "deviceside", "get_attributes", {"entity_id": entity_id}, return_response=True
        )
        row = (response or {}).get(entity_id) or {}
        if missing := [name for name in AUTO_ROWS if name not in row]:
            raise StepFailure(
                f"{entity_id}: get_attributes read no {' or '.join(missing)}, so no restore could put it back"
            )
        return {name: row[name] for name in AUTO_ROWS}

    def watch_reload(self, entity_id: str) -> None:
        """Start watching for the entry reload a write to the entity's device makes, at once, so a quick one can't
        slip past; one already pending covers this write too, as core's debouncer coalesces them."""
        if (task := self.reloading.get(entity_id)) is None or task.done():
            deadline_s = ENTRY_RELOADS[self.integration].deadline_s
            self.reloading[entity_id] = asyncio.create_task(self._reloaded(entity_id, deadline_s))

    async def _reloaded(self, entity_id: str, deadline_s: float) -> None:
        """The entity goes unavailable and comes back, within deadline_s of the write; a StepFailure otherwise."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + deadline_s
        try:
            await self.ha.wait_for(entity_id, _unavailable, deadline_s)
        except TimeoutError:
            raise StepFailure(f"{entity_id}: no entry reload within {deadline_s:g} s of the write") from None
        try:
            await self.ha.wait_for(entity_id, lambda state: not _unavailable(state), max(deadline - loop.time(), 0))
        except TimeoutError:
            raise StepFailure(f"{entity_id}: still unavailable {deadline_s:g} s after the write") from None

    async def await_reload(self, entity_id: str | None = None) -> None:
        """Wait out the entity's pending entry reload, or every one; a watcher's failure is raised, once."""
        for pending in [entity_id] if entity_id is not None else list(self.reloading):
            if (task := self.reloading.pop(pending, None)) is not None:
                await task

    async def settle_reloads(self) -> None:
        """Wait out every pending reload before a restore's calls, logging a watcher's failure: the case reports it."""
        for entity_id in list(self.reloading):
            try:
                await self.await_reload(entity_id)
            except Exception as err:  # noqa: BLE001 - logged; the case has reported a failed watcher itself
                _LOGGER.warning("Restoring anyway: %s", err)

    async def put_back_auto(self) -> list[str]:
        """Put each entity's auto_off and auto_on back as they were taken, waiting out the reload a write makes; the
        entities it failed on. The set and clear actions change nothing on a device already there."""
        failed = []
        for entity_id, rows in self.auto.items():
            try:
                for name, row in rows.items():
                    which = name.removeprefix("auto_")
                    action = f"clear_auto_{which}" if row is None else f"set_auto_{which}"
                    data = {"entity_id": entity_id} | (
                        {} if row is None else {"duration": {"seconds": row["duration"]}}
                    )
                    response = await self.ha.call_service("deviceside", action, data, return_response=True)
                    status = ((response or {}).get(entity_id) or {}).get("status")
                    if status in _WRITES:
                        self.watch_reload(entity_id)
                    elif status != "unchanged":
                        raise RuntimeError(f"{action} came back {status}: {response}")
                await self.await_reload(entity_id)
            except Exception as err:  # noqa: BLE001 - as restore(): logged, and --recover tries again
                _LOGGER.warning("Could not put back %s's %s: %s; --recover tries again", entity_id, rows, err)
                failed.append(entity_id)
        return failed

    async def restore(self, clear: bool = True) -> None:
        """Put back every entity taken; `clear=False` keeps the journal, for a case that goes on moving them."""
        await self.settle_reloads()
        failed = await self.put_back_auto()
        # A powered entity goes back after its supply, whatever order the case touched them in.
        cut = [entity_id for entity_id in self.taken if self.power.get(entity_id) in self.taken]
        put_back = False
        for entity_id in [entity_id for entity_id in self.taken if entity_id not in cut] + cut:
            state = self.taken[entity_id]
            if state is None or state["state"] not in ("on", "off"):
                continue
            if entity_id not in cut:
                try:
                    await self.put_back(entity_id, state)
                    put_back = True
                except Exception as err:  # noqa: BLE001 - a failed restore is logged so it can't mask the test result
                    _LOGGER.warning(
                        "Could not restore %s to %s: %s; --recover tries again", entity_id, state["state"], err
                    )
                    failed.append(entity_id)
            elif (self.taken[self.power[entity_id]] or {}).get("state") == "on":  # unpowered before: nothing to restore
                if not await self.put_back_after_power(entity_id, state):
                    failed.append(entity_id)
        if clear and put_back:
            await asyncio.sleep(RESTORE_SETTLE_S)
        if clear and self.journal is not None:
            self.journal.drop("cases", self.journal_key)
            self.keep_unrestored(failed)

    def keep_unrestored(self, failed: list[str]) -> None:
        """Journal the entities a restore failed on under the key's `unrestored` entry, keeping any already there.
        Recovering that entry itself, it was dropped first, so only what failed again is left."""
        if not failed or self.journal is None:
            return
        key = self.journal_key if self.journal_key.endswith(UNRESTORED) else f"{self.journal_key}{UNRESTORED}"
        kept = (self.journal.get("cases") or {}).get(key) or {"taken": {}, "stored": {}, "power": {}}
        for entity_id in failed:
            if entity_id not in kept["taken"]:
                kept["taken"][entity_id] = self.taken[entity_id]
                if entity_id in self.stored:
                    kept["stored"][entity_id] = self.stored[entity_id]
                if entity_id in self.power:
                    kept["power"][entity_id] = self.power[entity_id]
                    supply = self.power[entity_id]
                    kept["taken"].setdefault(supply, self.taken.get(supply))
                if entity_id in self.auto:
                    kept.setdefault("auto", {})[entity_id] = self.auto[entity_id]
                    kept["integration"] = self.integration
        self.journal.put("cases", key, kept)

    async def put_back_after_power(self, entity_id: str, state: dict) -> bool:
        """Put back an entity once its supply's power is back, retrying while it rejoins; whether it went back."""
        loop = asyncio.get_running_loop()
        await asyncio.sleep(POWER_SETTLE_S)
        deadline = loop.time() + POWER_REJOIN_S
        while True:
            try:
                await self.put_back(entity_id, state)
                return True
            except Exception as err:  # noqa: BLE001 - as restore(): retried, then logged
                if loop.time() >= deadline:
                    _LOGGER.warning(
                        "Could not restore %s after power returned: %s; --recover tries again", entity_id, err
                    )
                    return False
            await asyncio.sleep(POWER_RETRY_S)

    async def put_back(self, entity_id: str, state: dict) -> None:
        domain = entity_id.partition(".")[0]
        if state["state"] == "off":
            await self.ha.call_service(domain, "turn_off", {"entity_id": entity_id})
            if (stored := self.stored.get(entity_id)) and (data := _light_data(stored)):
                await self.ha.call_service("deviceside", "set_attributes", {"entity_id": entity_id, **data})
            return
        data = _light_data(state["attributes"]) if domain == "light" else {}
        await self.ha.call_service(domain, "turn_on", {"entity_id": entity_id, **data})


async def _prompt(ask: Callable[[str], str], text: str) -> str:
    """`ask(text)` on a daemon thread. A cancelled run can't interrupt a thread blocked in input(), and
    asyncio.to_thread's executor would hold the exit until someone pressed Enter; a daemon thread is left behind."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def settle(result: str | None, error: BaseException | None) -> None:
        if not future.done():  # cancelled while the person was at the prompt
            if error is None:
                future.set_result(result)
            else:
                future.set_exception(error)

    def read() -> None:
        try:
            result, error = ask(text), None
        except BaseException as err:  # noqa: BLE001 - EOFError and the like belong to the awaiting task
            result, error = None, err
        with contextlib.suppress(RuntimeError):  # the loop closed while the prompt waited
            loop.call_soon_threadsafe(settle, result, error)

    threading.Thread(target=read, name="rig prompt", daemon=True).start()
    return await future


def _unavailable(state: dict | None) -> bool:
    """An entity mid-reload: unavailable, or gone from the state machine."""
    return state is None or state["state"] == "unavailable"


def _is_off_light(entity_id: str, state: dict | None) -> bool:
    """A light that is off and not a group (a group lists its members in its entity_id attribute)."""
    return (
        entity_id.startswith("light.")
        and state is not None
        and state["state"] == "off"
        and "entity_id" not in state["attributes"]
    )


def _light_data(attributes: dict) -> dict:
    """Brightness and the first color attribute light.turn_on (or set_attributes) accepts, from a light's attributes."""
    data = {"brightness": attributes["brightness"]} if attributes.get("brightness") is not None else {}
    for name in _RESTORE_COLOR:
        if attributes.get(name) is not None:
            data[name] = attributes[name]
            break
    return data


def _value(state: dict, name: str) -> Any:
    """The state, or an attribute by dotted name as _lookup reads one (`hs_color.1`)."""
    return state["state"] if name == "state" else _lookup(state["attributes"], name)


def _lookup(row: dict, name: str) -> Any:
    """A response attribute by dotted name (`timer.turns`, or `hs_color.0` for a list item); None where any part is
    absent."""
    value: Any = row
    for part in name.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit():
            value = value[int(part)] if int(part) < len(value) else None
        else:
            return None
    return value


_COLOR_MODES = frozenset({"hs", "xy", "rgb", "rgbw", "rgbww"})
# The call fields a light's color modes decide; a light that can't take one should report it fields_dropped.
_LIGHT_FIELDS = frozenset({"brightness", "min_brightness", "max_brightness", "color_temp_kelvin", *_RESTORE_COLOR})


def _carries(state: dict | None, name: str) -> bool:
    """Whether a light's `supported_color_modes` (present while it is off) can carry an attribute or call field
    (dotted, as _lookup reads one): a mixed group's White member takes no color temperature. Anything else, or an
    entity that lists no modes, carries everything."""
    modes = (state or {}).get("attributes", {}).get("supported_color_modes")
    if not isinstance(modes, list):
        return True
    root = name.partition(".")[0]
    if root == "color_temp_kelvin":
        return "color_temp" in modes
    if root in ("hs_color", "xy_color", "rgb_color"):
        return not _COLOR_MODES.isdisjoint(modes)
    if root in ("brightness", "min_brightness", "max_brightness"):
        return any(mode != "onoff" for mode in modes)
    return True


def _by_modes(wants: dict[str, dict], states: dict[str, dict | None], target: str) -> dict[str, dict]:
    """Each member's wanted attributes, with each its color modes can't carry wanted absent (None) instead: a mixed
    group's White member must show no color temperature. One no member carries fails the step, so a step can't pass
    by checking only absences."""
    shaped = {
        member: {name: value if _carries(states.get(member), name) else None for name, value in want.items()}
        for member, want in wants.items()
    }
    for name in {name for want in wants.values() for name, value in want.items() if value is not None}:
        if not any(_carries(states.get(member), name) for member in wants):
            raise StepFailure(f"no member of {target} takes {name}")
    return shaped


def _matches(actual: Any, want: Any) -> bool:
    if isinstance(want, dict):
        if isinstance(actual, bool) or not isinstance(actual, int | float):
            return False
        return want.get("min", actual) <= actual <= want.get("max", actual)
    return actual == want


def _describe(state: dict | None, names: Iterable[str]) -> str:
    if state is None:
        return "no state"
    return ", ".join(f"{name}={_value(state, name)!r}" for name in names)


def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m"


class _NoOneThere(Exception):
    """EOF at a prompt: nobody is at the rig, so the observed case ends not observed."""


def _timed(readings: list[float], offsets: list[float], call_times: tuple[float, float] | None) -> str:
    """A sample's readings, each with when it was read: after the last call was sent, which tells a light that started
    high from one read late, else after the sample began."""
    timed = ", ".join(f"{reading:.1f} at {offset:.2f} s" for reading, offset in zip(readings, offsets, strict=True))
    if call_times is None:
        return f"[{timed}] into the sample"
    sent_at, returned_at = call_times
    return f"[{timed}] after the call, which took {returned_at - sent_at:.2f} s"


def _plain(value: Any, role: str) -> Any:
    """A step's args as data: an expression as written, and the case's own role, wherever it is named, as `target`."""
    if isinstance(value, Expr):
        return ast.unparse(value.node)
    if isinstance(value, dict):
        return {key: _plain(item, role) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item, role) for item in value]
    return "target" if value == role else value


def starting_state(case: Case) -> str:
    """The hash of the case's setup as written for its integration, its own role as `target`: two cases whose setups
    read the same start their calls from the same state, whichever test or role they are."""
    steps = [step for step in case.test.setup if step.only is None or case.suite.integration in step.only]
    return short_hash([[step.kind, _plain(step.args, case.role)] for step in steps])


class _CaseRun:
    """The steps of one case, against one HA connection, with what earlier steps left behind.

    An observed case runs its setup, briefs the person and waits for Enter, runs its steps with every check relaxed to
    the wait it implies (so the timing matches the automated case), then asks its question.
    """

    def __init__(
        self,
        case: Case,
        ha: HAClient,
        registry: Any,
        tolerance: float,
        ask: Callable[[str], str] | None,
        power: dict[str, str] | None = None,
        out: Callable[[str], None] = print,
        position: str = "",
        busy: dict[str, float] | None = None,
        settle: float = 0.0,
        bridge: HueBridge | None = None,
        journal: Journal | None = None,
        journal_key: str = SERIAL,
        entry: EntryOptions | None = None,
        wire: HAClient | None = None,
        makers: Callable[[], Awaitable[dict[str, Maker]]] | None = None,
    ):
        self.bridge = bridge
        self.wire, self.makers = wire, makers  # the admin connection the wire trace comes on, and its devices
        self.traced: list[dict] | None = None  # the open wire tap's calls, while the case's steps run
        self.entry = entry  # for entry steps; None without an admin connection
        self.case, self.ha, self.registry, self.ask, self.out, self.position = case, ha, registry, ask, out, position
        self.integration = case.suite.integration
        # Each light's loop time once the predictions an earlier move or stop left on it have expired; shared
        # by every case in the run.
        self.busy = {} if busy is None else busy
        self.settle = settle
        self.names: dict[str, float] = {"tol": tolerance}
        self.snapshot = Snapshot(
            ha,
            power or {},
            deviceside=all(
                registry.adapter(action, self.integration) is not None
                for action in ("get_attributes", "set_attributes")
            ),
            journal=journal,
            journal_key=journal_key,
            integration=self.integration,
            reads_auto=self.reads_auto(),
        )
        # Z2M device options a z2m_option step sets, held for the case in the baseline, which puts them back.
        self.baseline: Baseline | None = None
        self.journal, self.journal_key = journal, journal_key
        self.response: dict | None = None
        self.error: HAError | None = None
        self.last_action = case.suite.action
        self.query = False
        # The loop times the last call was sent and returned, which a sample's readings are timed from.
        self.call_times: tuple[float, float] | None = None
        self.sent: dict[str, Any] = {}  # the last call's data
        self.hue_marks: dict[str, float] = {}  # each light's own hue at the last mark_hue

    def reads_auto(self) -> frozenset[str]:
        """The case's lights and switches whose auto config its snapshot keeps: those of a case calling an
        ENTRY_RELOADS action, which writes it."""
        reload = ENTRY_RELOADS.get(self.integration)
        if reload is None or reload.actions.isdisjoint({self.case.suite.action, *self.case.test.calls}):
            return frozenset()
        return frozenset(entity_id for entity_id in self.case.entities.values() if not self.is_group(entity_id))

    def entity(self, args: Any) -> str:
        role = args.get("target") if isinstance(args, dict) else None
        return self.case.entities[role or self.case.role]

    def is_group(self, entity_id: str) -> bool:
        return any(self.case.entities.get(role) == entity_id for role in GROUP_ROLES)

    async def members(self, entity_id: str) -> list[str]:
        """A group's member lights, from its `entity_id` attribute, else the profile's `members:` (a Z2M group light
        has no such attribute); any other entity is its own one member."""
        if not self.is_group(entity_id):
            return [entity_id]
        state = await self.ha.get_state(entity_id)
        members = state and state["attributes"].get("entity_id")
        if isinstance(members, list) and members:
            return members
        if self.case.members:
            return list(self.case.members)
        raise StepFailure(f"{entity_id} lists no members in its entity_id attribute, and the profile gives none")

    async def touch(self, entity_id: str) -> None:
        """Snapshot the entity, and a group's members after it, so a restore puts each member back last."""
        await self.snapshot.take(entity_id, group=self.is_group(entity_id))
        if self.is_group(entity_id):
            for member in await self.members(entity_id):
                await self.snapshot.take(member)

    async def run(self) -> CaseResult:
        case = self.case
        domain = case.entities[case.role].partition(".")[0]
        result = CaseResult(
            case.id, case.role, domain, Outcome.PASS, entity_ids=tuple(case.entities.values()), twin=case.twin
        )
        slots: dict[str, str] = {}
        traced: list[dict] = []
        try:
            if case.observed:
                self.out("")
                self.out(f"--- {case.suite.action} / {case.test.name} on {case.role} ({self.position}) ---")
            await self.await_settled()
            await self.check_power()
            await self.steps(case.test.setup)
            slots = await self.slots()
            async with self.tap(slots) as traced:
                if case.observed:
                    await self.observed()
                else:
                    await self.steps(case.test.steps)
            try:
                await self.snapshot.await_reload()  # so the next case never lands mid-reload
            except Exception as err:  # noqa: BLE001 - a watcher's error fails the case, not the run
                raise StepFailure(f"case end: {err}") from None
        except StepFailure as err:
            result.outcome, result.detail = Outcome.FAIL, str(err)
        except _NoOneThere:
            result.outcome = Outcome.NOT_OBSERVED
        finally:
            # Before the restore, which may write the entities' stored attributes back through DeviceSide.
            if detail := await self.enable_entry():
                result.outcome, result.detail = Outcome.FAIL, result.detail or detail
            await self.snapshot.restore()
            if self.baseline is not None:
                await self.baseline.release()
        if traced and result.outcome in (Outcome.PASS, Outcome.FAIL):
            try:
                result.trace = await self.trace(slots, traced)
            except Exception as err:  # noqa: BLE001 - a lookup that fails costs the trace, not the case
                _LOGGER.warning("Could not read the devices behind %s, so it has no trace: %s", case.id, err)
        return result

    async def slots(self) -> dict[str, str]:
        """The slot of each entity the case's calls may reach: `target` for its role, the role's name for another,
        and `<slot>.member<n>` for a group's members, which DeviceSide traces apart when it writes them one by one."""
        slots = {}
        for role, entity_id in self.case.entities.items():
            slots[entity_id] = "target" if role == self.case.role else role
        for entity_id, slot in list(slots.items()):
            if self.is_group(entity_id):
                for number, member in enumerate(await self.members(entity_id), 1):
                    slots.setdefault(member, f"{slot}.member{number}")
        return slots

    @contextlib.asynccontextmanager
    async def tap(self, slots: dict[str, str]) -> AsyncIterator[list[dict]]:
        """The calls the steps send to the case's entities: DeviceSide's, from its wire trace, and the rig's own HA
        calls (`_set`), in order. Empty without the admin connection the trace comes on."""
        if self.wire is None:
            yield []
            return
        with self.wire.wire_tap(slots) as traced:
            self.traced = traced
            try:
                yield traced
            finally:
                # A failed drain loses only the calls still in flight, which a person may not have seen anyway.
                with contextlib.suppress(Exception):
                    await self.wire.drain_wire()
                self.traced = None

    async def trace(self, slots: dict[str, str], traced: list[dict]) -> Trace:
        seq, legend = sequence(
            (slots[call["entity_id"]], (call["transport"], call["command"], call["params"], call.get("options") or {}))
            for call in traced
        )
        makers = await self.makers() if self.makers is not None else {}
        devices = tuple(dict.fromkeys(makers[e] for e in slots if e in makers))
        unknown = any(None in options.values() for *_, options in legend.values())
        return Trace(starting_state(self.case), seq, legend, devices, unknown)

    async def enable_entry(self) -> str | None:
        """Enable the entry a step left disabled; why it couldn't be, or None. The journal keeps it until it is."""
        if self.entry is None or not self.entry.disabled:
            return None
        try:
            await self.entry.move("enable")
        except Exception as err:  # noqa: BLE001 - the case fails, and --recover tries again
            _LOGGER.warning("Could not enable DeviceSide's entry again: %s; --recover tries again", err)
            return f"could not enable DeviceSide's entry again: {err}"
        return None

    async def steps(self, steps: tuple[Step, ...], relaxed: bool = False) -> None:
        integration = self.case.suite.integration
        steps = tuple(step for step in steps if step.only is None or integration in step.only)
        for index, step in enumerate(steps):
            following = steps[index + 1].kind if index + 1 < len(steps) else None
            handler = getattr(self, f"_{step.kind}")
            if relaxed:
                handler = getattr(self, f"_relaxed_{step.kind}", handler)
            try:
                await handler(step.args, following)
            except StepFailure as err:
                raise StepFailure(f"{step.label}: {err}") from None
            except _NoOneThere:
                raise
            except Exception as err:  # noqa: BLE001 - any error ends the case as a fail, not the run
                raise StepFailure(f"{step.label}: {type(err).__name__}: {err}") from None

    async def observed(self) -> None:
        observe = self.case.test.observe
        assert observe is not None  # only a test with observe has an observed case
        await asyncio.sleep(SETUP_SETTLE_S)
        self.out(f"Lights: {await self.lights_by_name()}")
        self.out(f"You'll see: {_bold(observe.see)}")
        self.out(f"Watch for: {observe.watch}")
        await self.answer("Press Enter to start... ")
        try:
            await self.steps(self.case.test.steps, relaxed=True)
        except StepFailure as err:
            self.out(f"    nothing to observe: {err}")
            with contextlib.suppress(EOFError):
                await _prompt(self.ask, "Press Enter to go on... ")
            raise
        while (answer := (await self.answer(f"    {observe.ask} [Y/n] ")).lower()) not in ("", "y", "yes", "n", "no"):
            self.out("    Answer y or n.")
        if answer in ("n", "no"):
            try:
                saw = await self.answer("    What did you see? (Enter to skip) ")
            except _NoOneThere:
                saw = ""
            raise StepFailure(f"{NOT_SEEN}{observe.ask}" + (f"; saw: {saw}" if saw else ""))

    async def lights_by_name(self) -> str:
        """The case's lights by name, a group's members after it: the brief's step files can't name household lights."""

        async def name(entity_id: str) -> str:
            state = await self.ha.get_state(entity_id)
            return (state and state["attributes"].get("friendly_name")) or entity_id

        names = []
        for entity_id in self.case.entities.values():
            label = await name(entity_id)
            if self.is_group(entity_id):
                label += f" ({', '.join([await name(member) for member in await self.members(entity_id)])})"
            names.append(label)
        return "; ".join(names)

    async def answer(self, text: str) -> str:
        """The person's reply, stripped; EOF there means nobody is at the rig."""
        try:
            return (await _prompt(self.ask, text)).strip()
        except EOFError:
            raise _NoOneThere from None

    async def await_settled(self) -> None:
        """Wait out the predictions an earlier case's move or stop left on this case's lights."""
        lights = [member for entity_id in self.case.entities.values() for member in await self.members(entity_id)]
        wait = max((self.busy.get(light, 0.0) for light in lights), default=0.0) - asyncio.get_running_loop().time()
        if wait > 0:
            if self.case.observed:
                self.out(f"Waiting {wait:.0f} s for the last move's predictions to expire...")
            await asyncio.sleep(wait)

    async def check_power(self) -> None:
        """Fail up front if a target's supply is off: its state in HA is stale, so every step would mislead."""
        for entity_id in self.case.entities.values():
            if (supply := self.snapshot.power.get(entity_id)) is None:
                continue
            state = await self.ha.get_state(supply)
            if state is None or state["state"] != "on":
                found = state["state"] if state else "missing"
                raise StepFailure(f"power: {entity_id} draws power through {supply}, which is {found}; turn it on")

    async def _set(self, args: dict, _following: str | None) -> None:
        entity_id = self.entity(args)
        domain = entity_id.partition(".")[0]
        await self.snapshot.await_reload(entity_id)
        await self.touch(entity_id)
        data = {key: value for key, value in args.items() if key not in ("target", "state")}
        service = "turn_on" if domain == "scene" or args["state"] == "on" else "turn_off"
        if self.traced is not None:
            await self.wire.drain_wire()  # so the calls before it are in the tap first
            self.traced.append(
                {"entity_id": entity_id, "transport": "ha", "command": f"{domain}.{service}", "params": data}
            )
        await self.ha.call_service(domain, service, {"entity_id": entity_id, **data})
        if domain == "scene":
            return
        want: dict[str, Any] = {"state": args["state"]}
        if "brightness_pct" in data:
            # A light already on reads "on" at once; wait for the brightness, so the bridge state the next call
            # reads is the one just set. Hue rounds to its own 0-100 scale, so allow a step either side.
            brightness = round(data["brightness_pct"] * 255 / 100)
            want["brightness"] = {"min": brightness - 2, "max": brightness + 2}
        # A transition is the light's own fade, which the state follows only at its end.
        await self.await_state(entity_id, want, self.names["tol"] + data.get("transition", 0))

    async def z2m_ieee(self, entity_id: str) -> str:
        entry = await self.ha.command({"type": "config/entity_registry/get", "entity_id": entity_id})
        for device in await self.ha.command({"type": "config/device_registry/list"}):
            if device["id"] == entry.get("device_id") and (ieee := device_ieee(device, "zigbee2mqtt")):
                return ieee
        raise StepFailure(f"{entity_id} has no Zigbee2MQTT device")

    async def _z2m_option(self, args: dict, _following: str | None) -> None:
        if self.integration != "zigbee2mqtt":
            return
        ieee = await self.z2m_ieee(self.entity(args))

        def setting(name: str, value: Any) -> Callable[[Any], Awaitable[None]]:
            return lambda _original: self.ha.set_z2m_device_options(ieee, {name: value})

        if self.baseline is None:
            self.baseline = Baseline.of(self.ha, self.journal, f"case:{self.journal_key}", self.out)
        options = {name: value for name, value in args.items() if name != "target"}
        await self.baseline.hold(
            *[("z2m_option", f"{ieee}:{name}", setting(name, value)) for name, value in options.items()]
        )

    async def _expect_z2m_option(self, args: dict, _following: str | None) -> None:
        if self.integration != "zigbee2mqtt":
            return
        entity_id = self.entity(args)
        got = await self.ha.z2m_device_options(await self.z2m_ieee(entity_id))
        for name, want in args.items():
            if name != "target" and got.get(name) != want:
                raise StepFailure(f"{entity_id}: Zigbee2MQTT option {name} is {got.get(name)!r}, expected {want!r}")

    async def bridge_resource(self, entity_id: str) -> tuple[str, str]:
        """The CLIP v2 type and id behind a Hue entity. Core's hue integration keys each entity by its resource id:
        a light's by its light, a room or zone light's by its grouped_light, a scene's by its scene."""
        entry = await self.ha.command({"type": "config/entity_registry/get", "entity_id": entity_id})
        if entry.get("platform") != "hue":
            raise StepFailure(f"{entity_id} is not a Hue entity (platform {entry.get('platform')!r})")
        if not entry.get("unique_id"):
            raise StepFailure(f"{entity_id} has no unique_id to find on the bridge")
        domain = entity_id.partition(".")[0]
        if domain == "scene":
            return "scene", entry["unique_id"]
        return ("grouped_light" if self.is_group(entity_id) else "light"), entry["unique_id"]

    async def _bridge(self, args: dict, _following: str | None) -> None:
        if self.integration != "hue":
            raise StepFailure(f"bridge steps need a Hue suite, not {self.integration}")
        if self.bridge is None:
            raise StepFailure("no Hue bridge: set HUE_BRIDGE_HOST and HUE_APP_KEY in .env.rig")
        entity_id = self.entity(args)
        is_scene = entity_id.startswith("scene.")
        if "recall" in args and not is_scene:
            raise StepFailure(f"recall needs a scene target, not {entity_id}")
        if "put" in args and is_scene:
            raise StepFailure(f"put needs a light or group target, not {entity_id}")
        await self.touch(entity_id)
        resource_type, resource_id = await self.bridge_resource(entity_id)
        body = {"recall": {"action": "active"}} if "recall" in args else args["put"]
        try:
            await self.bridge.put(resource_type, resource_id, body)
        except BridgeError as err:
            raise StepFailure(str(err)) from None

    async def _call(self, args: dict, following: str | None) -> None:
        action = args.get("action", self.case.suite.action)
        entity_id = self.entity(args)
        await self.snapshot.await_reload(entity_id)
        await self.touch(entity_id)
        data: dict[str, Any] = {"entity_id": entity_id}
        for key, value in args.items():
            if key == "duration":
                seconds = self.names["duration"] = value.eval(self.names)
                data["duration"] = {"seconds": int(seconds) if seconds.is_integer() else seconds}
            elif key not in ("action", "target"):
                data[key] = value
        self.last_action, self.response, self.error, self.sent = action, None, None, data
        # ActionKind is a StrEnum: compared by value so the engine needs no integration import.
        self.query = getattr(getattr(self.registry, "actions", {}).get(action), "kind", None) == "query"
        sent_at = asyncio.get_running_loop().time()
        try:
            self.response = await self.ha.call_service("deviceside", action, data, return_response=True)
            self.call_times = (sent_at, asyncio.get_running_loop().time())
        except HAError as err:
            if following != "expect_error":
                raise StepFailure(f"deviceside.{action} raised {err.translation_key}: {err}") from None
            self.error = err
            return
        if following == "expect_error":
            raise StepFailure(f"deviceside.{action} succeeded where an error was expected: {self.response}")
        await self.mark_busy(action, data)
        self.mark_reloading(action)

    def mark_reloading(self, action: str) -> None:
        """After a write by an ENTRY_RELOADS action, watch for the entry reload on each light it wrote to: the next
        call or set on it, the restore and the case's end wait it out (Snapshot.watch_reload). Checks don't."""
        if (reload := ENTRY_RELOADS.get(self.integration)) is None or action not in reload.actions:
            return
        for entity_id, row in (self.response or {}).items():
            if isinstance(row, dict) and row.get("status") in _WRITES:
                self.snapshot.watch_reload(entity_id)

    async def mark_busy(self, action: str, data: dict) -> None:
        """After a raise, lower or stop, hold its lights until the predictions it left expire (see await_settled)."""
        if not self.settle or action not in _PREDICTED:
            return
        sweep = data.get("sweep_time", SWEEP_TIME_S) if action in _RAMPS else 0.0
        until = asyncio.get_running_loop().time() + sweep + self.settle
        for member in await self.members(data["entity_id"]):
            self.busy[member] = max(self.busy.get(member, 0.0), until)

    def expected(self, name: str, want: Any) -> Any:
        if name == "status" and want == "expected":
            if (adapter := self.registry.adapter(self.last_action, self.integration)) is None:
                raise StepFailure(f"no {self.last_action} adapter on {self.integration} to expect a status from")
            return "confirmed" if adapter.scope.confirmable else "sent"
        if name == "integration" and want == "self":
            return self.integration
        return want

    async def _expect_response(self, args: dict, _following: str | None) -> None:
        if self.response is None:
            raise StepFailure("no response to check: no call has returned one")
        entity_id = self.entity(args)
        keys = [entity_id] if entity_id in self.response else await self.members(entity_id)
        expect, attributes = await self.per_light(args, keys, entity_id)
        for key in keys:
            if not isinstance(entry := self.response.get(key), dict):
                raise StepFailure(f"the response has no entry for {key}: {self.response}")
            for name, want in expect[key].items():
                if not _matches(entry.get(name), expected := self.expected(name, want)):
                    raise StepFailure(f"{key}: {name} is {entry.get(name)!r}, expected {expected!r}: {entry}")
            if args["attributes"]:
                if not self.query:
                    raise StepFailure(f"attributes are checked in a query's response, not {self.last_action}'s")
                want, got = attributes[key], entry
                if bad := [attr for attr, value in want.items() if not _matches(_lookup(got, attr), value)]:
                    raise StepFailure(f"{key}: attributes {bad} are {got}, expected {want}")

    async def per_light(self, args: dict, keys: list[str], entity_id: str) -> tuple[dict, dict]:
        """For each response row, the fields and the attributes expect_response checks, shaped by the light's color
        modes: an attribute it can't carry wanted absent, and `detail: fields_dropped` after a call that sent it a
        field it can't take (color temperature to a White bulb), unless the step names a detail. A group's own row
        (Z2M) is the mean over its members that report: it takes everything."""
        fields = {name: want for name, want in args.items() if name not in ("target", "attributes")}
        expect = {key: dict(fields) for key in keys}
        attributes = {key: args.get("attributes", {}) for key in keys}
        if self.is_group(keys[0]):
            return expect, attributes
        states = dict(zip(keys, await asyncio.gather(*map(self.ha.get_state, keys)), strict=True))
        if "attributes" in args:
            attributes = _by_modes(attributes, states, entity_id)
        sent = _LIGHT_FIELDS & set(self.sent)
        for key in keys if "detail" not in args and not self.query else ():
            if any(not _carries(states[key], name) for name in sent):
                expect[key]["detail"] = "fields_dropped"
        return expect, attributes

    async def _expect_error(self, args: dict, _following: str | None) -> None:
        if self.error is None:
            raise StepFailure("no error to check: the last call did not raise")
        err = self.error
        if "key" in args and err.translation_key != args["key"]:
            raise StepFailure(f"error key is {err.translation_key!r}, expected {args['key']!r}: {err}")
        if "summary" in args:
            expected = f"{self.entity({})}: {args['summary']}"
            if (summary := err.translation_placeholders.get("summary")) != expected:
                raise StepFailure(f"error summary is {summary!r}, expected {expected!r}")

    async def _expect_state(self, args: dict, _following: str | None) -> None:
        want = {name: value for name, value in args.items() if name not in ("target", "within")}
        within = args["within"].eval(self.names) if "within" in args else None
        await self.await_state(self.entity(args), want, within)

    async def await_state(self, entity_id: str, want: dict, within: float | None) -> None:
        """Every member of the target matches `want`, in what its color modes carry, now or within `within` seconds
        shared between them."""
        deadline = asyncio.get_running_loop().time() + (within or 0)
        members = await self.members(entity_id)
        states = dict(zip(members, await asyncio.gather(*map(self.ha.get_state, members)), strict=True))
        wants = _by_modes(dict.fromkeys(members, want), states, entity_id)
        for member in members:
            member_want = dict(wants[member])
            if member_want.get("state") == "unchanged":
                before = self.snapshot.taken.get(member)
                member_want["state"] = before["state"] if before else None

            def holds(state: dict | None, member_want: dict = member_want) -> bool:
                return state is not None and all(_matches(_value(state, n), v) for n, v in member_want.items())

            if within is None:
                if not holds(state := states[member]):
                    raise StepFailure(f"{member}: expected {member_want}, got {_describe(state, member_want)}")
                continue
            remaining = max(deadline - asyncio.get_running_loop().time(), 0)
            try:
                await self.ha.wait_for(member, holds, remaining)
            except TimeoutError:
                state = await self.ha.get_state(member)
                raise StepFailure(
                    f"{member}: expected {member_want} within {within:g} s, got {_describe(state, member_want)}"
                ) from None

    async def _expect_matches_read(self, args: dict, _following: str | None) -> None:
        """Each attribute (dotted: `xy_color.0`) in HA's state of every member is within `tolerance` of a live
        get_attributes read, now or within `within` seconds shared between them."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + args["within"].eval(self.names)
        for member in await self.members(self.entity(args)):
            while True:
                state = await self.ha.get_state(member)
                response = await self.ha.call_service(
                    "deviceside", "get_attributes", {"entity_id": member}, return_response=True
                )
                shown = {name: _lookup((state or {}).get("attributes", {}), name) for name in args["attributes"]}
                read = {name: _lookup((response or {}).get(member) or {}, name) for name in args["attributes"]}
                if all(
                    isinstance(shown[name], int | float)
                    and isinstance(read[name], int | float)
                    and abs(shown[name] - read[name]) <= args["tolerance"]
                    for name in args["attributes"]
                ):
                    break
                if loop.time() >= deadline:
                    raise StepFailure(f"{member}: HA shows {shown}, a live read gives {read}")
                await asyncio.sleep(0.5)

    async def bulb_hue(self, light: str) -> float:
        """The light's hue on its own wheel, in degrees. ZHA's comes live off the bulb (EnhancedCurrentHue, else
        CurrentHue), since HA's is worked out from the bulb's xy and departs from the bulb's wheel by 25° and more in
        the greens; elsewhere it is HA's hs_color, which Zigbee2MQTT publishes from the bulb's own hue."""
        if self.integration != "zha":
            state = await self.ha.get_state(light)
            hue = _lookup((state or {}).get("attributes", {}), "hs_color.0")
            if not isinstance(hue, int | float):
                raise StepFailure(f"{light}: HA shows no hs_color to take its hue from")
            return float(hue)
        entry = await self.ha.command({"type": "config/entity_registry/get", "entity_id": light})
        ieee, _, endpoint = (entry.get("unique_id") or "").partition("-")
        if not endpoint.isdigit():
            raise StepFailure(f"{light} has no ZHA address in its unique_id {entry.get('unique_id')!r}")
        for attribute, steps in ((_ENHANCED_CURRENT_HUE, 65535), (_CURRENT_HUE, 254)):
            raw = await self.ha.zha_read_attribute(ieee, int(endpoint), _COLOR_CLUSTER, attribute)
            if raw is not None:
                return raw * 360 / steps
        raise StepFailure(f"{light}: the bulb returned no hue")

    async def _mark_hue(self, args: dict, _following: str | None) -> None:
        for light in await self.members(self.entity(args)):
            self.hue_marks[light] = await self.bulb_hue(light)

    async def _expect_hue_travel(self, args: dict, _following: str | None) -> None:
        """Each light has turned `by` degrees (negative: down the wheel) round its own wheel since the last mark_hue,
        give or take `tolerance`; the shorter way round, so a travel is under 180°."""
        for light in await self.members(self.entity(args)):
            if (start := self.hue_marks.get(light)) is None:
                raise StepFailure(f"{light}: no mark_hue before expect_hue_travel")
            now = await self.bulb_hue(light)
            travel = (now - start + 180) % 360 - 180
            if abs(travel - args["by"]) > args["tolerance"]:
                raise StepFailure(
                    f"{light}: hue went {start:.1f}° to {now:.1f}°, {travel:+.1f}°; expected {args['by']:+g}° "
                    f"± {args['tolerance']:g}°"
                )

    async def _expect_no_reload(self, args: dict, _following: str | None) -> None:
        entity_id, seconds = self.entity(args), args["for"].eval(self.names)
        try:
            state = await self.ha.wait_for(entity_id, _unavailable, seconds)
        except TimeoutError:
            return
        found = state["state"] if state else "gone"
        raise StepFailure(f"{entity_id}: went {found} within {seconds:g} s, an entry reload")

    async def _wait(self, seconds: Expr, _following: str | None) -> None:
        await asyncio.sleep(max(seconds.eval(self.names), 0))

    async def _sample(self, args: dict, _following: str | None) -> None:
        """Poll an attribute (dotted, as _lookup reads one) for `for` seconds; an absent attribute (a light that is
        off) reads as 0, but a sample in which it was never read fails, else an absent attribute would pass `steady`.

        `source: deviceside` reads deviceside.get_attributes instead of HA's state, in its units (brightness in
        percent): mid-move it predicts where the light is, where HA shows a Hue fade's end value at once.
        """
        entity_id, attribute, source = self.entity(args), args["attribute"], args.get("source", "ha")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + args["for"].eval(self.names)
        every = max(args["every"].eval(self.names), 0.05)
        readings: list[float] = []
        read = False  # any reading was not None
        offsets: list[float] = []  # each reading's round-trip middle, after the last call was sent (else the start)
        since = self.call_times[0] if self.call_times else loop.time()
        while True:
            before = loop.time()
            value = await self.reading(entity_id, attribute, source)
            offsets.append((before + loop.time()) / 2 - since)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int | float)):
                raise StepFailure(f"{entity_id}: {attribute} is {value!r}, not a number")
            read = read or value is not None
            readings.append(float(value or 0))
            if loop.time() >= deadline:
                break
            await asyncio.sleep(min(every, max(deadline - loop.time(), 0)))
        if not read:
            raise StepFailure(f"{entity_id}: {attribute} was never read")
        shown = _timed(readings, offsets, self.call_times)
        if "min" in args and min(readings) < args["min"]:
            raise StepFailure(f"{entity_id}: {attribute} fell below {args['min']}: {shown}")
        if "max" in args and max(readings) > args["max"]:
            raise StepFailure(f"{entity_id}: {attribute} rose above {args['max']}: {shown}")
        pairs = list(zip(readings, readings[1:], strict=False))
        match args.get("expect", "rising"):
            case "rising" if not (all(b >= a for a, b in pairs) and readings[-1] > readings[0]):
                raise StepFailure(f"{entity_id}: {attribute} did not rise steadily: {shown}")
            case "falling" if not (all(b <= a for a, b in pairs) and readings[-1] < readings[0]):
                raise StepFailure(f"{entity_id}: {attribute} did not fall steadily: {shown}")
            case "steady" if max(readings) - min(readings) > STEADY_SPREAD.get((attribute, source), 0):
                raise StepFailure(f"{entity_id}: {attribute} moved: {shown}")

    async def reading(self, entity_id: str, attribute: str, source: str) -> Any:
        if source == "ha":
            state = await self.ha.get_state(entity_id)
            return _lookup(state["attributes"], attribute) if state else None
        response = (
            await self.ha.call_service("deviceside", "get_attributes", {"entity_id": entity_id}, return_response=True)
            or {}
        )
        if entity_id in response:
            return _lookup(response[entity_id] or {}, attribute)
        # A group expanded into its members (ZHA) answers per member: the mean of those that have the attribute, as HA
        # averages a group's lit lights.
        values = [
            value for read in response.values() if isinstance(value := _lookup(read or {}, attribute), int | float)
        ]
        return sum(values) / len(values) if values else None

    async def _do(self, instruction: str, _following: str | None) -> None:
        await self.answer(f"    {instruction} Press Enter when done... ")

    async def _entry(self, how: str, _following: str | None) -> None:
        if self.entry is None:
            raise StepFailure("entry steps need HA_ADMIN_TOKEN in .env.rig")
        if how == "disable":
            # Snapshot the target first: an off light's stored attributes are read through DeviceSide.
            await self.touch(self.entity({}))
        await self.entry.move(how)

    async def _restore(self, _args: None, _following: str | None) -> None:
        await self.snapshot.restore(clear=False)

    # Observed mode: a check becomes the wait it implies, and never fails.

    async def _relaxed_expect_response(self, _args: dict, _following: str | None) -> None:
        pass

    _relaxed_expect_error = _relaxed_expect_response
    _relaxed_expect_hue_travel = _relaxed_expect_response
    _relaxed_expect_z2m_option = _relaxed_expect_response

    async def _relaxed_expect_state(self, args: dict, following: str | None) -> None:
        if "within" in args:
            with contextlib.suppress(StepFailure):
                await self._expect_state(args, following)

    async def _relaxed_expect_matches_read(self, args: dict, _following: str | None) -> None:
        await asyncio.sleep(max(args["within"].eval(self.names), 0))

    async def _relaxed_sample(self, args: dict, _following: str | None) -> None:
        await asyncio.sleep(max(args["for"].eval(self.names), 0))

    _relaxed_expect_no_reload = _relaxed_sample


async def run(
    suites: list[Suite],
    profile: Profile,
    registry: Any,
    ha: HAClient,
    *,
    admin: HAClient | None = None,
    keyword: str | None = None,
    ask: Callable[[str], str] | None = input,
    tolerance: dict[str, float] = TOLERANCE_S,
    settle: dict[str, float] = SETTLE_S,
    out: Callable[[str], None] = print,
    bridge: HueBridge | None = None,
    journal: Journal | None = None,
    only: Callable[[Case], bool] | None = None,
    lanes: bool = False,
    busy: dict[str, float] | None = None,
    reloads: bool = True,
    done: dict[tuple[str, str, str], CaseResult] | None = None,
    on_result: Callable[[Suite, CaseResult], None] | None = None,
    on_facts: Callable[[Suite, str | None, list[Device]], None] | None = None,
    budget: Budget | None = None,
    observations: dict[str, list[Observation]] | None = None,
    reobserve: bool = False,
) -> RunResult:
    """Run every case and print the table; ADMIN_INTEGRATIONS run on `admin`. `ask=None` is --unattended.

    Every automated case runs first, with nobody needed. Then each observed case runs, unless its automated twin
    failed; unattended, they are reported not observed instead. `only` keeps the cases it accepts, leaving each suite
    incomplete (see merge). With `lanes`, each integration's automated cases run as one lane, the lanes side by side.
    `busy` (_CaseRun.busy) carries the predictions an earlier run's cases left, for a run that follows straight on.
    `reloads` False refuses to save DeviceSide's options (EntryOptions): the run holds HA shared, with other runs.

    Resuming, `done` holds an earlier session's results by (integration, suite name, case id): those cases are left
    out, as `only` leaves cases out, and an observed case whose saved twin failed is still skipped. `on_result` gets
    each case's result as it finishes (a cancelled case has none), `on_facts` each part's facts, taken even when a
    signal or the `budget` stops the part. Once the budget is spent no case starts; the rest get no result.

    With `admin` subscribed to DeviceSide's wire trace (HAClient.subscribe_wire), each case's result carries the calls
    its steps sent (CaseResult.trace). An observed case is then lent its pass, not run, when a pass in `observations`
    (each integration's _observed.md rows) covers its twin's calls, unless `reobserve`. Each observed case's rows go
    into `observations` as it ends, so a run passing the same dict to the next sees them, and a lend that a fail seen
    later in this run blocks is taken back and the case run (_Run.unlend).
    """
    only = unfinished(done, only)
    planned = _plan(suites, profile, registry, ha, admin, keyword, only, out)
    options = {
        id(suite): {name: name in suite.file.options for name in adapter.scope.options}
        for suite, adapter, *_ in planned
    }
    planned = by_options(planned, options)
    rig_run = _Run(
        registry,
        tolerance,
        settle,
        profile.power(),
        ask,
        out,
        options,
        EntryOptions(admin, journal=journal, reloads=reloads) if admin else None,
        bridge=bridge,
        journal=journal,
        lanes=lanes,
        on_result=on_result,
        budget=budget,
        wire=admin,
        observations={} if observations is None else observations,
        reobserve=reobserve,
    )
    if busy is not None:
        rig_run.busy = busy
    rig_run.seed([suite for suite, *_ in planned], done or {})
    suite_results: list[SuiteResult] = []
    try:
        waiting = await rig_run.all_automated([(suite, client, cases_) for suite, _, client, cases_ in planned])
        await rig_run.observed(waiting)
        await rig_run.unlend()
    finally:
        # A failure here must not hide the one that ended the run.
        if rig_run.entry_options is not None:
            try:
                await rig_run.entry_options.restore()
            except Exception as err:  # noqa: BLE001
                out(f"Could not put DeviceSide's options back; the next run tries again: {err}")
        # The cases that finished, and their facts, taken now while the bulbs are on this part's network, even when a
        # signal or the budget stopped the part.
        complete = not keyword and only is None
        suite_results = await _finished(rig_run, [(s, c) for s, _, _, c in planned], ha, out, complete, on_facts)
    for suite_result in suite_results:
        out("")
        out(f"{suite_result.suite.integration} / {suite_result.suite.name}")
        out("\n".join(table(suite_result, details=True)))
    return RunResult(suite_results)


async def _finished(
    rig_run: _Run,
    planned: list[tuple[Suite, list[Case]]],
    ha: HAClient,
    out: Callable[[str], None],
    complete: bool,
    on_facts: Callable[[Suite, str | None, list[Device]], None] | None,
) -> list[SuiteResult]:
    """Each planned suite's finished cases, with their facts, handed to `on_facts`. Every part, recordable or not: a
    lanes part is incomplete until merge joins it to the phases part. Saving the facts must not hide the error that
    ended the run, so its failure is printed."""
    parts = []
    for suite, suite_cases in planned:
        if not (finished := [result for case in suite_cases if (result := rig_run.results.get((id(suite), case.id)))]):
            continue
        part = SuiteResult(suite, finished, complete=complete)
        parts.append(part)
        await _capture_facts(part, ha, out)
        if on_facts is not None:
            try:
                on_facts(suite, part.ha_version, part.devices)
            except Exception as err:  # noqa: BLE001
                out(f"Could not save the facts of {suite.name}: {err}")
    return parts


async def _capture_facts(suite_result: SuiteResult, ha: HAClient, out: Callable[[str], None]) -> None:
    """Store HA's version and the suite's devices on its result; a failed lookup costs the record, not the run."""
    entity_ids = [entity_id for case in suite_result.cases for entity_id in case.entity_ids]
    try:
        suite_result.ha_version, suite_result.devices = await rig_facts(ha, entity_ids)
    except Exception as err:  # noqa: BLE001
        out(f"Could not read the devices behind {suite_result.suite.name}, so it writes no record: {err}")


def by_options[T: tuple](planned: list[T], options: dict[int, dict[str, bool]]) -> list[T]:
    """The planned suites, those that pin the same DeviceSide options side by side, in the order each set first
    appears, and in planned order within it: each save reloads the entry, so a run pins each set once rather than
    once per file (`move` then `move-predicted_state`, then the next action's pair). A suite pinning none stays put."""
    order: dict[tuple, int] = {}
    keys = []
    for index, entry in enumerate(planned):
        wanted = tuple(sorted(options.get(id(entry[0]), {}).items()))
        keys.append((order.setdefault(wanted, index), index) if wanted else (index, index))
    return [entry for _, entry in sorted(zip(keys, planned, strict=True), key=lambda pair: pair[0])]


def unfinished(
    done: dict[tuple[str, str, str], CaseResult] | None, only: Callable[[Case], bool] | None = None
) -> Callable[[Case], bool] | None:
    """`only`, narrowed to the cases `done` has no result for; `only` itself when nothing is done."""
    if not done:
        return only
    return lambda case: (case.suite.integration, case.suite.name, case.id) not in done and (only is None or only(case))


def _plan(
    suites: list[Suite],
    profile: Profile,
    registry: Any,
    ha: HAClient,
    admin: HAClient | None,
    keyword: str | None,
    only: Callable[[Case], bool] | None,
    out: Callable[[str], None],
) -> list[tuple[Suite, Any, HAClient, list[Case]]]:
    """Each suite that can run, with its adapter, its client and its cases; says why each other one is skipped. A
    suite with no case `only` accepts is left out unsaid: another part of the run has its cases."""
    planned = []
    for suite in suites:
        suite_cases = cases(suite, profile, keyword, adapters_for(suite, registry))
        if only is not None and not (suite_cases := [case for case in suite_cases if only(case)]):
            continue
        adapter = registry.adapter(suite.action, suite.integration)
        if adapter is None:
            out(f"{suite.integration} / {suite.action}: no adapter, skipped")
            continue
        if not applies(suite, registry):
            # Another integration's option file, such as Hue's predicted_state: selection drops it silently.
            continue
        client = admin if suite.integration in ADMIN_INTEGRATIONS else ha
        if client is None or ((adapter.scope.options or suite.file.moves_entry) and admin is None):
            out(f"{suite.integration} / {suite.action}: needs HA_ADMIN_TOKEN, skipped")
            continue
        planned.append((suite, adapter, client, suite_cases))
    return planned


@dataclass(slots=True)
class _Run:
    """The cases of one rig run, and their results by suite (by identity: a Suite holds dicts) and case id."""

    registry: Any
    tolerance: dict[str, float]
    settle: dict[str, float]
    power: dict[str, str]
    ask: Callable[[str], str] | None
    out: Callable[[str], None]
    # Each suite's options by its id, set before each of its cases.
    options: dict[int, dict[str, bool]] = field(default_factory=dict)
    entry_options: EntryOptions | None = None
    results: dict[tuple[int, str], CaseResult] = field(default_factory=dict)
    busy: dict[str, float] = field(default_factory=dict)  # _CaseRun.busy
    bridge: HueBridge | None = None
    journal: Journal | None = None
    lanes: bool = False  # whether automated cases run in lanes: their lines then name their suite
    on_result: Callable[[Suite, CaseResult], None] | None = None
    budget: Budget | None = None
    wire: HAClient | None = None  # the admin connection DeviceSide's wire trace comes on
    observations: dict[str, list[Observation]] = field(default_factory=dict)  # each integration's _observed.md rows
    reobserve: bool = False  # run observed cases even where a pass covers their calls
    lends: list[tuple[Case, HAClient]] = field(default_factory=list)  # the cases this run lent a pass
    taken_back: list[tuple[Case, HAClient]] = field(default_factory=list)  # lends a fail since blocks, for unlend
    _makers: dict[str, Maker] | None = None
    _makers_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def makers(self) -> dict[str, Maker]:
        """The device behind each entity, read once a run: a roaming run calls run() once per network."""
        async with self._makers_lock:
            if self._makers is None:
                self._makers = await entity_makers(self.wire)
            return self._makers

    def lent(self, case: Case, twin: CaseResult | None) -> CaseResult | None:
        """An observed case's pass, not run, when an _observed.md pass covers the calls its twin just sent; else None.
        A twin with no trace (no admin connection, or a resumed result saved before traces) lends nothing."""
        if self.reobserve or twin is None or twin.outcome is not Outcome.PASS or twin.trace is None:
            return None
        trace = twin.trace
        row = trace.lent_by(self.observations.get(case.suite.integration, ()))
        if row is None:
            return None
        result = _not_run(case, Outcome.PASS, f"its calls passed when observed on {row.date}")
        result.trace, result.lent = trace, row.date
        return result

    def seed(self, suites: list[Suite], done: dict[tuple[str, str, str], CaseResult]) -> None:
        """An earlier session's results, under this run's suites, for the twin lookup in `automated`."""
        for suite in suites:
            for (integration, name, case_id), result in done.items():
                if (integration, name) == (suite.integration, suite.name):
                    self.results[id(suite), case_id] = result

    def case_run(self, case: Case, client: HAClient, ask: Callable[[str], str] | None, position: str = "") -> _CaseRun:
        integration = case.suite.integration
        # An observed case runs alone, after the lanes.
        journal_key = integration if self.lanes and not case.observed else SERIAL
        return _CaseRun(
            case,
            client,
            self.registry,
            self.tolerance[integration],
            ask,
            self.power,
            self.out,
            position,
            self.busy,
            self.settle.get(integration, 0.0),
            self.bridge,
            self.journal,
            journal_key,
            self.entry_options,
            self.wire,
            self.makers,
        )

    async def run_case(
        self, case: Case, client: HAClient, ask: Callable[[str], str] | None, position: str = ""
    ) -> CaseResult:
        if wanted := self.options.get(id(case.suite)):
            await self.entry_options.apply(wanted)
        return await self.case_run(case, client, ask, position).run()

    def report(self, case: Case, result: CaseResult) -> None:
        self.results[id(case.suite), case.id] = result
        if (seen := observed_by(result, datetime.date.today().isoformat())) is not None:
            rows = self.observations.setdefault(case.suite.integration, [])
            for row in seen[0]:
                merge_row(rows, row)
        if self.on_result is not None:
            self.on_result(case.suite, result)
        # Lanes interleave their lines, so each names its suite.
        prefix = f"{case.suite.integration} / {case.suite.name}  " if self.lanes else "  "
        self.out(f"{prefix}{case.id}: {result.outcome}" + (f" ({result.detail})" if result.detail else ""))
        if seen is not None and result.outcome is Outcome.FAIL:
            self.take_back()

    def take_back(self) -> None:
        """Report not observed each case this run lent a pass that a fail seen since blocks, at once, so its pass is
        never left saved however the run ends; unlend runs it."""
        for case, client in self.lends:
            if self.lent(case, self.results.get((id(case.suite), case.twin or ""))) is None:
                self.taken_back.append((case, client))
        gone = {(id(case.suite), case.id) for case, _ in self.taken_back}
        self.lends = [(case, client) for case, client in self.lends if (id(case.suite), case.id) not in gone]
        for case, _ in self.taken_back:
            if self.results[id(case.suite), case.id].lent:
                self.report(case, _not_run(case, Outcome.NOT_OBSERVED, TAKEN_BACK))

    async def all_automated(self, planned: list[tuple[Suite, HAClient, list[Case]]]) -> list[tuple[Case, HAClient]]:
        """Run the planned suites' automated cases, in lanes or one suite after another; return the observed cases
        still to run."""
        if self.lanes:
            return await self.in_lanes(planned)
        return [case for suite, client, cases_ in planned for case in await self.automated(suite, client, cases_)]

    async def in_lanes(self, planned: list[tuple[Suite, HAClient, list[Case]]]) -> list[tuple[Case, HAClient]]:
        """Run each integration's suites' automated cases as one lane, the lanes side by side, after setting the
        options they share once: a save reloads the entry, which would cut off the other lanes' fades. Returns the
        observed cases still to run. An error other than a case's failure cancels every lane, each cancelled case
        putting its entities back, and is raised as itself."""
        wanted = [options for suite, _, _ in planned if (options := self.options.get(id(suite)))]
        if any(options != wanted[0] for options in wanted):
            raise RuntimeError(f"lanes need the same DeviceSide options, not {wanted}")
        if wanted:
            await self.entry_options.apply(wanted[0])
        by_integration: dict[str, list[tuple[Suite, HAClient, list[Case]]]] = {}
        for entry in planned:
            by_integration.setdefault(entry[0].integration, []).append(entry)

        async def lane(entries: list[tuple[Suite, HAClient, list[Case]]]) -> list[tuple[Case, HAClient]]:
            return [case for suite, client, cases_ in entries for case in await self.automated(suite, client, cases_)]

        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(lane(entries)) for entries in by_integration.values()]
        except ExceptionGroup as err:
            raise err.exceptions[0] from None
        return [case for task in tasks for case in task.result()]

    async def automated(self, suite: Suite, client: HAClient, suite_cases: list[Case]) -> list[tuple[Case, HAClient]]:
        """Run the suite's automated cases; return the observed ones still to run, with their client."""
        if not self.lanes:
            self.out(f"{suite.integration} / {suite.name}")
        waiting = []
        for case in suite_cases:
            twin = self.results.get((id(suite), case.twin or ""))
            if case.skip:
                self.report(case, _not_run(case, Outcome.SKIPPED, case.skip))
            elif not case.observed:
                if self.budget is not None and self.budget.stops(device_case=True):
                    continue
                self.report(case, await self.run_case(case, client, None))
            elif (lent := self.lent(case, twin)) is not None:
                self.lends.append((case, client))
                self.report(case, lent)
            elif self.ask is None:
                self.report(case, _not_run(case, Outcome.NOT_OBSERVED))
            elif twin is not None and twin.outcome is Outcome.FAIL:
                self.report(case, _not_run(case, Outcome.SKIPPED, "automated case failed"))
            else:
                waiting.append((case, client))
        return waiting

    async def observed(self, waiting: list[tuple[Case, HAClient]]) -> None:
        """Run each observed case in turn; after an EOF at a prompt, the rest are not observed."""
        if self.budget is not None and waiting and self.budget.spent and not self.budget.granted:
            self.budget.stopped = True
            return
        if waiting:
            self.out("")
            self.out(f"{len(waiting)} test{'s need' if len(waiting) > 1 else ' needs'} you")
        for number, (case, client) in enumerate(waiting, 1):
            if self.ask is None:
                self.report(case, _not_run(case, Outcome.NOT_OBSERVED))
                continue
            if self.budget is not None and self.budget.stops(device_case=True):
                return
            result = await self.run_case(case, client, self.ask, f"{number} of {len(waiting)}")
            self.report(case, result)
            if result.outcome is Outcome.NOT_OBSERVED:
                self.ask = None

    async def unlend(self) -> None:
        """Run the observed cases whose lends take_back took, while someone is there; a run of one can fail and take
        back another, so it goes round again. One left unrun (unattended, or the budget spent) stays not observed."""
        while self.taken_back and self.ask is not None:
            taken, self.taken_back = self.taken_back, []
            await self.observed(taken)


def _not_run(case: Case, outcome: Outcome, detail: str = "") -> CaseResult:
    domain = case.entities[case.role].partition(".")[0] if case.entities else None
    return CaseResult(
        case.id, case.role, domain, outcome, detail, twin=case.twin, unreachable=case.unreachable, absent=case.absent
    )


def merge(parts: Iterable[SuiteResult], profile: Profile, registry: Any) -> RunResult:
    """One result per suite from its parts, which runs split between them (the lanes, then the phases, or runs), in
    the order the parts first name each suite, its cases in file order. A suite is complete only when every case it
    plans has a result, none of them an unreachable skip: lanes whose phases never ran write no record, and neither
    does a run that left a case out (-k) until another runs it."""
    joined: dict[tuple[str, str], SuiteResult] = {}
    for part in parts:
        key = (part.suite.integration, part.suite.name)
        if key in joined:
            joined[key].cases += part.cases
            joined[key].devices += [device for device in part.devices if device not in joined[key].devices]
            joined[key].ha_version = joined[key].ha_version or part.ha_version
        else:
            joined[key] = SuiteResult(
                part.suite, list(part.cases), complete=False, ha_version=part.ha_version, devices=list(part.devices)
            )
    for result in joined.values():
        planned = [case.id for case in cases(result.suite, profile, None, adapters_for(result.suite, registry))]
        position = {case_id: index for index, case_id in enumerate(planned)}
        result.cases.sort(key=lambda case: position.get(case.id, len(position)))
        result.planned = len(planned)
        result.complete = set(planned) <= {case.id for case in result.cases if not case.unreachable}
    return RunResult(list(joined.values()))


# --- Test records --------------------------------------------------------------------------------------------


def table(suite_result: SuiteResult, details: bool = False) -> list[str]:
    """The pass/fail table, by role and domain: a record is public, so it never names a household entity."""
    lines = ["| Test | Target | Result |", "|---|---|---|"]
    for case in suite_result.cases:
        target = f"`{case.domain}` ({case.role})" if case.domain else f"{case.role}: none in the profile"
        outcome = str(case.outcome) + (f": {case.detail}" if details and case.detail else "")
        if case.outcome is Outcome.PASS and _firmware_fact(suite_result.suite, case.id):
            outcome += FIRMWARE_FACT
        if case.outcome is Outcome.PASS and case.lent:
            outcome = f"{Outcome.PASS} (observed {case.lent})"
        lines.append(f"| `{case.id}` | {target} | {outcome} |")
    return lines


def _firmware_fact(suite: Suite, case_id: str) -> bool:
    return (test := suite.test(case_id)) is not None and test.firmware_fact


@dataclass(frozen=True, slots=True)
class Device:
    model: str | None
    model_id: str | None
    firmware: str | None

    def __str__(self) -> str:
        model_id = f" (`{self.model_id}`)" if self.model_id and self.model_id != self.model else ""
        return f"{self.model or 'unknown model'}{model_id}, firmware `{self.firmware or 'unknown'}`"


async def entity_makers(ha: HAClient) -> dict[str, Maker]:
    """The maker, model and firmware of the device behind each entity whose device names a maker and model."""
    listed, registered = await asyncio.gather(
        ha.command({"type": "config/entity_registry/list_for_display"}),
        ha.command({"type": "config/device_registry/list"}),
    )
    devices = {device["id"]: device for device in registered}
    found = {}
    for entry in listed["entities"]:
        if (device := devices.get(entry.get("di"))) is None:
            continue
        # A device that names no maker or model can't lend to, or take a block from, another unnamed one.
        if (maker := device.get("manufacturer")) and (model := device.get("model_id") or device.get("model")):
            found[entry["ei"]] = Maker(maker, model, device.get("sw_version") or "unknown")
    return found


async def rig_facts(ha: HAClient, entity_ids: Iterable[str]) -> tuple[str, list[Device]]:
    """HA's version and the devices behind the entities, via registry calls a non-admin token may make."""
    config, listed, registered = await asyncio.gather(
        ha.command({"type": "get_config"}),
        ha.command({"type": "config/entity_registry/list_for_display"}),
        ha.command({"type": "config/device_registry/list"}),
    )
    device_ids = {entry["ei"]: entry.get("di") for entry in listed["entities"]}
    devices = {device["id"]: device for device in registered}
    found: dict[Device, None] = {}
    for entity_id in entity_ids:
        if (device := devices.get(device_ids.get(entity_id))) is not None:
            found[Device(device.get("model"), device.get("model_id"), device.get("sw_version"))] = None
    return config["version"], list(found)


CITED_COMMIT = re.compile(r"^\| Source \|.*\bcommit `([0-9a-f]{7,40})`", re.MULTILINE)  # check_release's
DATE = re.compile(r"^\| Date \| (\d{4}-\d{2}-\d{2}) \|", re.MULTILINE)
FIRMWARE_FACT = " (firmware fact)"  # after a firmware fact's PASS in a record's Tests table
CASE_ROW = re.compile(r"^\| `([^`]+)` \|.*\| ([^|]*?) \|$", re.MULTILINE)  # id, then the result column
LENT = re.compile(r" \(observed \d{4}-\d{2}-\d{2}\)$")  # after a lent observed pass in a record's Tests table


@dataclass(frozen=True, slots=True)
class Record:
    """What the rig reads from a test record: the commit it cites, its date, the case IDs in its Tests table and
    which of them passed or were skipped (a record writes no skip detail)."""

    path: Path
    commit: str | None
    date: str | None
    case_ids: frozenset[str]
    passed: frozenset[str] = frozenset()
    skipped: frozenset[str] = frozenset()

    @classmethod
    def read(cls, path: Path) -> Record | None:
        if not path.is_file():
            return None
        text = path.read_text()
        cited, date = CITED_COMMIT.search(text), DATE.search(text)
        _, _, tests = text.partition("## Tests")
        rows = [
            (case_id, LENT.sub("", result.removesuffix(FIRMWARE_FACT))) for case_id, result in CASE_ROW.findall(tests)
        ]
        return cls(
            path,
            cited and cited[1],
            date and date[1],
            frozenset(case_id for case_id, _ in rows),
            frozenset(case_id for case_id, result in rows if result == Outcome.PASS),
            frozenset(case_id for case_id, result in rows if result == Outcome.SKIPPED),
        )

    @property
    def age_days(self) -> int | None:
        try:
            return (datetime.date.today() - datetime.date.fromisoformat(self.date)).days if self.date else None
        except ValueError:
            return None


def narrows(cases: Iterable[Case | CaseResult], record: Record | None) -> list[str]:
    """The IDs of the cases `record` passed that the profile skips for lacking the role: a record with these
    rewritten would lose what an earlier profile proved."""
    return sorted(case.id for case in cases if record and case.absent and case.id in record.passed)


def render_record(
    suite_result: SuiteResult, title: str, ha_version: str, devices: list[Device], date: str, source: str, profile: str
) -> str:
    """The test record: a key/value table, then the per-test table. deviceside's check_release.py --records reads
    its commit."""
    suite = suite_result.suite
    rows = [
        ("Result", "pass"),
        ("Date", date),
        ("Source", source),
        ("Profile", profile),
        ("Home Assistant", ha_version),
        ("Device", "; ".join(map(str, devices)) or "none"),
    ]
    if suite.file.options:
        rows.append(("Options", ", ".join(sorted(suite.file.options))))
    lines = [f"# {title} · {suite.name}", "", "| | |", "|---|---|"]
    lines += [f"| {name} | {value} |" for name, value in rows]
    lines += ["", "## Tests", "", *table(suite_result)]
    if any(case.outcome is Outcome.NOT_OBSERVED for case in suite_result.cases):
        lines += ["", "Tests marked `not observed` had nobody watching."]
    if any(case.lent for case in suite_result.cases):
        lines += [
            "",
            f"A pass marked `observed` was not watched in this run: the calls its automated case sent here were seen "
            f"to pass on the same maker's devices on that date ([`{OBSERVED_FILE}`]({OBSERVED_FILE})).",
        ]
    if any(_firmware_fact(suite, case.id) for case in suite_result.proven):
        lines += [
            "",
            "A firmware fact proves the device's behavior, not the adapter's code: its pass stands at the firmware "
            "listed under Device, and may come from an earlier commit at that firmware.",
        ]
    return "\n".join(lines) + "\n"


def report_path(directory: Path, title: str) -> Path:
    """Where the report titled `title` goes; the run's logs share its stem."""
    return directory / f"{title.replace(' ', '-').replace(':', '')}.md"


def calls(suite_result: SuiteResult) -> list[str]:
    """The report's calls section: each traced case's starting state and, per slot, the calls it sent as hashed (a
    run's own, or the twin's a lent case took), so two runs of the same code can be diffed for calls that jitter."""
    lines = []
    for case in suite_result.cases:
        if (trace := case.trace) is None:
            continue
        lent = f", lent from {case.lent}" if case.lent else ""
        unknown = ", never lent or recorded: an option is unknown (null)" if trace.unknown else ""
        lines.append(f"- `{case.id}`: state `{trace.state}`{lent}{unknown}")
        for slot, hashes in trace.sequence:
            lines.append(f"  - {slot}")
            lines += [f"    - `{digest}` `{render_call(trace.legend[digest])}`" for digest in hashes]
    return ["", "### Calls", "", *lines] if lines else []


def write_report(result: RunResult, directory: Path, title: str) -> Path:
    """Every suite's table with each failure's and skip's reason, for any run: failed, partial or unattended."""
    lines = [f"# {title}"]
    for suite_result in result.suites:
        suite = suite_result.suite
        lines += ["", f"## {suite.integration} / {suite.name}", ""]
        if partial := suite_result.partial:
            lines += [f"{partial}: no record.", ""]
        lines += table(suite_result, details=True)
        lines += calls(suite_result)
    directory.mkdir(parents=True, exist_ok=True)
    path = report_path(directory, title)
    path.write_text("\n".join(lines) + "\n")
    return path


async def write_records(
    result: RunResult,
    registry: Any,
    records_dir: Path,
    date: str,
    source: str,
    profile: str,
    into: Path | None = None,
) -> tuple[list[Path], list[tuple[Path, list[str]]]]:
    """`(written, kept)`: one record per suite that fully passed with its facts taken; a partial or failed suite, or
    one whose facts lookup failed, writes nothing: a record naming no device would replace one that did. Each goes
    under `into` (a run's staging, ha_rig/staging.py), laid out as `records_dir`, else into `records_dir`.

    A record never narrows: where the profile lacks a role (an `absent` skip) for a case the existing record passed,
    the file in `records_dir` stays as it is, and the suite is `kept` with those case IDs (its `records_dir` path). To
    narrow on purpose, delete the record."""
    written, kept = [], []
    into = into or records_dir
    for suite_result in result.suites:
        if not suite_result.recordable or suite_result.ha_version is None:
            continue
        suite = suite_result.suite
        title = registry.integrations[suite.integration].title
        path = records_dir / suite.integration / f"{suite.name}.md"
        if narrowed := narrows(suite_result.cases, Record.read(path)):
            kept.append((path, narrowed))
            continue
        path = into / suite.integration / f"{suite.name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_record(suite_result, title, suite_result.ha_version, suite_result.devices, date, source, profile)
        )
        written.append(path)
    return written, kept


def observed_by(case: CaseResult, date: str) -> tuple[list[Observation], dict] | None:
    """What one observed case saw: its rows for _observed.md and their calls. A case a person watched pass, or saw
    fail, adds a row per device; one lent, not observed, failed for another reason or with no trace adds none."""
    if case.trace is None or case.lent or case.twin is None or not case.id.endswith(" observed"):
        return None
    passed = case.outcome is Outcome.PASS
    if not passed and not (case.outcome is Outcome.FAIL and case.detail.startswith(NOT_SEEN)):
        return None
    return case.trace.rows(case.twin, date, passed), dict(case.trace.legend)


def add_observed(records_dir: Path, integration: str, title: str, rows: list[Observation], legend: dict) -> Path:
    """Merge `rows` into the integration's _observed.md in `records_dir` and write it."""
    path = records_dir / integration / OBSERVED_FILE
    observed = ObservedFile.read(path, title)
    observed.add(rows, legend)
    observed.write(path)
    return path


def write_observed_case(suite: Suite, case: CaseResult, registry: Any, records_dir: Path, date: str) -> Path | None:
    """Write what one finished case observed, dated now: each row is written once, when its case ends, so a later
    report or an interrupted run can't re-date it or lose it."""
    if (seen := observed_by(case, date)) is None:
        return None
    return add_observed(
        records_dir, suite.integration, registry.integrations[suite.integration].title, seen[0], seen[1]
    )


def load_observations(records_dir: Path, integrations: Iterable[str]) -> dict[str, list[Observation]]:
    """Each integration's _observed.md rows in `records_dir`, for run()."""
    return {key: ObservedFile.read(records_dir / key / OBSERVED_FILE, "").rows for key in integrations}
