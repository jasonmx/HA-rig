"""A test run's finished cases, on disk, so a run that stops part way picks up where it left off.

A run saves each case the moment it finishes, and the facts each part of a suite took (HA's version, the devices behind
its entities), in `deviceside-rig-progress/<profile>--<branch>.json` in deviceside's git common dir: one file per
profile and branch, so a worktree's runs don't collide. A rerun with the same key (profile, the integration's local
tree, the selection) skips the saved cases and joins their results to its own, so a suite finished across sessions
writes its record; any other key starts fresh, saying why. So does HA running another version than the one saved: a
record names one. The file goes once every case has a result. It holds household entity IDs, so it stays out of the
repo, like the journals.

Nothing is written until the run saves its first case, so a dry run, or a run that never starts, leaves an earlier
run's progress as it was.
"""

import datetime
import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .engine import CaseResult, Device, Outcome, Suite, SuiteResult
from .journal import write_whole

DIRECTORY = "deviceside-rig-progress"

Done = dict[tuple[str, str, str], CaseResult]  # (integration, suite name, case id): its result


@dataclass(frozen=True, slots=True)
class Key:
    """What a run's saved cases are valid for: the profile, the integration tree HA runs, and the selection."""

    profile: str
    tree: str
    integrations: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    keyword: str | None = None
    serial: bool = False
    unattended: bool = False

    @classmethod
    def of(cls, raw: Any) -> Key | None:
        try:
            return cls(
                raw["profile"],
                raw["tree"],
                tuple(raw["integrations"]),
                tuple(raw["actions"]),
                raw["keyword"],
                raw["serial"],
                raw["unattended"],
            )
        except KeyError, TypeError:
            return None

    def as_json(self) -> dict:
        return asdict(self) | {"integrations": list(self.integrations), "actions": list(self.actions)}

    def differs(self, other: Key | None) -> str | None:
        """Why saved cases under `other` don't hold for this key, or None when they do."""
        if other is None:
            return "its key is unreadable"
        if other.profile != self.profile:
            return "profile changed"
        if other.tree != self.tree:
            return "tree changed"
        if other != self:
            return "selection changed"
        return None


def path_for(directory: Path, profile: str, branch: str) -> Path:
    return directory / DIRECTORY / f"{profile}--{branch.replace('/', '_')}.json"


class Progress:
    def __init__(self, path: Path, key: Key, saved: dict | None = None):
        self.path = path
        self.key = key
        saved = saved or {}
        self.started: str = saved.get("started") or datetime.datetime.now().isoformat(timespec="seconds")
        self.ha_version: str | None = saved.get("ha_version")
        self.cases: list[dict] = list(saved.get("cases", []))
        self.facts_by: dict[tuple[str, str], dict] = {
            (entry["integration"], entry["suite"]): entry for entry in saved.get("facts", [])
        }

    @classmethod
    def load(
        cls,
        directory: Path,
        profile: str,
        branch: str,
        key: Key,
        out: Callable[[str], None] = print,
        fresh: bool = False,
    ) -> Progress:
        """The saved progress when its key matches `key`, else an empty one, saying why it starts fresh. `fresh`
        (--fresh) starts fresh whatever is saved. The saved file stays until the run saves its first case."""
        path = path_for(directory, profile, branch)
        try:
            saved = json.loads(path.read_text()) if path.exists() else None
        except ValueError:
            saved = {}
        if saved is None:
            return cls(path, key)
        if fresh:
            out("Starting fresh (--fresh): the saved progress is dropped.")
            return cls(path, key)
        if why := key.differs(Key.of(saved.get("key"))):
            out(f"Starting fresh: {why} since the saved progress (started {saved.get('started', 'unknown')}).")
            return cls(path, key)
        return cls(path, key, saved)

    def __bool__(self) -> bool:
        return bool(self.cases)

    def check_ha(self, version: str | None, out: Callable[[str], None] = print) -> None:
        """Start fresh when HA runs another version than the saved cases ran on, or its version can't be read: a
        record names one version. Otherwise note HA's version for the cases to come."""
        if self.cases and version != self.ha_version:
            seen = f"HA is {version}" if version else "HA's version can't be read"
            out(f"Starting fresh: {seen}, and the saved progress ran on {self.ha_version}.")
            self.reset()
        self.ha_version = self.ha_version or version

    def reset(self) -> None:
        """Forget the saved cases in memory; the file is overwritten on the next save."""
        self.cases, self.facts_by = [], {}
        self.started = datetime.datetime.now().isoformat(timespec="seconds")

    def record(self, suite: Suite, result: CaseResult) -> None:
        """Save one finished case, replacing any earlier result of it."""
        entry = {"integration": suite.integration, "suite": suite.name} | asdict(result)
        entry["outcome"] = str(result.outcome)
        entry["entity_ids"] = list(result.entity_ids)
        same = (suite.integration, suite.name, result.id)
        self.cases = [case for case in self.cases if (case["integration"], case["suite"], case["id"]) != same]
        self.cases.append(entry)
        self.save()

    def facts(self, suite: Suite, ha_version: str | None, devices: Iterable[Device]) -> None:
        """Save the facts a part of `suite` took, joined to those of its earlier parts."""
        key = (suite.integration, suite.name)
        entry = self.facts_by.setdefault(
            key, {"integration": key[0], "suite": key[1], "ha_version": None, "devices": []}
        )
        entry["ha_version"] = entry["ha_version"] or ha_version
        self.ha_version = self.ha_version or ha_version
        for device in map(asdict, devices):
            if device not in entry["devices"]:
                entry["devices"].append(device)
        self.save()

    def done(self) -> Done:
        return {(case["integration"], case["suite"], case["id"]): _result(case) for case in self.cases}

    def parts(self, suites: Iterable[Suite]) -> list[SuiteResult]:
        """The saved results of each of `suites` that has any, as incomplete parts for merge to join."""
        parts = []
        for suite in suites:
            key = (suite.integration, suite.name)
            saved = [_result(case) for case in self.cases if (case["integration"], case["suite"]) == key]
            if not saved:
                continue
            facts = self.facts_by.get(key, {})
            devices = [Device(**device) for device in facts.get("devices", [])]
            parts.append(SuiteResult(suite, saved, complete=False, ha_version=facts.get("ha_version"), devices=devices))
        return parts

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_whole(
            self.path,
            {
                "key": self.key.as_json(),
                "ha_version": self.ha_version,
                "started": self.started,
                "cases": self.cases,
                "facts": list(self.facts_by.values()),
            },
        )

    def discard(self) -> None:
        """Delete the saved progress: the run finished every case."""
        self.reset()
        self.path.unlink(missing_ok=True)


def _result(case: dict) -> CaseResult:
    return CaseResult(
        case["id"],
        case["role"],
        case["domain"],
        Outcome(case["outcome"]),
        case["detail"],
        tuple(case["entity_ids"]),
        case["twin"],
    )
