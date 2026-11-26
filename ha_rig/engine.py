"""Runs deviceside's YAML step files against the live HA and renders test records from the results: JSON (format 3, or
format 2 where the schema or the action's transcript lacks it) where the checkout has deviceside's record schema
(ha_rig/schema.py), else markdown.

The step files are the checkout under test's (`tests/rig/steps/`, target.steps_dir). One file per action
(`<action>.yaml`) runs on every integration in the profile that has that action's adapter; a file with `options`
(`<action>-<option>.yaml`) runs it again with those DeviceSide options on, only on adapters whose scope declares them.
The registry supplies what differs per integration: the expected status, the tolerance, and the options in scope.
No pytest here: scripts/rig.py calls run().
"""

import ast
import asyncio
import contextlib
import datetime
import json
import logging
import operator
import re
import shutil
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from . import target as checkout
from .baseline import Baseline
from .bridge import BridgeError, HueBridge
from .client import Z2M, HAClient, HAError, device_ieee
from .facts import AUTOMATED, FAIL, PASS, Fact, FactsFile, append, append_json
from .facts import FILE as FACTS_FILE
from .facts import JSON_FILE as JSON_FACTS_FILE
from .facts import OBSERVED as OBSERVED_KIND
from .journal import Journal
from .profile import (
    EACH_ENTITY,
    ENTRY_RELOADS,
    GROUP_ROLES,
    HA_SCENE,
    ROLES,
    SETTLE_S,
    TOLERANCE_S,
    Profile,
    ha_scene_entity,
    ha_scene_id,
    normalize_ieee,
)
from .schema import CALLS_FORMAT, FORMAT, RecordSchema

_LOGGER = logging.getLogger(__name__)

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
# Z2M's coordinator answers BUSY to a groupcast it has no room for, and Herdsman doesn't retry it. A BUSY in the
# window of a DeviceSide call (its start to its answer plus the log's lag) means the case didn't test DeviceSide: it
# runs again after the coordinator has had this long to clear.
BUSY_LAG_S = 1.0
BUSY_RERUN_WAIT_S = 10.0
COORDINATOR_BUSY = "coordinator busy"

# A light reports off as its fade starts: an observed case waits this long after setup, so the brief comes once the
# lights have reached the start state it describes.
SETUP_SETTLE_S = 2.0
NOT_SEEN = "not seen: "  # an observed case's failure detail when the person saw something else

# A fact case (Case.fact: a watched case, or any case of a firmware fact) finds a fact, whose result stands as written
# down until evidence contradicts it, so a default run leaves it out and --facts (Suite.facts) adds it. A test's
# automated case is an ordinary check of a shipped call, and runs by default, unless the test is a firmware fact.
# Fact cases' results go to the integration's _facts.md (ha_rig/facts.py), never into a record.
OPTED_OUT = "fact case: runs with --facts"  # the skip detail of a fact case in a default run

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

# How long an ha_scene step waits for the scene scene.create made to show in HA.
HA_SCENE_WAIT_S = 10.0
# The roles whose target is a scene, an entity with no device: a test on one lists the lights behind it, as a group's.
SCENE_ROLES = frozenset({"scene", HA_SCENE})
# What an ha_scene step's light takes, in the rig's units: state, and for one on, its brightness and color temperature.
HA_SCENE_KEYS = ("state", "brightness_pct", "color_temp_kelvin")

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
    # The target's DeviceSide classes it runs on (`classes`) or not on (`except_classes`), by integration, as prefixes;
    # at most one is set. Decided per step at run time (Run.skips_class), as the class is learned from a response.
    classes: dict[str, tuple[str, ...]] | None = None
    except_classes: dict[str, tuple[str, ...]] | None = None

    @property
    def class_selector(self) -> dict[str, tuple[str, ...]] | None:
        return self.classes if self.classes is not None else self.except_classes


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
    # A device behavior (precedence, bounds, timing) rather than the adapter's code: its every case, its watched one
    # included, is a fact case (Case.fact). It may also `observe`.
    firmware_fact: bool = False

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
        """The case's own role and every role a step targets, or an ha_scene step makes its scene of, in first-use
        order."""
        names = [role]
        for step in (*self.setup, *self.steps):
            if step.kind == HA_SCENE:
                names += step.args
            elif isinstance(step.args, dict) and (target := step.args.get("target")):
                names.append(target)
        return list(dict.fromkeys(names))

    @property
    def ha_scene(self) -> Step | None:
        """The setup step that makes the test's HA scene, if it has one."""
        return next((step for step in self.setup if step.kind == HA_SCENE), None)


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
    # The name its record takes in place of the action's, for a file that tests something else through it
    # (config_entry).
    name: str | None = None
    # The integrations whose own step files (tests/integrations/<x>/rig/) added tests to it, each only on its own.
    owned_by: frozenset[str] = frozenset()
    # Those files' expected_log, by integration: they apply only to a suite on it (expected_on).
    owned_log: tuple[tuple[str, re.Pattern[str]], ...] = ()

    def expected_on(self, integration: str) -> tuple[re.Pattern[str], ...]:
        """The patterns its suite on `integration` expects: the shared ones, and that integration's own."""
        return self.expected_log + tuple(pattern for owner, pattern in self.owned_log if owner == integration)

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


def _parse_zcl(args: Any) -> dict:
    args = _mapping(args, required=["cluster", "command"], allowed=["target", "endpoint", "params"])
    for key in ("cluster", "command", "endpoint"):
        if key in args and (not isinstance(args[key], int) or isinstance(args[key], bool) or args[key] < 0):
            raise ValueError(f"{key} takes a number, got {args[key]!r}")
    if not isinstance(args.setdefault("params", {}), dict):
        raise ValueError("params takes a mapping of the command's fields")
    return args


ENTRY_MOVES = ("reload", "disable", "enable")


def _parse_entry(args: Any) -> str:
    if args not in ENTRY_MOVES:
        raise ValueError(f"entry takes {', '.join(ENTRY_MOVES)}, got {args!r}")
    return args


def _parse_ha_scene(args: Any) -> dict[str, dict]:
    """The lights of the HA scene the step makes, by light role: each one's state and, for one on, its brightness_pct
    and color_temp_kelvin. An off light takes nothing else: the scene turns it off."""
    if not isinstance(args, dict) or not args:
        raise ValueError("ha_scene takes a mapping of light roles to their states in the scene")
    lights = {}
    for role, raw in args.items():
        if not isinstance(role, str) or role not in ROLES or not role.startswith("light_"):
            raise ValueError(f"{role!r} is not a light role")
        light = _mapping(raw, required=("state",), allowed=HA_SCENE_KEYS)
        light["state"] = _state(light["state"])
        if light["state"] not in ("on", "off"):
            raise ValueError(f"{role}: state must be on or off, got {light['state']!r}")
        if light["state"] == "off" and len(light) > 1:
            raise ValueError(f"{role}: an off light takes no brightness or color temperature")
        pct = light.get("brightness_pct")
        if pct is not None and (isinstance(pct, bool) or not isinstance(pct, int | float) or not 0 < pct <= 100):
            raise ValueError(f"{role}: brightness_pct must be a number above 0 and up to 100, got {pct!r}")
        kelvin = light.get("color_temp_kelvin")
        if kelvin is not None and (isinstance(kelvin, bool) or not isinstance(kelvin, int) or kelvin <= 0):
            raise ValueError(f"{role}: color_temp_kelvin must be a positive whole number, got {kelvin!r}")
        lights[role] = light
    return lights


def _scene_light(light: dict) -> dict:
    """One light's entry in scene.create's `entities`, as HA stores a light's state: brightness 0-255, and a color
    temperature with its color mode, so the route's pick by color mode reads it as a real scene holds it."""
    entry: dict[str, Any] = {"state": light["state"]}
    if (pct := light.get("brightness_pct")) is not None:
        entry["brightness"] = round(pct * 255 / 100)
    if (kelvin := light.get("color_temp_kelvin")) is not None:
        entry |= {"color_temp_kelvin": kelvin, "color_mode": "color_temp"}
    return entry


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
    # One raw ZCL cluster command to a ZHA light, for probes; fails in any other integration's suite.
    "zcl": _parse_zcl,
    # A Home Assistant scene of the case's lights (scene.create), the ha_scene role; setup only, deleted at the case's
    # end.
    "ha_scene": _parse_ha_scene,
    # Zigbee2MQTT device options; both steps do nothing in another integration's suite.
    "z2m_option": _parse_z2m_option,
    "expect_z2m_option": _parse_z2m_option,
}


def _parse_class_selector(value: Any, key: str, label: str) -> dict[str, tuple[str, ...]]:
    """A `classes:` or `except_classes:` value: integration keys mapped to non-empty lists of class prefixes."""
    ok = isinstance(value, dict) and value
    if ok:
        ok = all(
            isinstance(name, str)
            and name
            and isinstance(prefixes, list)
            and prefixes
            and all(isinstance(p, str) and p for p in prefixes)
            for name, prefixes in value.items()
        )
    if not ok:
        raise ValueError(f"{label}: {key} must map integration keys to non-empty lists of class prefixes")
    return {name: tuple(prefixes) for name, prefixes in value.items()}


def _split_class_selectors(raw: Any, label: str) -> tuple[Any, dict[str, dict[str, tuple[str, ...]]]]:
    """The step's mapping without its `classes:` or `except_classes:`, and the one it had, parsed, by key."""
    if not isinstance(raw, dict) or not ({"classes", "except_classes"} & raw.keys()):
        return raw, {}
    if "classes" in raw and "except_classes" in raw:
        raise ValueError(f"{label}: a step takes classes or except_classes, not both")
    selectors = {
        key: _parse_class_selector(raw[key], key, label) for key in ("classes", "except_classes") if key in raw
    }
    return {key: value for key, value in raw.items() if key not in selectors}, selectors


def _parse_step(raw: Any, label: str) -> Step:
    """One `kind: args` mapping, with an optional `only: [<integration>]` beside it limiting the step to those, and
    `classes:` or `except_classes:` limiting it to targets of some DeviceSide classes."""
    raw, selectors = _split_class_selectors(raw, label)
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
        return Step(kind, PARSERS[kind](args), label, frozenset(only) if only else None, **selectors)
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
        test = RigTest(name, tuple(roles), frozenset(only) if only else None, steps, setup, observe, firmware_fact)
        _check_observe_scope(test)
        _check_step_scope(test)
        _check_class_selectors(test)
        _check_ha_scene(test)
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


def _check_step_scope(test: RigTest) -> None:
    """A step's `only` names integrations the test runs on: one outside them would never run."""
    if test.only is None:
        return
    for step in (*test.setup, *test.steps):
        if step.only is not None and (outside := sorted(step.only - test.only)):
            raise ValueError(f"{step.label}: only {', '.join(outside)} are not in the test's only")


def _check_ha_scene(test: RigTest) -> None:
    """The ha_scene role and the setup step that makes it come together: one step, on every integration the test runs
    on (no `only` or class selector), before any step that targets the scene."""
    made = [step for step in test.setup if step.kind == HA_SCENE]
    if len(made) > 1:
        raise ValueError(f"{made[1].label}: a test makes one {HA_SCENE}")
    step = made[0] if made else None
    steps = (*test.setup, *test.steps)
    targeting = [
        s for s in steps if isinstance(s.args, dict) and s.kind != HA_SCENE and s.args.get("target") == HA_SCENE
    ]
    if step is None:
        if HA_SCENE in test.roles or targeting:
            raise ValueError(f"the {HA_SCENE} role needs an {HA_SCENE} setup step to make it")
        return
    if step.only is not None or step.class_selector is not None:
        raise ValueError(
            f"{step.label}: it runs wherever the test does, so it takes no only, classes or except_classes"
        )
    if HA_SCENE not in test.roles and not targeting:
        raise ValueError(f"{step.label}: no step uses the scene it makes; give the test the {HA_SCENE} role")
    if early := next((s for s in targeting if steps.index(s) < steps.index(step)), None):
        raise ValueError(f"{early.label}: targets the {HA_SCENE} before the step that makes it")


def _check_class_selectors(test: RigTest) -> None:
    """A selector needs a class to read: a `call` step before it (setup, then steps), or no class can be known. Its
    integrations are ones the test runs on, as a step's `only` is."""
    called = False
    for step in (*test.setup, *test.steps):
        if (selector := step.class_selector) is not None:
            if not called:
                raise ValueError(f"{step.label}: a class selector needs a call step before it, to learn the class")
            if test.only is not None and (outside := sorted(set(selector) - test.only)):
                raise ValueError(f"{step.label}: classes of {', '.join(outside)} are not in the test's only")
        called = called or step.kind == "call"


def _parse_phases(raw: dict) -> tuple[tuple[Step, ...], tuple[Step, ...], Observe | None]:
    """A test's steps, setup and observe block, with `do` only in the setup of an observed test."""
    steps = _parse_steps(raw.get("steps"), "steps")
    setup = _parse_steps(raw["setup"], "setup") if "setup" in raw else ()
    observe = _parse_observe(raw["observe"]) if "observe" in raw else None
    if observe is not None and not setup:
        raise ValueError("a test with observe needs setup: the steps that bring the lights to where see begins")
    if do := next((step for step in steps if step.kind == "do"), None):
        raise ValueError(f"{do.label}: do belongs in setup, before the brief")
    if made := next((step for step in steps if step.kind == HA_SCENE), None):
        raise ValueError(f"{made.label}: {HA_SCENE} belongs in setup, which makes the scene the steps use")
    if observe is None and (do := next((step for step in setup if step.kind == "do"), None)):
        raise ValueError(f"{do.label}: do is only for a test with observe")
    return steps, setup, observe


def load_step_file(path: Path, text: str | None = None) -> StepFile:
    """The step file at `path`, parsed from `text` when given (what the caller read, and compared)."""
    try:
        raw = yaml.safe_load(path.read_text() if text is None else text)
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


def load_step_files(steps_dir: Path | None = None, integration_dirs: dict[str, Path] | None = None) -> list[StepFile]:
    """Every `<action>.yaml` directly in steps_dir, the shared files, with each integration's own from its directory in
    `integration_dirs` merged in (merge_owned); by default the checkout under test's (target.steps_dir and
    target.integration_steps_dirs)."""
    if steps_dir is None:
        repo = checkout.deviceside_repo()
        steps_dir, integration_dirs = checkout.steps_dir(repo), checkout.integration_steps_dirs(repo)
    shared = [load_step_file(path) for path in sorted(steps_dir.glob("*.yaml"))]
    owned = [
        owned_step_file(load_step_file(path), integration)
        for integration, directory in sorted((integration_dirs or {}).items())
        for path in sorted(directory.glob("*.yaml"))
    ]
    return merge_owned(shared, owned)


def owned_step_file(step_file: StepFile, integration: str) -> StepFile:
    """An integration's own step file, every test scoped to `integration` as if it said `only: [<integration>]`. Raises
    SchemaError for an `only:` naming another integration, or for options or a record name, which only a shared file
    takes."""
    where = f"{integration}/rig/{step_file.path.name}"
    if step_file.options or step_file.name is not None:
        raise SchemaError(f"{where}: options and name are for shared step files")
    mine = frozenset({integration})
    tests = []
    for test in step_file.tests:
        named = (test.only or mine) | ((test.observe and test.observe.only) or mine)
        named |= {key for step in (*test.setup, *test.steps) for key in step.only or ()}
        named |= {key for step in (*test.setup, *test.steps) for key in step.class_selector or ()}
        if others := sorted(named - mine):
            raise SchemaError(f"{where}: {test.name} names {', '.join(others)}, but the file is {integration}'s own")
        tests.append(replace(test, only=mine))
    owned_log = tuple((integration, pattern) for pattern in step_file.expected_log)
    return replace(step_file, tests=tuple(tests), owned_by=mine, expected_log=(), owned_log=owned_log)


def merge_owned(shared: list[StepFile], owned: list[StepFile]) -> list[StepFile]:
    """The shared files with each integration's own merged into the plain file for its action (no options, no record
    name): its tests join that suite, its record and its duration estimate, and its expected_log that suite's on its
    integration alone. One with no shared file stands alone.
    Raises SchemaError for a test name the action's file already has."""
    files = list(shared)
    for own in owned:
        index = next((i for i, f in enumerate(files) if f.action == own.action and not f.options and not f.name), None)
        if index is None:
            files.append(own)
            continue
        base = files[index]
        if clash := sorted({t.name for t in base.tests} & {t.name for t in own.tests}):
            where = f"{next(iter(own.owned_by))}/rig/{own.path.name}"
            raise SchemaError(f"{where}: test {', '.join(clash)} is already in {base.action}'s step file")
        files[index] = replace(
            base,
            tests=base.tests + own.tests,
            rig_seconds=base.rig_seconds + own.rig_seconds,
            owned_log=base.owned_log + own.owned_log,
            owned_by=base.owned_by | own.owned_by,
        )
    return files


def check_owned(step_files: Iterable[StepFile], registry: Any) -> None:
    """Raise SchemaError for an integration's own tests of an action it has no adapter for."""
    for step_file in step_files:
        if missing := sorted(x for x in step_file.owned_by if serving(registry, step_file.action, x) is None):
            raise SchemaError(f"{missing[0]}/rig: {missing[0]} has no {step_file.action} adapter")


def load_checkout_steps(repo: Path) -> tuple[list[StepFile], str | None, bool]:
    """`repo`'s step files, shared and each integration's own merged in (load_step_files), parsed from the bytes read
    here; HEAD, read before them; and whether they are HEAD's: each file's bytes as committed there, and no file more or
    less, in either home. Git's own view (status) would miss an untracked file
    it hides, an ignored one and an assume-unchanged or skip-worktree edit, which the loader reads all the same.

    A record cites the commit, so a run whose files aren't HEAD's writes none, and no fact row, and keeps no ledger: a
    pass saved with an edit, then reverted, would otherwise resume into a record at that commit. Committing moves HEAD,
    so nothing saved before a commit resumes after it."""
    head = checkout.head_commit(repo)
    shared = sorted(checkout.steps_dir(repo).glob("*.yaml"))
    own = {x: sorted(directory.glob("*.yaml")) for x, directory in checkout.integration_steps_dirs(repo).items()}
    paths = shared + [path for paths in own.values() for path in paths]
    sources = {path.relative_to(repo).as_posix(): path.read_bytes() for path in paths}

    def parsed(path: Path) -> StepFile:
        return load_step_file(path, sources[path.relative_to(repo).as_posix()].decode())

    owned = [owned_step_file(parsed(path), x) for x, paths in own.items() for path in paths]
    files = merge_owned([parsed(path) for path in shared], owned)
    return files, head, head is not None and sources == checkout.committed_step_sources(repo, head)


# --- Planning ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Suite:
    """One step file on one integration; `facts` when the run asked for its fact cases (--facts)."""

    file: StepFile
    integration: str
    facts: bool = False

    @property
    def action(self) -> str:
        return self.file.action

    @property
    def name(self) -> str:
        """The name its record takes: the file's `name`, else the action, then any options it runs with on
        (`move-predicted_state`)."""
        return self.file.name or "-".join([self.action, *sorted(self.file.options)])

    def test(self, case_id: str) -> RigTest | None:
        """The test a case id (`name[role]` or `name[role-n]`, maybe ` observed`: Case.id) is a run of."""
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
    # The skip is for a case that doesn't apply here: the role is not in the profile (absent), a group lacks the
    # light it targets, another action it calls has no adapter, or a call isn't built on its declared class (unbuilt).
    # A record allows these skips (SuiteResult), and
    # never narrows to them: one the existing record passed keeps its row (narrows, merge_rows).
    inapplicable: bool = False
    # Which of the role's entities it targets, from 1 in profile order, for an EACH_ENTITY role: each runs its own case.
    index: int = 1
    # The DeviceSide class the profile declares for its own target (`class:`): a call it makes that DeviceSide's scope
    # doesn't serve on it skips the case as not applying (unbuilt), and a response naming another class fails it.
    declared: str | None = None
    # The role whose entity Case.declared is the class of, when not its own: an ha_scene case's light held on, whose
    # class the ridden action isn't built on (unbuilt_light), on the case's skip.
    declared_role: str | None = None

    @property
    def id(self) -> str:
        """`<test>[<role>]`, `<test>[<role>-<n>]` for the n-th entity of an EACH_ENTITY role, ` observed` after for a
        watched case. The first keeps the bare role, so a profile with one entity plans what it always did."""
        return case_id(self.test.name, self.role, self.index, self.observed)

    @property
    def fact(self) -> bool:
        """Whether it is a fact case, which runs only with --facts and whose result is a fact for _facts.md, not a
        record's: its test's watched case, or any case of a firmware fact."""
        return is_fact(self.id, self.test)

    @property
    def twin(self) -> str | None:
        """The id of the test's other case on this role, when it has both: an observed case is skipped when its
        automated twin failed."""
        if not self.test.observed_on(self.role, self.suite.integration) or not self.test.automated:
            return None
        return replace(self, observed=not self.observed).id


def names_classes(registry: Any, suite: Suite) -> bool:
    """Whether the suite's integration names classes: its IntegrationInfo has a `capability` (docs/evidence.md, in
    deviceside: Sign-off format). Read with getattr, as a deviceside checkout from before it has none."""
    info = registry.integrations.get(suite.integration)
    return getattr(info, "capability", None) is not None


def reported_classes(entry: Any) -> frozenset[str] | None:
    """The classes a response entry reports its call reached (deviceside's EntityResult.class_keys): a target's
    `capability`, or a native group's members' `capabilities`; None for neither, which credits none."""
    if not isinstance(entry, dict):
        return None
    if isinstance(capability := entry.get("capability"), str):
        return frozenset({capability})
    capabilities = entry.get("capabilities")
    if isinstance(capabilities, list) and capabilities and all(isinstance(name, str) for name in capabilities):
        return frozenset(capabilities)
    return None


def _one_class(entry: Any) -> str | None:
    """The one class a response entry reports (reported_classes), or None for none or several."""
    classes = reported_classes(entry)
    return next(iter(classes)) if classes is not None and len(classes) == 1 else None


def _behind_class(entry: Any, entity_id: str) -> str | None:
    """The class the target's entry reports for an entity behind it: a native group's `member_capabilities` (each
    member's class by entity ID; one left out, unclassed or provisional, is None), else, as before deviceside's
    response had that map, the entry's one class (_one_class), which a group's `capabilities` names only when its
    members share it."""
    members = entry.get("member_capabilities") if isinstance(entry, dict) else None
    if isinstance(members, dict):
        found = members.get(entity_id)
        return found if isinstance(found, str) and found else None
    return _one_class(entry)


def case_id(test: str, role: str, index: int = 1, observed: bool = False) -> str:
    """A case's id (Case.id)."""
    return f"{test}[{role}{f'-{index}' if index > 1 else ''}]" + (" observed" if observed else "")


def case_family(case_id_: str) -> tuple[str, str, bool]:
    """A case id's test, role and whether it is watched, its entity's index dropped: the cases of one test and role,
    one per entity of an EACH_ENTITY role."""
    observed = case_id_.endswith(" observed")
    test, _, rest = case_id_.removesuffix(" observed").partition("[")
    return test, rest.removesuffix("]").partition("-")[0], observed


# The domain and HA integration of the ha_scene role's entity, a scene HA's own integration holds (scene.create): a
# Ride naming them routes a call on it through the ridden action on every integration (routes).
HA_SCENE_HOST = ("scene", "homeassistant")


def ride(registry: Any, action: str) -> Any:
    """deviceside's Action.rides for `action`, or None: a Ride naming the action whose adapters serve the targets it
    routes itself, and those targets' domain and HA integration (set_scene rides set_attributes on scenes of
    homeassistant). Read with getattr, as a checkout from before Ride has none."""
    return getattr(registry.actions.get(action), "rides", None)


def ridden(registry: Any, action: str) -> str | None:
    """The action `action` rides (ride), or None."""
    return getattr(ride(registry, action), "action", None)


def routes(registry: Any, action: str, role: str | None) -> str | None:
    """The action DeviceSide routes `action`'s call on a target of `role` through, on every integration, or None: the
    ridden one for the ha_scene role, when the Ride names its domain and integration (HA_SCENE_HOST). Any other target
    (Hue's bridge scene, the scene role) is the integration's own adapter's."""
    found = ride(registry, action)
    if role == HA_SCENE and found is not None and (found.domain, found.integration) == HA_SCENE_HOST:
        return found.action
    return None


def serves(registry: Any, action: str, integration: str, role: str | None = None) -> str | None:
    """The action whose adapter serves `action`'s call on `integration`: on a target of `role` DeviceSide routes, the
    ridden action (routes), whatever adapter of its own the integration has; else its own, else, for an action that
    rides another (ridden), the ridden action, whose adapter stands in as DeviceSide's route writes through it; None
    for neither. With no `role`, which adapter serves the suite at all (applies)."""
    if (routed := routes(registry, action, role)) is not None:
        return routed if registry.adapter(routed, integration) is not None else None
    if registry.adapter(action, integration) is not None:
        return action
    if (other := ridden(registry, action)) is not None and registry.adapter(other, integration) is not None:
        return other
    return None


def serving(registry: Any, action: str, integration: str, role: str | None = None) -> Any:
    """The adapter that serves `action`'s call on `integration`, on a target of `role` (serves), or None."""
    found = serves(registry, action, integration, role)
    return None if found is None else registry.adapter(found, integration)


def suite_options(suite: Suite, registry: Any) -> frozenset[str]:
    """The DeviceSide options of each adapter serving the suite's own action on its cases' targets (serving, by the
    target's role), which a run pins as the file sets them: two adapters' for a riding action whose tests target both a
    scene DeviceSide routes and another (Hue's set_scene, on its ha_scene and scene cases)."""
    roles: set[str | None] = set()
    for test in suite.file.tests:
        for step in (*test.setup, *test.steps):
            args = step.args if isinstance(step.args, dict) else {}
            if step.kind == "call" and (args.get("action") or suite.action) == suite.action:
                roles |= {args["target"]} if "target" in args else set(test.roles)
    adapters = [serving(registry, suite.action, suite.integration, role) for role in roles or {None}]
    return frozenset(option for adapter in adapters if adapter is not None for option in adapter.scope.options)


def adapters_for(suite: Suite, registry: Any) -> frozenset[str]:
    """The actions the suite's tests call that an adapter serves on its integration (serving), for `cases`."""
    called = {action for test in suite.file.tests for action in test.calls}
    return frozenset(action for action in called if serving(registry, action, suite.integration) is not None)


def applies(suite: Suite, registry: Any) -> bool:
    """Whether the suite runs on its integration: an adapter serves its action (serving: its own, or the ridden
    action's) and its scope has every option the file sets (a file with none applies wherever there is an adapter). An
    option file that doesn't apply is not part of the run."""
    adapter = serving(registry, suite.action, suite.integration)
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
    suite: Suite,
    profile: Profile,
    keyword: str | None,
    adapters: frozenset[str] | None,
    registry: Any,
) -> list[Case]:
    """The suite's cases in file order, a test's automated case before its observed one; `keyword` keeps only case
    ids containing it. With `adapters` (from adapters_for), a test calling another action without one is skipped, and
    so is a fact case the suite didn't opt in to (`opt_out`), and, from `registry`, a case on a target whose
    declared class one of its calls isn't built on (unbuilt)."""
    result = []
    for test in suite.file.tests:
        if not test.runs_on(suite.integration):
            continue
        lacking = next((a for a in test.calls if adapters is not None and a not in adapters), None)
        for role, index in roles_by_entity(test, profile, suite.integration):
            for observed in [False] * test.automated + [True] * test.observed_on(role, suite.integration):
                case = Case(suite, test, role, {}, observed=observed, index=index)
                if keyword and keyword not in case.id:
                    continue
                if why := opt_out(case):
                    result.append(replace(case, skip=why))
                    continue
                if lacking:
                    result.append(replace(case, skip=f"no {lacking} adapter on {suite.integration}", inapplicable=True))
                    continue
                result.append(_planned(case, profile, registry))
    return result


def _planned(case: Case, profile: Profile, registry: Any) -> Case:
    """A case with its entities, or skipped for why it can't run here: a role the profile lacks, a light outside the
    group, a call not built on the target's declared class (unbuilt), or a device offline."""
    suite, test, role, index = case.suite, case.test, case.role, case.index
    entities = case_entities(test, role, index, profile, suite.integration)
    if missing := [name for name, entity_id in entities.items() if entity_id is None]:
        why = f"no {missing[0]} entity in the profile"
        return replace(case, skip=why, absent=True, inapplicable=True)
    if why := one_light(test, entities):
        return replace(case, entities=entities, skip=why, inapplicable=True)
    group = next((name for name in entities if name in GROUP_ROLES), None)
    members = profile.members(suite.integration, entities[group]) if group else ()
    # A group case that also targets a light means one of the group's: where the profile lists them.
    lights = [name for name in entities if name.startswith("light_")]
    if outside := [name for name in lights if members and entities[name] not in members]:
        why = f"{outside[0]} is not one of the group's members"
        return replace(case, entities=entities, members=members, skip=why, inapplicable=True)
    declared = profile.declared_class(suite.integration, entities[role])
    case = replace(case, entities=entities, members=members, declared=declared)
    if why := unbuilt(case, registry):
        return replace(case, skip=why, inapplicable=True)
    if found := unbuilt_light(case, registry, profile):
        why, light, name = found
        return replace(case, skip=why, inapplicable=True, declared=name, declared_role=light)
    if why := unreachable(case, profile):
        return replace(case, skip=why, unreachable=True)
    return case


# DeviceSide's refusal reasons (its core/errors.py Reason) for a class its scope doesn't serve, as a skip reads them.
UNBUILT = {"not_built": "is not built on", "no_known_method": "has no known method on"}


def unbuilt(case: Case, registry: Any) -> str | None:
    """Why the case doesn't apply to its target's declared class (Case.declared), or None: a call on its own target
    that would run there (its `only` and class selector, judged on that class) names an action whose scope, in the
    checkout under test, refuses the class as not built or no known method (AdapterScope.refusal, as DeviceSide's
    dispatcher refuses it). The scope is that of the action serving the call on the target (serves): for an action that
    rides another, the ridden one's where the integration has none of its own or DeviceSide routes the target through
    it. Decided before the case runs, from the code under test, so a refusal in a case that runs
    still fails it. Raises ValueError for a class the integration doesn't name: a typo would skip every case."""
    if (declared := case.declared) is None:
        return None
    integration = case.suite.integration
    _check_named(registry, integration, declared)
    domain = case.entities[case.role].partition(".")[0]
    for step in _own_calls(case):
        args = step.args if isinstance(step.args, dict) else {}
        if not _selects(step, integration, declared):
            continue
        if (action := serves(registry, args.get("action") or case.suite.action, integration, case.role)) is None:
            continue  # no adapter at all: cases() skips the test for that
        if phrase := _refused(registry.adapter(action, integration), declared, domain):
            return f"{action} {phrase} {declared}"
    return None


def unbuilt_light(case: Case, registry: Any, profile: Profile) -> tuple[str, str, str] | None:
    """For a case on the ha_scene role: why a light its scene holds on doesn't apply to the class the profile declares
    for it, with the light's role and class, or None. DeviceSide routes a call on the scene through the action it rides
    (routes), gating each light held on on that action's scope as unbuilt judges a target; a light held off it turns
    off through core's light.turn_off, which no class gates. Raises ValueError for a class the integration doesn't
    name, as unbuilt does."""
    if case.role != HA_SCENE or (made := case.test.ha_scene) is None:
        return None
    integration = case.suite.integration
    held_on = {
        role: name
        for role, light in made.args.items()
        if light["state"] == "on" and (name := profile.declared_class(integration, case.entities[role])) is not None
    }
    if not held_on:
        return None
    for name in held_on.values():
        _check_named(registry, integration, name)
    for step in _own_calls(case):
        action = (step.args if isinstance(step.args, dict) else {}).get("action") or case.suite.action
        if (routed := routes(registry, action, HA_SCENE)) is None:
            continue
        if (adapter := registry.adapter(routed, integration)) is None:
            continue  # no adapter at all: the suite doesn't apply
        for role, name in held_on.items():
            if phrase := _refused(adapter, name, "light"):
                return f"{routed} {phrase} {name} ({role})", role, name
    return None


def _own_calls(case: Case) -> Iterator[Step]:
    """The case's call steps on its own target that run on its integration (their `only`)."""
    integration = case.suite.integration
    for step in (*case.test.setup, *case.test.steps):
        args = step.args if isinstance(step.args, dict) else {}
        if step.kind != "call" or args.get("target", case.role) != case.role:
            continue
        if step.only is None or integration in step.only:
            yield step


def _check_named(registry: Any, integration: str, name: str) -> None:
    """Raise ValueError for a profile's class the integration doesn't name: a typo would skip every case."""
    if name not in (named := getattr(registry.integrations[integration], "classes", None) or {}):
        raise ValueError(f"the profile's class {name!r} is not one {integration} names: {sorted(named)}")


def _refused(adapter: Any, name: str, domain: str) -> str | None:
    """How the adapter's scope refuses class `name` on `domain` as UNBUILT phrases it, or None for serving it or
    refusing it for another reason (AdapterScope.refusal, as DeviceSide's dispatcher refuses it)."""
    refused = adapter.scope.refusal([name], domain=domain, failure_harms=adapter.failure_harms)
    return UNBUILT.get(str(refused[0])) if refused is not None else None


def _selects(step: Step, integration: str, name: str) -> bool:
    """Whether a step's class selector lets it run on a target of class `name`, as Run.skips_class decides it once the
    class is known: one that doesn't name the integration is inert there, `classes` leaving the step out and
    `except_classes` letting it run."""
    if (selector := step.class_selector) is None:
        return True
    if integration not in selector:
        return step.classes is None
    matches = any(name.startswith(prefix) for prefix in selector[integration])
    return matches if step.classes is not None else not matches


def roles_by_entity(test: RigTest, profile: Profile, integration: str) -> list[tuple[str, int]]:
    """Each of the test's roles with the index of each entity its cases run on: one, the first (or none listed, which
    skips), for most roles; one per listed entity for an EACH_ENTITY role."""
    found = []
    for role in test.roles:
        count = len(profile.entities(integration, role)) if role in EACH_ENTITY else 1
        found += [(role, index) for index in range(1, max(count, 1) + 1)]
    return found


def one_light(test: RigTest, entities: dict[str, str | None]) -> str | None:
    """Why the test's ha_scene can't be made here, or None: two of its roles are one light in the profile, so the
    scene would hold it twice."""
    if (step := test.ha_scene) is None:
        return None
    roles = list(step.args)
    for index, first in enumerate(roles):
        if second := next((other for other in roles[index + 1 :] if entities[other] == entities[first]), None):
            return f"{HA_SCENE}: {first} and {second} are one light"
    return None


def case_entities(test: RigTest, role: str, index: int, profile: Profile, integration: str) -> dict[str, str | None]:
    """The entity for each role the case needs (None: the profile lists none): its own role's `index`-th, the first
    for every role a step targets, and for the ha_scene, the integration's fixed scene, which no profile lists."""
    entities = {
        name: ha_scene_entity(integration) if name == HA_SCENE else profile.entity(integration, name)
        for name in test.roles_needed(role)
    }
    if index > 1:
        entities[role] = profile.entities(integration, role)[index - 1]
    return entities


def opt_out(case: Case) -> str | None:
    """Why a run leaves the case out, or None: a fact case (Case.fact) in a run without --facts. It is skipped, with
    no entities, so it moves and locks nothing."""
    return OPTED_OUT if case.fact and not case.suite.facts else None


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
    return hidden_devices(case, profile)


def hidden_devices(case: Case, profile: Profile) -> str | None:
    """Why the profile can't say which devices the case moves, or None: it targets a fixed group whose members it
    doesn't list, or a fixed scene. Rig-made groups and scenes are made of the roaming bulbs."""
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
                "under other runs; this run holds HA shared. Set them back in HA, or rerun: a suite whose adapter "
                "has options takes HA exclusively"
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
    NOT_RUN = "not run"  # the case never tested DeviceSide (coordinator busy); never saved, so a resume runs it again


# The outcomes of a case that ran, which a record lists with the devices it reached.
RAN = frozenset({Outcome.PASS, Outcome.FAIL, Outcome.NOT_OBSERVED})


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
    inapplicable: bool = False  # Case.inapplicable
    # Its own target and the class the profile declares for it (Case.declared): on a skip, the class a call isn't built
    # on (unbuilt); on a pass, the class DeviceSide's response named for it. A record needs each skip's beside a pass's.
    declared: tuple[str, str] | None = None
    # On a pass of an ha_scene case: each (light, class) its responses named in the scene's `member_capabilities`
    # (entity_classes), the lights it held on, which confirm a declared-class skip as `declared` does.
    confirms: tuple[tuple[str, str], ...] = ()
    # The action whose adapter served its call, when not the suite's own (serves, by its role): an ha_scene case's
    # ridden action, set_attributes. A record's test names it (`via`), so its pass credits that adapter, not the
    # record's own action's (deviceside's check_release.credited, plan Decision 4). Set when the case starts, so a
    # not_run or not_observed row keeps it; None for a case that never started.
    via: str | None = None
    # A case whose result is a fact (fact_kind): the devices it ran on, for its _facts.md row. Never saved.
    devices: tuple[Device, ...] = ()
    # The classes its target reached (reached_classes), for an integration that names them; empty for none.
    classes: tuple[str, ...] = ()
    # A case that ran (RAN): the devices it reached, each with its model ID, firmware and class (_CaseRun.reached),
    # for a record and _facts.json row. Saved, so a resumed suite has them; empty when the lookup failed.
    reached: tuple[Reached, ...] = ()


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
    classes: bool = False  # whether its integration names classes (names_classes): its table has a Capability column

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
        """A complete run in which every default case that applies passed: the only skips allowed are of opt-in cases,
        whose results are facts, not the record's, and of cases that don't apply (Case.inapplicable). At least one
        passed: a record of nothing would replace one that proved something. A skip for a declared class (unbuilt)
        stands only beside a pass whose response named that class for the same entity, so a profile's stale class
        never drops cases unseen; an ha_scene pass names its held-on lights' classes (CaseResult.confirms)."""
        default = [case for case in self.cases if not opt_in(self.suite, case.id)]
        allowed = all(
            case.outcome is Outcome.PASS or (case.outcome is Outcome.SKIPPED and case.inapplicable) for case in default
        )
        passed = [case for case in default if case.outcome is Outcome.PASS]
        confirmed = {case.declared for case in passed} | {pair for case in passed for pair in case.confirms}
        declared = all(
            case.declared in confirmed for case in default if case.declared and case.outcome is Outcome.SKIPPED
        )
        return self.complete and allowed and declared and any(case.outcome is Outcome.PASS for case in default)


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
    paced: bool = False  # a Z2M group case: its restore waits RESTORE_SETTLE_S though nothing was put back
    auto: dict[str, dict] = field(default_factory=dict)  # entity -> {auto_off, auto_on}, as get_attributes reads them
    reloading: dict[str, asyncio.Task] = field(default_factory=dict)  # entity -> its entry reload's watcher
    ha_scene: str | None = None  # the HA scene the case made (its ha_scene step), deleted when it is restored

    def entry(self) -> dict:
        """What the journal keeps for the case: `auto` and `integration` only where a case read auto config,
        `ha_scene` only where it made one."""
        entry = {"taken": self.taken, "stored": self.stored, "power": self.power}
        entry |= {"auto": self.auto, "integration": self.integration} if self.auto else {}
        return entry | ({"ha_scene": self.ha_scene} if self.ha_scene else {})

    def making_scene(self, entity_id: str) -> None:
        """Journal the HA scene the case is about to make, so a crash leaves it to --recover to delete."""
        self.ha_scene = entity_id
        if self.journal is not None:
            self.journal.put("cases", self.journal_key, self.entry())

    async def delete_scene(self) -> bool:
        """Delete the case's HA scene, if HA still has it (a restart drops it); whether it is gone."""
        if self.ha_scene is None:
            return True
        try:
            if await self.ha.get_state(self.ha_scene) is not None:
                await self.ha.call_service("scene", "delete", {"entity_id": self.ha_scene})
        except Exception as err:  # noqa: BLE001 - as restore(): logged, and --recover tries again
            _LOGGER.warning("Could not delete %s: %s; --recover tries again", self.ha_scene, err)
            return False
        self.ha_scene = None
        return True

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
        # The case's end, not a restore step's: the lights are back, so the scene goes.
        scene_left = clear and not await self.delete_scene()
        # A paced case's next groupcasts mustn't land in this one's broadcast-table entries (~9 s each on EmberZNet).
        if clear and (put_back or self.paced):
            await asyncio.sleep(RESTORE_SETTLE_S)
        if clear and self.journal is not None:
            self.journal.drop("cases", self.journal_key)
            self.keep_unrestored(failed, scene_left)

    def keep_unrestored(self, failed: list[str], scene_left: bool = False) -> None:
        """Journal the entities a restore failed on, and a scene it couldn't delete, under the key's `unrestored`
        entry, keeping any already there. Recovering that entry itself, it was dropped first, so only what failed
        again is left."""
        if not (failed or scene_left) or self.journal is None:
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
        if scene_left:
            kept["ha_scene"] = self.ha_scene
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


_SAYING: set[asyncio.Task] = set()  # the speaking cues, held so none is garbage-collected mid-word


async def _say(text: str) -> None:
    """Speak `text` with macOS `say`, then reap the process. Never awaited by a step: see `speak`."""
    with contextlib.suppress(OSError):
        process = await asyncio.create_subprocess_exec(
            "say", text, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        await process.wait()


def say_exists() -> bool:
    return shutil.which("say") is not None


def speak(text: str, out: Callable[[str], None]) -> None:
    """A watched case's cue: print `text` and speak it, when `say` exists, in a task of its own. The call a cue marks
    is timed by the case, so it never waits for the speech or for the process to start."""
    out(f"    >> {text}")
    if not say_exists():
        return
    task = asyncio.get_running_loop().create_task(_say(text))
    _SAYING.add(task)
    task.add_done_callback(_SAYING.discard)


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
        admin: HAClient | None = None,
        json_records: bool = False,
    ):
        self.bridge = bridge
        # The run writes JSON records (ha_rig/schema.py), which list the devices each case reached; markdown ones
        # name a fact's devices instead.
        self.json_records = json_records
        self.admin = admin  # the admin connection, which watches Z2M's log for BUSY refusals
        self.in_setup = False  # while the case's setup runs: a group's `set` may be sent once more
        self.step: Step | None = None  # the step being run
        # Each DeviceSide call's loop times, from the call step's send to its answer: the windows a BUSY refusal in
        # Z2M's log fails the case in (met_busy_coordinator).
        self.call_windows: list[tuple[float, float]] = []
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
            paced=self.integration == "zigbee2mqtt" and any(role in GROUP_ROLES for role in case.entities),
        )
        # Z2M device options a z2m_option step sets, held for the case in the baseline, which puts them back.
        self.baseline: Baseline | None = None
        self.journal, self.journal_key = journal, journal_key
        self.response: dict | None = None
        # For each call of the tested action on the case's own target, outside setup, that returned a response, its
        # entries for the target (else for the members it has), by entity: what the case's classes are read from.
        # Another action's call can reach other members of a group (one action refuses a class another serves), so
        # it credits none.
        self.answers: list[dict[str, Any]] = []
        # The classes the case's own target reported in the latest call's response with an entry for it, setup calls
        # and every action included (empty: it reported none); None before any. What a step's selector reads.
        self.target_classes: frozenset[str] | None = None
        self.upcoming: tuple[Step, ...] = ()  # the steps after the one running, in its phase (following)
        self.error: HAError | None = None
        self.last_action = case.suite.action
        self.last_role = case.role  # the last call's target's role, which picks the adapter serving it (serving)
        self.query = False
        # The loop times the last call was sent and returned, which a sample's readings are timed from.
        self.call_times: tuple[float, float] | None = None
        self.sent: dict[str, Any] = {}  # the last call's data
        self.hue_marks: dict[str, float] = {}  # each light's own hue at the last mark_hue
        # Called with each call's action in words as it goes out; set only while a watched case runs its steps.
        self.cue: Callable[[str], None] | None = None

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
        served = serves(self.registry, case.suite.action, self.integration, case.role)
        result = CaseResult(
            case.id,
            case.role,
            domain,
            Outcome.PASS,
            entity_ids=tuple(case.entities.values()),
            twin=case.twin,
            via=served if served != case.suite.action else None,
        )
        try:
            if case.observed:
                self.out("")
                self.out(f"--- {case.suite.action} / {case.id} ({self.position}) ---")
            await self.await_settled()
            await self.check_power()
            await self.steps(case.test.setup, setup=True)
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
        if await self.met_busy_coordinator():
            result.outcome, result.detail = Outcome.NOT_RUN, COORDINATOR_BUSY
        else:
            await self.note_reached(result)
        if result.outcome is Outcome.PASS:
            self.note_confirmed(result)
        return result

    def note_confirmed(self, result: CaseResult) -> None:
        """On a pass, the classes its responses named that confirm a declared-class skip (SuiteResult.recordable): its
        own target's declared class, when named (CaseResult.declared), and an ha_scene's lights' (scene_classes)."""
        case = self.case
        if case.declared in (self.target_classes or ()):
            result.declared = (case.entities[case.role], case.declared)
        if case.role == HA_SCENE:
            result.confirms = self.scene_classes()

    async def note_reached(self, result: CaseResult) -> None:
        """What the case reached, on its result once it has its outcome, as the run's records name it: for JSON ones,
        the devices a case that ran reached, for its record and _facts.json row; for markdown, a fact's
        devices for its _facts.md row and the classes for the Capability column."""
        if self.json_records:
            if result.outcome in RAN:
                result.reached = await self.reached()
            return
        if fact_kind(self.case.suite, result):
            result.devices = await self.fact_devices()
        result.classes = await self.reached_classes(result.outcome)

    async def reached_classes(self, outcome: Outcome) -> tuple[str, ...]:
        """The classes the case's target reached, sorted, each once, as DeviceSide's responses report them, when it
        passed: only a pass credits a class. Each call of the tested action on the target (answers_for_class) gives
        its entry for the
        target, else its entries for the target's members as they were when it answered (an HA group, which
        DeviceSide answers member by member, or a native group that fell back to its members, a refused one absent).
        None for an integration that names no classes, and none when any such entry reports no class, a call has no
        entry, or no call returned a response: a record writes that as a dash, which credits no class."""
        if outcome is not Outcome.PASS or not names_classes(self.registry, self.case.suite) or not self.answers:
            return ()
        found: set[str] = set()
        for entries in self.answers:
            if not entries or None in (classes := [reported_classes(entry) for entry in entries.values()]):
                return ()
            found.update(*classes)
        return tuple(sorted(found))

    def answers_for_class(self, action: str, entity_id: str) -> bool:
        """Whether the call just answered is one the case's classes are read from: the tested action, on the case's
        own target, outside setup, with a response."""
        tested = action == self.case.suite.action and not self.in_setup
        return tested and entity_id == self.case.entities[self.case.role] and self.response is not None

    async def learn_classes(self) -> None:
        """Take the target's classes from the response just returned, when it has an entry for the case's own target
        (else its members', as entries gives) and a step selects on class or the profile declares one (Case.declared),
        which a response naming other classes fails; a response with none leaves them as they were."""
        test, declared = self.case.test, self.case.declared
        if declared is None and not any(step.class_selector is not None for step in (*test.setup, *test.steps)):
            return
        if not isinstance(self.response, dict):
            return
        if entries := await self.entries(self.response, self.case.entities[self.case.role]):
            self.target_classes = frozenset().union(
                *(reported_classes(entry) or frozenset() for entry in entries.values())
            )
        if declared is not None and self.target_classes and declared not in self.target_classes:
            named = ", ".join(sorted(self.target_classes))
            raise StepFailure(f"DeviceSide named the target's class {named}; the profile declares {declared}")

    def following(self) -> str | None:
        """The kind of the next step that will run, as the classes stand now: a call reads it once its response is
        in, since a class-skipped step is no more the one that checks it (expect_error). A step whose selector
        can't be decided runs, and fails there."""
        for step in self.upcoming:
            try:
                if not self.skips_class(step):
                    return step.kind
            except StepFailure:
                return step.kind
        return None

    def skips_class(self, step: Step) -> bool:
        """Whether the step's `classes:` or `except_classes:` leaves it out here. A selector that doesn't name the
        suite's integration is inert: `classes` skips, `except_classes` runs. Else the target's classes decide, by
        prefix, and none known fails the case rather than guess."""
        if (selector := step.class_selector) is None:
            return False
        if (prefixes := selector.get(self.integration)) is None:
            return step.classes is not None
        if not names_classes(self.registry, self.case.suite) or not self.target_classes:
            raise StepFailure(
                f"{self.integration}'s DeviceSide response has named no class for the target, which this step's "
                f"{'classes' if step.classes is not None else 'except_classes'} needs"
            )
        matches = any(found.startswith(prefix) for found in self.target_classes for prefix in prefixes)
        return matches != (step.classes is not None)

    async def entries(self, response: dict, entity_id: str) -> dict[str, Any]:
        """The response's entry for the entity, else its entries for those of the entity's members it has, by
        entity."""
        if entity_id in response:
            return {entity_id: response[entity_id]}
        return {member: response[member] for member in await self.members(entity_id) if member in response}

    async def reached(self) -> tuple[Reached, ...]:
        """The devices the case reached (reached_entities), each with its model ID, firmware and the class its calls
        reached (entity_classes), for a record and _facts.json row; none when the lookup fails, which costs
        the record (a test that ran must list its devices), not the case."""
        try:
            entity_ids = await self.reached_entities()
            target, role = self.case.entities[self.case.role], self.case.role
            # The entities behind the target, which its own entry's class covers: a group's members, a scene's lights.
            if role in GROUP_ROLES:
                behind = [member for member in await self.members(target) if member in entity_ids]
            else:
                behind = entity_ids if role in SCENE_ROLES else [target]
            found = await reached_devices(self.ha, entity_ids, self.entity_classes(entity_ids, behind))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Could not read the devices %s reached, so its suite writes no record: %s",
                self.case.id,
                err,
            )
            return ()
        if any(device.model is None for device in found):
            self.out(
                f"{self.case.suite.integration} / {self.case.id}: a device it reached has no model ID in HA's registry "
                "(or, on Zigbee2MQTT, its bridge/devices), so its suite's record won't hold to deviceside's schema"
            )
        return found

    async def reached_entities(self) -> list[str]:
        """The entities whose devices the case reached, each once, in role order: a native group's members, never the
        group's own entity, and every other one but a scene's, which has no device (a Hue scene case reaches its
        group's lights; an ha_scene case, the lights its step made the scene of, held off ones included, which the
        scene's entry names no class for: entity_classes). A light a step targets is one of the case's group in every
        step file today."""
        found: list[str] = []
        for role, entity_id in self.case.entities.items():
            if role not in SCENE_ROLES:
                found += await self.members(entity_id)
        return list(dict.fromkeys(found))

    def entity_classes(self, entity_ids: list[str], behind: list[str]) -> dict[str, str | None]:
        """Each reached entity's class, as the responses read for the case's classes (answers) report it: the
        target's entry for every entity `behind` it (_behind_class: a native group's `member_capabilities` names each
        member's), else a member's own entry, where the response answered member by member. None where the integration
        names no classes, no response gave the entity one, or the calls disagree."""
        seen: dict[str, set[str | None]] = {entity_id: set() for entity_id in entity_ids}
        if not names_classes(self.registry, self.case.suite):
            return dict.fromkeys(entity_ids)
        target = self.case.entities[self.case.role]
        for entries in self.answers:
            if target in entries:
                for entity_id in behind:
                    seen[entity_id].add(_behind_class(entries[target], entity_id))
            else:
                for entity_id in entity_ids:
                    if entity_id in entries:
                        seen[entity_id].add(_one_class(entries[entity_id]))
        return {entity_id: next(iter(found)) if len(found) == 1 else None for entity_id, found in seen.items()}

    def scene_classes(self) -> tuple[tuple[str, str], ...]:
        """An ha_scene case's lights, each with the class the scene's entry named for it (entity_classes reads its
        `member_capabilities`), sorted: the lights its route wrote through the ridden action, held on, a held-off one
        never named, as core's light.turn_off takes it. None for an integration that names no classes."""
        lights = [entity_id for role, entity_id in self.case.entities.items() if role != HA_SCENE]
        named = self.entity_classes(lights, lights).items()
        return tuple(sorted((entity_id, name) for entity_id, name in named if name is not None))

    async def fact_devices(self) -> tuple[Device, ...]:
        """The devices behind the case's entities, a group's members included, for its _facts.md row; none when the
        lookup fails, which costs the row, not the case."""
        try:
            lights = [member for entity_id in self.case.entities.values() for member in await self.members(entity_id)]
            entity_ids = dict.fromkeys([*self.case.entities.values(), *lights])
            return tuple((await rig_facts(self.ha, entity_ids))[1])
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not read the devices behind %s, so _facts.md gets no row: %s", self.case.id, err)
            return ()

    @property
    def watches_busy(self) -> bool:
        """Whether Z2M's BUSY refusals are watched for: a Z2M case with the admin connection's log feed."""
        return self.integration == "zigbee2mqtt" and self.admin is not None and self.admin.watching_z2m_busy

    async def met_busy_coordinator(self) -> bool:
        """Whether Z2M logged a BUSY refusal in the window of any DeviceSide call the case sent: the call may not have
        reached the lights, so the case proved nothing about DeviceSide."""
        if not self.watches_busy or not self.call_windows:
            return False
        loop = asyncio.get_running_loop()
        await asyncio.sleep(max(0.0, max(end for _, end in self.call_windows) + BUSY_LAG_S - loop.time()))
        return any(self.admin.z2m_busy_between(start, end + BUSY_LAG_S) for start, end in self.call_windows)

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

    async def steps(self, steps: tuple[Step, ...], relaxed: bool = False, setup: bool = False) -> None:
        integration = self.case.suite.integration
        steps = tuple(step for step in steps if step.only is None or integration in step.only)
        self.in_setup = setup
        try:
            for index, step in enumerate(steps):
                following = steps[index + 1].kind if index + 1 < len(steps) else None
                self.upcoming = steps[index + 1 :]
                handler = getattr(self, f"_{step.kind}")
                if relaxed:
                    handler = getattr(self, f"_relaxed_{step.kind}", handler)
                self.step = step
                try:
                    if self.skips_class(step):
                        continue
                    await handler(step.args, following)
                except StepFailure as err:
                    raise StepFailure(f"{step.label}: {err}") from None
                except _NoOneThere:
                    raise
                except Exception as err:  # noqa: BLE001 - any error ends the case as a fail, not the run
                    raise StepFailure(f"{step.label}: {type(err).__name__}: {err}") from None
        finally:
            self.in_setup = False

    async def observed(self) -> None:
        observe = self.case.test.observe
        assert observe is not None  # only a test with observe has an observed case
        await asyncio.sleep(SETUP_SETTLE_S)
        self.out(f"Lights: {await self.lights_by_name()}")
        self.out(f"You'll see: {_bold(observe.see)}")
        self.out(f"Watch for: {observe.watch}")
        await self.answer("Press Enter to start... ")
        if self.ask is not None:  # a person is there: cue each call, so they know when a move starts and stops
            self.cue = lambda text: speak(text, self.out)
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
        call = {"entity_id": entity_id, **data}
        loop = asyncio.get_running_loop()
        sent_at = loop.time()
        await self.ha.call_service(domain, service, call)
        if domain == "scene":
            return
        want: dict[str, Any] = {"state": args["state"]}
        if "brightness_pct" in data:
            # A light already on reads "on" at once; wait for the brightness, so the bridge state the next call
            # reads is the one just set. Hue rounds to its own 0-100 scale, so allow a step either side.
            brightness = round(data["brightness_pct"] * 255 / 100)
            want["brightness"] = {"min": brightness - 2, "max": brightness + 2}
        # A transition is the light's own fade, which the state follows only at its end.
        within = self.names["tol"] + data.get("transition", 0)
        try:
            await self.await_state(entity_id, want, within)
        except StepFailure as err:
            if self.watches_busy and self.admin.z2m_busy_between(sent_at, loop.time()):
                # Z2M's coordinator refused the send with BUSY, so nothing reached the lights: send it once more.
                label = self.step.label if self.step is not None else "set"
                self.out(f"  {self.case.id}: {label}: Z2M's coordinator answered BUSY; sent the set again")
                await self.ha.call_service(domain, service, call)
                await self.await_state(entity_id, want, within)
                return
            if not (self.in_setup and self.is_group(entity_id)):
                raise
            # The bridge never reports a stale member again by itself, but a fresh command makes the bulb report.
            _LOGGER.warning("%s: setup set to %s; %s; setting the group again", entity_id, data, err)
            await self.ha.call_service(domain, service, call)
            await self.await_state(entity_id, want, within)

    async def _ha_scene(self, args: dict[str, dict], _following: str | None) -> None:
        """Make the case's HA scene of its lights (scene.create), journaled first, and wait for HA to show it. Its
        lights are snapshotted first, as they are the case's to put back."""
        entity_id = self.case.entities[HA_SCENE]
        entities = {}
        for role, light in args.items():
            await self.touch(self.case.entities[role])
            entities[self.case.entities[role]] = _scene_light(light)
        self.snapshot.making_scene(entity_id)
        await self.ha.call_service("scene", "create", {"scene_id": ha_scene_id(self.integration), "entities": entities})
        try:
            await self.ha.wait_for(entity_id, lambda state: state is not None, HA_SCENE_WAIT_S)
        except TimeoutError:
            raise StepFailure(f"scene.create made no {entity_id} within {HA_SCENE_WAIT_S:g} s") from None

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

    async def _zcl(self, args: dict, _following: str | None) -> None:
        """One server-cluster command through zha.issue_zigbee_cluster_command, to the endpoint in the target's
        unique_id unless `endpoint` says otherwise."""
        if self.integration != "zha":
            raise StepFailure(f"zcl steps need a ZHA suite, not {self.integration}")
        entity_id = self.entity(args)
        await self.touch(entity_id)
        entry = await self.ha.command({"type": "config/entity_registry/get", "entity_id": entity_id})
        ieee, _, endpoint = (entry.get("unique_id") or "").partition("-")
        if not ieee or not (endpoint.isdigit() or "endpoint" in args):
            raise StepFailure(f"{entity_id} has no ZHA address in its unique_id {entry.get('unique_id')!r}")
        data = {
            "ieee": ieee,
            "endpoint_id": args.get("endpoint", int(endpoint) if endpoint.isdigit() else None),
            "cluster_id": args["cluster"],
            "cluster_type": "in",
            "command": args["command"],
            "command_type": "server",
            "params": args["params"],
        }
        try:
            await self.ha.call_service("zha", "issue_zigbee_cluster_command", data)
        except HAError as err:
            raise StepFailure(
                f"zcl cluster {args['cluster']} command {args['command']} on {entity_id}: {err}"
            ) from None

    async def _call(self, args: dict, _following: str | None) -> None:
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
        self.last_role = args.get("target", self.case.role)
        # ActionKind is a StrEnum: compared by value so the engine needs no integration import.
        self.query = getattr(getattr(self.registry, "actions", {}).get(action), "kind", None) == "query"
        if self.cue is not None:
            self.cue(action.replace("_", " "))
        sent_at = asyncio.get_running_loop().time()
        try:
            try:
                self.response = await self.ha.call_service("deviceside", action, data, return_response=True)
            finally:
                self.call_windows.append((sent_at, asyncio.get_running_loop().time()))
            self.call_times = self.call_windows[-1]
            if self.answers_for_class(action, entity_id):
                self.answers.append(await self.entries(self.response, entity_id))
            await self.learn_classes()
        except HAError as err:
            if self.following() != "expect_error":
                raise StepFailure(f"deviceside.{action} raised {err.translation_key}: {err}") from None
            self.error = err
            return
        if self.following() == "expect_error":
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
            if (adapter := serving(self.registry, self.last_action, self.integration, self.last_role)) is None:
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
        """The light's hue on its own wheel, in degrees, read live off the bulb: never HA's, which is worked out from
        an xy (ZHA's always, Z2M's after an xy set, as hue_native_control makes one) and departs from the bulb's wheel
        by 25° and more in the greens. ZHA's is EnhancedCurrentHue, else CurrentHue; Z2M's is CurrentHue
        (HAClient.z2m_hue)."""
        if self.integration == "zigbee2mqtt":
            device = await self.ha.z2m_device(ieee := await self.z2m_ieee(light)) or {}
            if (hue := await self.ha.z2m_hue(ieee, device.get("friendly_name") or ieee)) is None:
                raise StepFailure(f"{light}: the bulb returned no hue through Zigbee2MQTT")
            return hue
        if self.integration != "zha":
            raise StepFailure(f"{light}: a hue travel is measured on ZHA and Zigbee2MQTT only")
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
    json_records: bool = False,
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

    With `admin`, a Z2M case watches Z2M's log for the coordinator's BUSY refusals in the windows of its own call steps
    (_CaseRun.met_busy_coordinator). With `json_records`, each case that ran notes the devices it reached, for JSON
    records (ha_rig/schema.py).
    """
    only = unfinished(done, only)
    planned = _plan(suites, profile, registry, ha, admin, keyword, only, out)
    options = {
        id(suite): {name: name in suite.file.options for name in suite_options(suite, registry)}
        for suite, *_ in planned
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
        admin=admin,
        json_records=json_records,
    )
    if busy is not None:
        rig_run.busy = busy
    rig_run.seed([suite for suite, *_ in planned], done or {})
    suite_results: list[SuiteResult] = []
    try:
        if admin is not None and any(suite.integration == "zigbee2mqtt" for suite, *_ in planned):
            try:
                await admin.watch_z2m_busy()
            except Exception as err:  # noqa: BLE001 - without it a BUSY fails a case as it did before
                out(f"Z2M's BUSY refusals aren't watched: {err}")
        waiting = await rig_run.all_automated([(suite, client, cases_) for suite, _, client, cases_ in planned])
        await rig_run.observed(waiting)
    finally:
        if admin is not None:
            await admin.stop_z2m_busy()
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
        part = SuiteResult(suite, finished, complete=complete, classes=names_classes(rig_run.registry, suite))
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
        suite_cases = cases(suite, profile, keyword, adapters_for(suite, registry), registry)
        if only is not None and not (suite_cases := [case for case in suite_cases if only(case)]):
            continue
        adapter = serving(registry, suite.action, suite.integration)
        if adapter is None:
            out(f"{suite.integration} / {suite.action}: no adapter, skipped")
            continue
        if not applies(suite, registry):
            # Another integration's option file, such as Hue's predicted_state: selection drops it silently.
            continue
        client = admin if suite.integration in ADMIN_INTEGRATIONS else ha
        if client is None or ((suite_options(suite, registry) or suite.file.moves_entry) and admin is None):
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
    admin: HAClient | None = None  # the admin connection, which watches Z2M's log for BUSY refusals
    json_records: bool = False  # _CaseRun.json_records

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
            self.admin,
            self.json_records,
        )

    async def run_case(
        self, case: Case, client: HAClient, ask: Callable[[str], str] | None, position: str = ""
    ) -> CaseResult:
        if wanted := self.options.get(id(case.suite)):
            await self.entry_options.apply(wanted)
        result = await self.case_run(case, client, ask, position).run()
        if result.outcome is Outcome.NOT_RUN:
            self.out(
                f"  {case.id}: {COORDINATOR_BUSY} in DeviceSide's call; it runs again in {BUSY_RERUN_WAIT_S:.0f} s"
            )
            await asyncio.sleep(BUSY_RERUN_WAIT_S)  # the case has restored what it took
            result = await self.case_run(case, client, ask, position).run()
        return result

    def report(self, case: Case, result: CaseResult) -> None:
        self.results[id(case.suite), case.id] = result
        if self.on_result is not None:
            self.on_result(case.suite, result)
        # Lanes interleave their lines, so each names its suite.
        prefix = f"{case.suite.integration} / {case.suite.name}  " if self.lanes else "  "
        self.out(f"{prefix}{case.id}: {result.outcome}" + (f" ({result.detail})" if result.detail else ""))

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


def _not_run(case: Case, outcome: Outcome, detail: str = "") -> CaseResult:
    domain = case.entities[case.role].partition(".")[0] if case.entities else None
    return CaseResult(
        case.id,
        case.role,
        domain,
        outcome,
        detail,
        twin=case.twin,
        unreachable=case.unreachable,
        absent=case.absent,
        inapplicable=case.inapplicable,
        declared=(
            (case.entities[case.declared_role or case.role], case.declared)
            if case.declared and case.inapplicable
            else None
        ),
    )


def merge(parts: Iterable[SuiteResult], profile: Profile, registry: Any) -> RunResult:
    """One result per suite from its parts, which runs split between them (the lanes, then the phases, or runs), in
    the order the parts first name each suite, its cases in file order. A suite is complete only when every case it
    plans has a result, none of them an unreachable skip or a case not run: lanes whose phases never ran write no
    record, and neither does a run that left a case out (-k) until another runs it."""
    joined: dict[tuple[str, str], SuiteResult] = {}
    for part in parts:
        key = (part.suite.integration, part.suite.name)
        if key in joined:
            joined[key].cases += part.cases
            joined[key].devices += [device for device in part.devices if device not in joined[key].devices]
            joined[key].ha_version = joined[key].ha_version or part.ha_version
        else:
            joined[key] = SuiteResult(
                part.suite,
                list(part.cases),
                complete=False,
                ha_version=part.ha_version,
                devices=list(part.devices),
                classes=names_classes(registry, part.suite),
            )
    for result in joined.values():
        planned = [
            case.id for case in cases(result.suite, profile, None, adapters_for(result.suite, registry), registry)
        ]
        position = {case_id: index for index, case_id in enumerate(planned)}
        result.cases.sort(key=lambda case: position.get(case.id, len(position)))
        result.planned = len(planned)
        ran = {case.id for case in result.cases if not case.unreachable and case.outcome is not Outcome.NOT_RUN}
        result.complete = set(planned) <= ran
    return RunResult(list(joined.values()))


# --- Test records --------------------------------------------------------------------------------------------


def table(suite_result: SuiteResult, details: bool = False) -> list[str]:
    """The pass/fail table, by role and domain: a record is public, so it never names a household entity. With
    `details` (the run's report), each result has its reason, opt-in cases' included; without (a record), an opt-in
    case reads `skipped` whatever it did, as its result is a fact in _facts.md, not the record's. An integration that
    names classes has a Capability column between Target and Result: the classes each passing target reached, or a
    dash for none and for every other case (docs/evidence.md, in deviceside: Sign-off format)."""
    suite, classes = suite_result.suite, suite_result.classes
    columns = ["Test", "Target", *(["Capability"] if classes else []), "Result"]
    lines = [f"| {' | '.join(columns)} |", "|" + "---|" * len(columns)]
    for case in suite_result.cases:
        via = f", via {case.via}" if case.via and case.outcome is not Outcome.SKIPPED else ""
        target = f"`{case.domain}` ({case.role}{via})" if case.domain else f"{case.role}: none in the profile"
        if not case.domain and not case.absent:  # skipped before planning entities: no adapter, or not opted in
            target = case.role
        cells = [f"`{case.id}`", target]
        if classes:  # only a pass credits a class, and not an opt-in one in a record, where it reads skipped
            credits = case.outcome is Outcome.PASS and (details or not opt_in(suite, case.id))
            cells.append(", ".join(f"`{name}`" for name in case.classes) if credits and case.classes else "-")
        cells.append(_result_cell(suite, case, details))
        lines.append(f"| {' | '.join(cells)} |")
    return lines


def _result_cell(suite: Suite, case: CaseResult, details: bool) -> str:
    if details:
        return str(case.outcome) + (f": {case.detail}" if case.detail else "")
    return str(Outcome.SKIPPED if opt_in(suite, case.id) else case.outcome)


def opt_in(suite: Suite, case_id: str) -> bool:
    """Whether the case is a fact case, which a default run skips (Case.fact)."""
    return is_fact(case_id, suite.test(case_id))


def is_fact(case_id: str, test: RigTest | None) -> bool:
    """A watched case, or any case of a firmware fact."""
    return case_id.endswith(" observed") or (test is not None and test.firmware_fact)


@dataclass(frozen=True, slots=True)
class Device:
    model: str | None
    model_id: str | None
    firmware: str | None

    def __str__(self) -> str:
        model_id = f" (`{self.model_id}`)" if self.model_id and self.model_id != self.model else ""
        return f"{self.model or 'unknown model'}{model_id}, firmware `{self.firmware or 'unknown'}`"


async def rig_facts(ha: HAClient, entity_ids: Iterable[str]) -> tuple[str, list[Device]]:
    """HA's version and the devices behind the entities, via registry calls a non-admin token may make."""
    config, device_of = await asyncio.gather(ha.command({"type": "get_config"}), devices_by_entity(ha))
    found: dict[Device, None] = {}
    for entity_id in entity_ids:
        if (device := device_of.get(entity_id)) is not None:
            found[Device(device.get("model"), device.get("model_id"), record_firmware(device))] = None
    return config["version"], list(found)


async def devices_by_entity(ha: HAClient) -> dict[str, dict]:
    """Each entity's device-registry entry, for those that have one, via registry calls a non-admin token may make."""
    listed, registered = await asyncio.gather(
        ha.command({"type": "config/entity_registry/list_for_display"}),
        ha.command({"type": "config/device_registry/list"}),
    )
    devices = {device["id"]: device for device in registered}
    return {entry["ei"]: devices[entry["di"]] for entry in listed["entities"] if entry.get("di") in devices}


@dataclass(frozen=True, slots=True)
class Reached:
    """A device a case reached, as a record and _facts.json row list it (deviceside's schema: test_device):
    its model ID, maker string, firmware and class."""

    model: str | None  # its model ID (record_model); a record naming none doesn't hold to the schema
    firmware: str | None
    capability: str | None = None  # the class its call reached; None where the response named none
    # Its maker string as its integration reports it (record_manufacturer): a Tuya plug's model, TS011F, is shared by
    # hundreds of makers, which deviceside tells apart by this.
    manufacturer: str | None = None

    def as_json(self) -> dict:
        return {
            "model": self.model,
            "manufacturer": self.manufacturer,
            "firmware": self.firmware,
            "class": self.capability,
        }


def record_model(device: dict, z2m_devices: dict[str, dict]) -> str | None:
    """The model ID a record names for a device-registry entry: a Zigbee device's own (the Basic cluster's,
    such as LWA012) on every integration, else HA's registry `model_id`. Z2M's is its bridge/devices `model_id`
    (`z2m_devices`, its entries by IEEE), as HA's registry holds Z2M's product code (9290018217); ZHA's is the registry
    `model` here, and its diagnostics' in a record (zha_strings), as a quirks v2 friendly name replaces the registry's.
    A Hue device's registry `model_id` is the bridge's product_data.model_id, which HA's Hue integration copies."""
    if (ieee := device_ieee(device, Z2M)) is not None:
        return z2m_devices.get(normalize_ieee(ieee), {}).get("model_id") or None
    if device_ieee(device, "zha") is not None:
        return device.get("model")
    return device.get("model_id")


Z2M_FALLBACK_FIRMWARE = re.compile(r"Zigbee2MQTT \d")  # Z2M's discovery sw_version until it reads a build ID


def record_firmware(device: dict) -> str | None:
    """A device-registry entry's firmware: its `sw_version`, None where it reports none: empty, or on a
    Zigbee2MQTT device or group, Z2M's own `Zigbee2MQTT <version>`, which its discovery sends until it reads the
    device's build ID."""
    firmware = device.get("sw_version") or None
    is_z2m = any(
        domain == "mqtt" and str(value).startswith("zigbee2mqtt_") for domain, value in device.get("identifiers", [])
    )
    if firmware is not None and is_z2m and Z2M_FALLBACK_FIRMWARE.match(firmware):
        return None
    return firmware


def record_manufacturer(device: dict, z2m_devices: dict[str, dict]) -> str | None:
    """The maker string a record names for a device-registry entry, as its integration reports it, never a
    device name: Z2M's bridge/devices `manufacturer` (the Basic cluster's, such as `_TZ3000_…`, not the definition's
    vendor, which HA's registry holds), else HA's registry `manufacturer`: on Hue the bridge's
    product_data.manufacturer_name, which HA's Hue integration copies. None where it reports none. A ZHA device's is
    read from its diagnostics instead (zha_strings)."""
    if (ieee := device_ieee(device, Z2M)) is not None:
        return z2m_devices.get(normalize_ieee(ieee), {}).get("manufacturer") or None
    return device.get("manufacturer") or None


ZHA_UNKNOWN = frozenset({"unk_manufacturer", "unk_model"})  # zha's stand-ins for a Basic cluster string it never read


async def zha_strings(ha: HAClient, device: dict) -> tuple[str | None, str | None]:
    """A ZHA device's own Basic-cluster model and manufacturer, as zigpy read them, from its diagnostics (admin only,
    which a ZHA suite runs on): HA's registry takes zha's `model` and `manufacturer`, which are a quirks v2 device's
    friendly name where one is set, and a Tuya plug's `_TZ3000_…` string is what tells its maker apart. zha's
    stand-ins for a string never read, and an empty one, are None."""
    diagnostics = await ha.device_diagnostics(device)

    def own(name: str) -> str | None:
        value = diagnostics.get(name)
        return value if isinstance(value, str) and value and value not in ZHA_UNKNOWN else None

    return own("model"), own("manufacturer")


async def reached_devices(
    ha: HAClient, entity_ids: Iterable[str], classes: dict[str, str | None]
) -> tuple[Reached, ...]:
    """The devices behind the entities, each once in their order, with each entity's class from `classes`: two
    entities of one device that reached different classes give it none. Z2M's bridge/devices is read only for a Z2M
    device, and a ZHA device's diagnostics only for it, on a connection that may (the admin one Z2M and ZHA suites run
    on). Raises LookupError for an entity HA's registry has no device for: a record can't name what it reached."""
    device_of = await devices_by_entity(ha)
    z2m_devices: dict[str, dict] | None = None  # bridge/devices' entries by IEEE, read for the first Z2M device
    found: dict[str, Reached] = {}
    for entity_id in entity_ids:
        if (device := device_of.get(entity_id)) is None:
            raise LookupError(f"HA's registry has no device for {entity_id}")
        if z2m_devices is None and device_ieee(device, Z2M) is not None:
            z2m_devices = {normalize_ieee(entry.get("ieee_address", "")): entry for entry in await ha.z2m_device_list()}
        z2m = z2m_devices or {}
        if device_ieee(device, "zha") is not None:
            model, manufacturer = await zha_strings(ha, device)
        else:
            model, manufacturer = record_model(device, z2m), record_manufacturer(device, z2m)
        reached = Reached(model, record_firmware(device), classes.get(entity_id), manufacturer)
        if (known := found.get(device["id"])) is None:
            found[device["id"]] = reached
        elif known.capability != reached.capability:
            found[device["id"]] = replace(known, capability=None)
    return tuple(found.values())


CITED_COMMIT = re.compile(r"^\| Source \|.*\bcommit `([0-9a-f]{7,40})`", re.MULTILINE)  # deviceside's Source row
DATE = re.compile(r"^\| Date \| (\d{4}-\d{2}-\d{2}) \|", re.MULTILINE)
OLD_FACT_MARK = " (firmware fact)"  # after a firmware fact's PASS in an older record's Tests table: read, never written
CASE_ROW = re.compile(r"^\| `([^`]+)` \|.*\| ([^|]*?) \|$", re.MULTILINE)  # id, then the result column


@dataclass(frozen=True, slots=True)
class Record:
    """What the rig reads from a test record: the commit it cites, its date, the case IDs in its Tests table (a JSON
    record's `tests`) and which of them passed or were skipped (a markdown record writes no skip detail; a JSON one's
    fact case reads as skipped)."""

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
        if path.suffix == ".json":
            return cls._read_json(path)
        text = path.read_text()
        cited, date = CITED_COMMIT.search(text), DATE.search(text)
        _, _, tests = text.partition("## Tests")
        rows = [(case_id, result.removesuffix(OLD_FACT_MARK)) for case_id, result in CASE_ROW.findall(tests)]
        return cls(
            path,
            cited and cited[1],
            date and date[1],
            frozenset(case_id for case_id, _ in rows),
            frozenset(case_id for case_id, result in rows if result == Outcome.PASS),
            frozenset(case_id for case_id, result in rows if result == Outcome.SKIPPED),
        )

    @classmethod
    def _read_json(cls, path: Path) -> Record:
        """A JSON record, any format; one the rig can't read holds nothing, as a markdown one with no table."""
        try:
            data = json.loads(path.read_text())
            source, tests = data.get("source") or {}, data.get("tests") or []
            rows = [(test["id"], Outcome.SKIPPED if test.get("fact") else test.get("result")) for test in tests]
            commit, date = source.get("commit"), data.get("date")
        except ValueError, AttributeError, KeyError, TypeError:
            return cls(path, None, None, frozenset())
        return cls(
            path,
            commit if isinstance(commit, str) else None,
            date if isinstance(date, str) else None,
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
    """The IDs of the cases `record` passed that this profile skips as not applying (Case.inapplicable: a role it
    lacks, a group without the light, no adapter, a call not built on the declared class), or doesn't plan at all
    though it plans their test on their role (an EACH_ENTITY role with fewer entities listed): a record with these
    rewritten would lose what an earlier profile proved, so their rows are carried into it (merge_rows), or it is
    kept."""
    if record is None:
        return []
    cases = list(cases)
    planned = {case.id for case in cases}
    families = {case_family(case.id) for case in cases}
    skipped = {case.id for case in cases if case.inapplicable and case.id in record.passed}
    dropped = {done for done in record.passed if done not in planned and case_family(done) in families}
    return sorted(skipped | dropped)


def row_devices(row: dict) -> tuple[tuple[str, str], ...]:
    """A record row's devices as (model ID, maker string) pairs, sorted: with its case ID, the row's identity in a
    record (merge_rows). The model, as every device of a format 2 or 3 row names one and the card credits it; with the
    maker string, as a Tuya model ID is many makers'. Not the class, which moves when DeviceSide reclassifies a device,
    and can be null. Empty for a row naming none: a skip, a fact case, a format 1 row."""
    return tuple(sorted((d.get("model") or "", d.get("manufacturer") or "") for d in row.get("devices") or ()))


def unbuilt_rows(rows: list[dict], cases: Iterable[CaseResult]) -> tuple[list[dict], list[tuple[str, str]]]:
    """`rows` less the passing rows of each case this run skips as not built on its declared class (unbuilt) on a
    device of that class, and the (case, class) of each dropped: the code now refuses a call the row shows passing, so
    the record mustn't carry it. Rows on other classes (a Hue bulb's) stay, to be carried (merge_rows)."""
    refused = {case.id: case.declared[1] for case in cases if case.outcome is Outcome.SKIPPED and case.declared}
    kept, dropped = [], []
    for row in rows:
        name = refused.get(row["id"])
        if (
            name is not None
            and row.get("result") == "pass"
            and any(isinstance(device, dict) and device.get("class") == name for device in row.get("devices") or ())
        ):
            dropped.append((row["id"], name))
        else:
            kept.append(row)
    return kept, dropped


def merge_rows(tests: list[dict], old: list[dict]) -> list[dict]:
    """`tests`, a run's rows in its suite's order, merged with `old`, the existing record's (prior). A row is its case
    ID and its devices (row_devices). This run's row of a case replaces the old one on the same devices, in its place,
    and an old one naming none (format 1); an old passing row on other devices stays beside it. An old passing row of
    a case this run skips (as not applying, the only skip a record allows but a fact case's) stands in for the skip.
    One of a case it doesn't plan, whose test and role it does (a plug beyond the profile's), goes after that test and
    role's last row. Every other old row goes: a skip, a fact case, a case no longer planned."""
    standing: dict[str, list[dict]] = {}
    for row in old:
        if row.get("result") == "pass" and not row.get("fact"):
            standing.setdefault(row["id"], []).append(row)
    merged: list[dict] = []
    for row in tests:
        olds = standing.pop(row["id"], [])
        merged += _beside(row, olds)
    families = {case_family(row["id"]) for row in tests}
    for case_id, rows in standing.items():
        if (family := case_family(case_id)) in families:
            at = max(n for n, test in enumerate(merged) if case_family(test["id"]) == family)
            merged[at + 1 : at + 1] = rows
    return merged


def _beside(row: dict, olds: list[dict]) -> list[dict]:
    """For merge_rows: this run's `row` of a case with the old passing rows of it, `olds`: the old rows in for a skip;
    else `row` in place of the old one on its devices (or of one naming none), the rest beside it, in their order."""
    if row.get("fact"):  # its result is _facts.json's: an old pass from before it was one credits nothing now
        return [row]
    if not row.get("devices"):
        return olds or [row]
    mine, placed, rows = row_devices(row), False, []
    for was in olds:
        if not placed and (not was.get("devices") or row_devices(was) == mine):
            rows.append(row)
            placed = True
        elif was.get("devices"):
            rows.append(was)
    return rows if placed else [*rows, row]


def prior(path: Path, blob: str | None) -> tuple[list[dict], str | None]:
    """The rows of the JSON record at `path` (none where there is none, or the rig can't read it), to merge into the
    format 3 record a run citing the transcript `blob` writes over it (merge_rows), and why a row of them can't be
    carried into it, if so: this run's record isn't format 3, the existing one isn't, or it cites another transcript.
    Only the same one: `calls` is the transcript as tested, so a row carried onto another would cite calls it never
    made, and could sign off segments it never ran (deviceside's check_release, reopened)."""
    if not path.is_file():
        return [], None
    try:
        data = json.loads(path.read_text())
        old = data.get("calls")
        rows = [test for test in data["tests"] if isinstance(test, dict) and isinstance(test.get("id"), str)]
    except OSError, ValueError, AttributeError, KeyError, TypeError:
        return [], None
    if blob is None:
        return rows, "this run's record isn't JSON format 3"
    if data.get("format") != CALLS_FORMAT or not isinstance(old, str):
        return rows, "the record isn't JSON format 3"
    if old != blob:
        return rows, f"the record's call transcript ({old[:8]}) isn't this run's ({blob[:8]})"
    return rows, None


def render_record(
    suite_result: SuiteResult,
    title: str,
    ha_version: str,
    devices: list[Device],
    date: str,
    source: str,
    facts: FactsFile | None = None,
    package: str | None = None,
) -> str:
    """The test record: a key/value table, then the per-test table, then a note on the opt-in cases quoting the latest
    row `facts` (the integration's _facts.md) holds for each. deviceside reads its commit, Package tree and Result."""
    suite = suite_result.suite
    cells = [_result_cell(suite, case, details=False) for case in suite_result.cases]
    rows = [
        ("Result", "pass"),
        ("Ran", f"{cells.count(Outcome.PASS)} passed, {cells.count(Outcome.SKIPPED)} skipped"),
        ("Date", date),
        ("Source", source),
        *([("Package", f"`{package}`")] if package else []),
        ("Home Assistant", ha_version),
        ("Device", "; ".join(map(str, devices)) or "none"),
    ]
    if suite.file.options:
        rows.append(("Options", ", ".join(sorted(suite.file.options))))
    lines = [f"# {title} · {suite.name}", "", "| | |", "|---|---|"]
    lines += [f"| {name} | {value} |" for name, value in rows]
    lines += ["", "## Tests", "", *table(suite_result)]
    if chosen := [case.id for case in suite_result.cases if opt_in(suite, case.id)]:
        lines += [
            "",
            f"Fact cases run only when asked (--facts), and their results go to [`{FACTS_FILE}`]({FACTS_FILE}), "
            "never here:",
            "",
        ]
        on_file = facts or FactsFile()
        lines += [f"- `{case_id}`: {on_file.latest(suite.name, case_id) or 'none on file'}" for case_id in chosen]
    return "\n".join(lines) + "\n"


def record_json(
    suite_result: SuiteResult,
    ha_version: str,
    date: str,
    commit: str,
    package: str | None,
    calls: str | None = None,
    old: list[dict] | None = None,
) -> dict:
    """The record (deviceside's docs/test-records.schema.json): the suite's action and options, the run, and a test
    per case in the suite's order, each case that ran with the devices it reached, a native group's its members. No
    counts, notes or facts: deviceside derives what it needs from the tests, and facts are _facts.json's.

    Format 3 with `calls`, the blob of the action's call transcript at the run's commit, which a sign-off stands on;
    format 2 without, citing `package`, the integration's tree there, which credits classes and signs nothing off.

    `old` holds the existing record's rows (prior), merged with this run's (merge_rows)."""
    suite = suite_result.suite
    tests = merge_rows([_test_json(suite, case) for case in suite_result.cases], old or [])
    record: dict[str, Any] = {
        "format": FORMAT if calls is None else CALLS_FORMAT,
        "integration": suite.integration,
        "action": suite.action,
    }
    if suite.file.options:
        record["options"] = sorted(suite.file.options)
    record |= {
        "result": "pass",
        "date": date,
        "source": {"kind": "rig", "commit": commit},
        **({"package": package} if calls is None else {"calls": calls}),
        "home_assistant": ha_version,
        "tests": tests,
    }
    return record


def _test_json(suite: Suite, case: CaseResult) -> dict:
    """A record's test: a fact case skipped as one (its result is _facts.json's), a skipped case with its skip's
    detail, a case that ran with its domain and devices."""
    if opt_in(suite, case.id):
        return {"id": case.id, "role": case.role, "result": str(Outcome.SKIPPED), "fact": True}
    if case.outcome is Outcome.SKIPPED:
        return {"id": case.id, "role": case.role, "result": str(Outcome.SKIPPED), "reason": case.detail}
    test: dict[str, Any] = {"id": case.id, "domain": case.domain, "role": case.role, "result": str(case.outcome)}
    if case.role in GROUP_ROLES or case.role in SCENE_ROLES:  # a scene's test, as a group's, lists its lights
        test["group"] = True
    if case.via:  # served by the action it rides (CaseResult.via): its pass credits that adapter, not the record's
        test["via"] = case.via
    test["devices"] = [device.as_json() for device in case.reached]
    return test


def record_name(suite: Suite, schema: RecordSchema | None) -> str:
    """The suite's record's file name: JSON where the checkout has deviceside's schema, else markdown."""
    return f"{suite.name}.json" if schema is not None else f"{suite.name}.md"


Records = Callable[[str], Path]  # an integration's test records directory (the runner's: target.test_records_dir)


def report_path(directory: Path, title: str) -> Path:
    """Where the report titled `title` goes; the run's logs share its stem."""
    return directory / f"{title.replace(' ', '-').replace(':', '')}.md"


def write_report(result: RunResult, directory: Path, title: str) -> Path:
    """Every suite's table with each failure's and skip's reason, for any run: failed, partial or unattended."""
    lines = [f"# {title}"]
    for suite_result in result.suites:
        suite = suite_result.suite
        lines += ["", f"## {suite.integration} / {suite.name}", ""]
        if partial := suite_result.partial:
            lines += [f"{partial}: no record.", ""]
        lines += table(suite_result, details=True)
    directory.mkdir(parents=True, exist_ok=True)
    path = report_path(directory, title)
    path.write_text("\n".join(lines) + "\n")
    return path


def _prior(
    suite_result: SuiteResult,
    path: Path,
    schema: RecordSchema | None,
    blob: str | None,
    out: Callable[[str], None],
) -> tuple[list[dict], list[str], str | None]:
    """For write_records: the existing record at `path`'s rows (prior), the IDs of the cases whose rows the merge
    would carry into this run's record (merge_rows), said in `out`, and, where they can't be carried, why: the record
    is then kept. A markdown record carries nothing, so it is kept where this run would lose a case it passed."""
    suite = suite_result.suite
    if schema is None:
        narrowed = narrows(suite_result.cases, Record.read(path))
        return [], narrowed, "the record isn't JSON format 3" if narrowed else None
    rows, why = prior(path, blob)
    rows, dropped = unbuilt_rows(rows, suite_result.cases)
    if dropped and why is None:
        for case_id, name in dropped:
            out(
                f"{suite.integration} / {suite.name}: {case_id}: "
                f"passed on {name} in the record, now not built: row dropped"
            )
    merged = merge_rows([_test_json(suite, case) for case in suite_result.cases], rows)
    carried = sorted({row["id"] for row in merged if any(row is was for was in rows)})
    if carried and why is None:
        out(
            f"{suite.integration} / {suite.name}: kept the record's rows of {', '.join(carried)} from earlier runs: "
            "cases this profile skips as not applying or doesn't plan, or ran on other devices"
        )
    return rows, carried, why if carried else None


async def write_records(
    result: RunResult,
    registry: Any,
    records: Records,
    date: str,
    source: str,
    into: Records | None = None,
    package: Callable[[str], str] | None = None,
    *,
    calls: Callable[[str, str], str | None] | None = None,
    schema: RecordSchema | None = None,
    commit: str | None = None,
    out: Callable[[str], None] = print,
) -> tuple[list[Path], list[tuple[Path, list[str], str]]]:
    """`(written, kept)`: one record per suite that fully passed with its facts taken; a partial or failed suite, or
    one whose facts lookup failed, writes nothing: a record naming no device would replace one that did. Each goes
    in its integration's directory of `into` (a run's staging, ha_rig/staging.py), else of `records`.

    A record never narrows, nor loses a device: the existing record's passing rows of the cases this run skips as not
    applying or doesn't plan, or ran on other devices, are carried into the record this run writes, as they are
    (merge_rows), and `out` says so. Where they can't be (a markdown or format 2 record on either side, or one citing
    another call transcript than this run's: prior), the file in `records` stays as it is, and the suite is `kept`
    with those case IDs and why (its `records` path). To narrow on purpose, delete the record.

    `package` gives an integration's package tree at the run's commit, which each of its records cites in a Package row
    after Source: what deviceside's release check compares to the tree that ships.

    With the checkout's `schema`, each record is JSON (record_json) citing `commit`, the run's (abbreviated), in place
    of `source`, and is written only once the schema takes it: one it refuses is said in `out`, and written nowhere.
    A schema that takes format 3 (`binds_calls`) gets it, citing `calls(integration, action)`, the blob of the
    action's call transcript at the run's commit, in place of the package tree; where that is None (the commit has no
    transcript of the action) the suite's record is format 2, citing `package`, and `out` says why. A schema without
    format 3 gets format 2. Without a schema, markdown (render_record)."""
    if schema is not None and not commit:
        raise ValueError("a JSON record cites the run's commit: pass `commit` with `schema`")
    written, kept = [], []
    into = into or records
    for suite_result in result.suites:
        if not suite_result.recordable or suite_result.ha_version is None:
            continue
        suite = suite_result.suite
        title = registry.integrations[suite.integration].title
        path = records(suite.integration) / record_name(suite, schema)
        binds = schema is not None and schema.binds_calls
        blob = calls(suite.integration, suite.action) if binds and calls else None
        old, carried, why = _prior(suite_result, path, schema, blob, out)
        if why is not None:
            kept.append((path, carried, why))
            continue
        path = into(suite.integration) / record_name(suite, schema)
        tree = package(suite.integration) if package else None
        if schema is not None:
            ran = [case for case in suite_result.cases if case.outcome in RAN and not opt_in(suite, case.id)]
            if blind := [case.id for case in ran if not case.reached]:
                out(
                    f"{suite.integration} / {suite.name}: no record, as no devices were noted for {', '.join(blind)} "
                    "(the registry lookup failed, or an older rig saved them): a run at this commit reruns those cases"
                )
                continue
            if binds and blob is None:
                out(
                    f"{suite.integration} / {suite.name}: no call transcript of {suite.action} at {commit} "
                    f"(tests/integrations/{suite.integration}/calls/{suite.action}.json), so the record is format 2: "
                    "it credits classes but signs nothing off"
                )
            try:
                schema.write(path, record_json(suite_result, suite_result.ha_version, date, commit, tree, blob, old))
            except (OSError, ValueError) as err:  # InvalidRecord, or a file that couldn't be written
                out(f"{suite.integration} / {suite.name}: {err}")
                continue
        else:
            facts = FactsFile.read(records(suite.integration) / FACTS_FILE)
            record = render_record(
                suite_result, title, suite_result.ha_version, suite_result.devices, date, source, facts, tree
            )
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(record)
            except OSError as err:
                out(f"{suite.integration} / {suite.name}: {path.name} couldn't be written: {err}")
                continue
        written.append(path)
    return written, kept


def observed_answer(case: CaseResult) -> bool:
    """Whether a person answered the observed case: they saw it pass, or saw something else (a step that broke, or
    nobody there, is no answer)."""
    if not case.id.endswith(" observed"):
        return False
    return case.outcome is Outcome.PASS or (case.outcome is Outcome.FAIL and case.detail.startswith(NOT_SEEN))


def fact_kind(suite: Suite, case: CaseResult) -> str | None:
    """How a fact case's result was seen, for _facts.md, or None for no fact: `observed` for a watched case a person
    answered, `automated` for a firmware fact's automated case that passed or failed."""
    if case.id.endswith(" observed"):
        return OBSERVED_KIND if observed_answer(case) else None
    if opt_in(suite, case.id) and case.outcome in (Outcome.PASS, Outcome.FAIL):
        return AUTOMATED
    return None


def write_fact(
    suite: Suite,
    case: CaseResult,
    registry: Any,
    records: Records,
    date: str,
    schema: RecordSchema | None = None,
    out: Callable[[str], None] = print,
) -> Path | None:
    """Append what one fact case found to its integration's _facts.md in `records`, dated now: each row is
    written once, when its case ends, so a later report or an interrupted run can't re-date it or lose it. None for a
    result that is no fact (fact_kind), or whose devices couldn't be read.

    With the checkout's `schema`, the row goes to _facts.json, with the devices the case reached (CaseResult.reached),
    once the schema takes the file; a file the rig can't read, or a row the schema refuses, is left as it was, and
    the row is said in `out` instead, so the fact isn't lost."""
    if (kind := fact_kind(suite, case)) is None:
        return None
    result = PASS if case.outcome is Outcome.PASS else FAIL
    if schema is not None:
        path = records(suite.integration) / JSON_FACTS_FILE
        if not case.reached:
            out(
                f"Not written to {path.name}: {suite.name} / {case.id} found {result}, but its devices couldn't be read"
            )
            return None
        row = {"date": date, "suite": suite.name, "test": case.id, "kind": kind, "result": result}
        row["devices"] = [device.as_json() for device in case.reached]
        try:
            append_json(path, suite.integration, row, schema)
        except (OSError, ValueError) as err:
            out(f"Not written to {path.name} ({err}); add the row by hand: {json.dumps(row)}")
            return None
        return path
    if not case.devices:
        return None
    path = records(suite.integration) / FACTS_FILE
    append(
        path,
        registry.integrations[suite.integration].title,
        Fact(date, suite.name, case.id, kind, _devices(case), result),
    )
    return path


def _devices(case: CaseResult) -> str:
    return "; ".join(map(str, case.devices))
