"""The ledger: each suite's finished cases, on disk, so a run that stops part way picks up where it stopped.

A run saves each case the moment it finishes, and the facts each part of a suite took (HA's version, the devices behind
its entities), in `deviceside-rig-progress/<profile>.json` in deviceside's git common dir: one file per profile, shared
by every worktree. Its entries are per suite, keyed by the commit the run planned at, so a rerun at the same commit
resumes, and any other starts the suite afresh. The commit covers the step files, as deviceside's: a run with
uncommitted step files reads and saves no ledger and writes no record (scripts/rig.py, uncommitted_steps). A
later run joins a suite's saved results to its own, so a suite finished across runs writes its record. It is
execution state only: what a suite needs to prove, and when, is deviceside's to say.

An entry goes when:
- HA runs another version than its cases ran on: a record names one;
- its suite's record is promoted into the checkout (ha_rig/staging.py);
- the run that saved a failure of one of its default cases ends: the suite reruns whole, so a record never covers up a
  failure. A fact case's failure is a fact (ha_rig/facts.py), not the suite's, so it drops nothing;
- nothing has touched it for PRUNE_DAYS.

Two runs on different integrations may save at once, so each save re-reads the file under a lock and merges this run's
changes into it. Nothing is written until the run saves, so a dry run, or a run that never starts, leaves the file as
it was. It holds household entity IDs, so it stays out of the repo, like the journals.
"""

import contextlib
import datetime
import fcntl
import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict
from pathlib import Path

from .engine import RAN, Case, CaseResult, Device, Outcome, Reached, Suite, SuiteResult, opt_in
from .journal import write_whole

DIRECTORY = "deviceside-rig-progress"
PRUNE_DAYS = 30

Done = dict[tuple[str, str, str], CaseResult]  # (integration, suite name, case id): its result
EntryKey = tuple[str, str, str]  # (integration, suite name, commit)


def path_for(directory: Path, profile: str) -> Path:
    return directory / DIRECTORY / f"{profile}.json"


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _key(entry: dict) -> EntryKey:
    return entry["integration"], entry["suite"], entry["at"]


def _read(path: Path) -> dict[EntryKey, dict]:
    """The file's entries; one with no `at` is left out, so its suite reruns. One keyed by an older rig's `at` (the
    commit and a step-file digest) matches no commit, so its suite reruns too."""
    try:
        saved = json.loads(path.read_text()) if path.exists() else {}
        return {_key(entry): entry for entry in saved.get("suites", []) if "at" in entry}
    except ValueError, KeyError, TypeError, AttributeError:
        return {}


def _old(entry: dict, now: datetime.datetime) -> bool:
    try:
        touched = datetime.datetime.fromisoformat(entry.get("touched", ""))
    except ValueError:
        return True
    return now - touched > datetime.timedelta(days=PRUNE_DAYS)


def holds(case: Case, result: CaseResult, unattended: bool, devices: bool = False) -> bool:
    """Whether a saved result still stands for `case` on resume: the profile skips it for the same reason, or it moved
    the entities the case plans now. A `not observed` stands only for an unattended run. With `devices` (JSON
    records, which list what each case reached), a case that ran saved with none (the lookup failed, or an older rig
    saved it) doesn't."""
    if case.skip and not case.unreachable:  # an unreachable skip is for now: what it saved before still stands
        return result.outcome is Outcome.SKIPPED and result.detail == case.skip
    if devices and result.outcome in RAN and not result.reached:
        return False
    if result.outcome is Outcome.NOT_OBSERVED:
        return unattended
    return result.outcome is not Outcome.SKIPPED and result.entity_ids == tuple(case.entities.values())


class Progress:
    """The profile's ledger, as this run sees it: every entry on disk, and the suites the run `track`s, each at its
    commit. Its changes are merged into the file on each save."""

    def __init__(self, path: Path, entries: dict[EntryKey, dict] | None = None):
        self.path = path
        self.entries: dict[EntryKey, dict] = dict(entries or {})
        self.tracked: dict[tuple[str, str], str] = {}  # (integration, suite name): its commit
        self.suites: dict[tuple[str, str], Suite] = {}  # the tracked suites, for which of their cases are fact cases
        self.ha_version: str | None = None
        self.changed: set[EntryKey] = set()
        self.dropped: set[EntryKey] = set()

    @classmethod
    def load(cls, directory: Path, profile: str) -> Progress:
        """The ledger on disk, its entries untouched for PRUNE_DAYS dropped (from the file at the next save)."""
        path = path_for(directory, profile)
        progress = cls(path, _read(path))
        if path.exists() and not progress.entries and path.read_text().strip() not in ("", "{}"):
            print(f"The ledger {path} is unreadable or from an older rig, so every saved case reruns.")
        now = datetime.datetime.now()
        for key in [key for key, entry in progress.entries.items() if _old(entry, now)]:
            progress._drop(key)
        return progress

    def track(self, suite: Suite, at: str) -> None:
        """Follow `suite` at `at`, a commit: its saved cases there are the run's, and its new ones are saved there."""
        self.tracked[suite.integration, suite.name] = at
        self.suites[suite.integration, suite.name] = suite

    def resume(
        self,
        suite: Suite,
        commit: str,
        planned: Iterable[Case],
        *,
        fresh: bool = False,
        unattended: bool = False,
        devices: bool = False,
    ) -> None:
        """Track `suite` at `commit`, keeping the saved results that still hold for the cases it
        `planned` (holds, with `devices` for JSON records). `fresh` (--fresh), or a saved failure, drops the suite's
        entry, so it runs whole."""
        self.track(suite, commit)
        saved = self.saved(suite)
        if fresh or _failed(suite, saved.values()):
            self.drop(suite)
            return
        by_id = {case.id: case for case in planned}
        self.forget(
            suite,
            [
                case_id
                for case_id, result in saved.items()
                if case_id not in by_id or not holds(by_id[case_id], result, unattended, devices)
            ],
        )

    def key(self, suite: Suite) -> EntryKey | None:
        if (at := self.tracked.get((suite.integration, suite.name))) is None:
            return None
        return suite.integration, suite.name, at

    def entry(self, suite: Suite) -> dict | None:
        """The tracked suite's entry at its commit, if it has one."""
        return self.entries.get(self.key(suite) or ("", "", ""))

    def __bool__(self) -> bool:
        return bool(self.done())

    def tracked_keys(self) -> list[EntryKey]:
        return [(integration, name, at) for (integration, name), at in self.tracked.items()]

    def check_ha(self, version: str, out: Callable[[str], None] = print) -> None:
        """Drop each entry whose cases ran on another HA version than `version`, or on one never noted (a run killed
        before its suite's facts), from the file at the next save: a record names one. Notes `version` for the cases
        to come."""
        self.ha_version = version
        for key, entry in list(self.entries.items()):
            if (saved := entry.get("ha_version")) != version and entry.get("cases"):
                out(f"{key[0]} / {key[1]}: its saved cases ran on HA {saved}, and HA is {version}; they rerun.")
                self._drop(key)

    def _entry(self, suite: Suite) -> dict:
        key = self.key(suite)
        if key is None:
            raise KeyError(f"{suite.integration} / {suite.name} isn't tracked")
        entry = self.entries.setdefault(
            key,
            {
                "integration": key[0],
                "suite": key[1],
                "at": key[2],
                "ha_version": self.ha_version,
                "devices": [],
                "cases": [],
            },
        )
        entry["ha_version"] = entry.get("ha_version") or self.ha_version
        entry["touched"] = _now()
        self.changed.add(key)
        self.dropped.discard(key)
        return entry

    def record(self, suite: Suite, result: CaseResult) -> None:
        """Save one finished case, replacing any earlier result of it; an unreachable skip or a case not run is
        never saved, as it proves nothing (a resume runs the case again)."""
        if result.unreachable or result.outcome is Outcome.NOT_RUN:
            return
        entry = self._entry(suite)
        entry["cases"] = [saved for saved in entry["cases"] if saved["id"] != result.id] + [_saved(result)]
        self.save()

    def facts(self, suite: Suite, ha_version: str | None, devices: Iterable[Device]) -> None:
        """Save the facts a part of `suite` took, joined to those of its earlier parts."""
        entry = self._entry(suite)
        entry["ha_version"] = entry["ha_version"] or ha_version
        for device in map(asdict, devices):
            if device not in entry["devices"]:
                entry["devices"].append(device)
        self.save()

    def forget(self, suite: Suite, case_ids: Iterable[str]) -> None:
        """Drop saved cases that no longer hold (their entities changed), until the next save writes it."""
        if (entry := self.entry(suite)) is None:
            return
        gone = set(case_ids)
        if any(case["id"] in gone for case in entry["cases"]):
            entry["cases"] = [case for case in entry["cases"] if case["id"] not in gone]
            self.changed.add(_key(entry))

    def saved(self, suite: Suite) -> dict[str, CaseResult]:
        """The tracked suite's saved results by case id."""
        entry = self.entry(suite)
        return {case["id"]: _result(case) for case in entry["cases"]} if entry else {}

    def done(self) -> Done:
        """The tracked suites' saved results."""
        return {
            (key[0], key[1], case["id"]): _result(case)
            for key in self.tracked_keys()
            if (entry := self.entries.get(key))
            for case in entry["cases"]
        }

    def parts(self, suites: Iterable[Suite]) -> list[SuiteResult]:
        """The saved results of each of `suites` that is tracked and has any, as incomplete parts for merge to join."""
        parts = []
        for suite in suites:
            if (entry := self.entry(suite)) is None or not entry["cases"]:
                continue
            devices = [Device(**device) for device in entry["devices"]]
            cases = [_result(case) for case in entry["cases"]]
            parts.append(SuiteResult(suite, cases, complete=False, ha_version=entry["ha_version"], devices=devices))
        return parts

    def drop(self, suite: Suite) -> None:
        """Drop the suite's entry at its commit (--fresh, or its record written), until the next save."""
        if (key := self.key(suite)) is not None:
            self._drop(key)

    def drop_entries(self, keys: Iterable[EntryKey]) -> None:
        """Drop these entries, tracked or not (their records promoted: ha_rig/staging.py), until the next save."""
        for key in keys:
            self._drop(tuple(key))

    def _drop(self, key: EntryKey) -> None:
        self.entries.pop(key, None)
        self.changed.discard(key)
        self.dropped.add(key)

    def drop_failed(self) -> list[tuple[str, str]]:
        """At the run's end: drop each tracked suite holding a default case's failure, so it reruns whole; saved.
        Returns them."""
        failed = []
        for key in self.tracked_keys():
            entry = self.entries.get(key)
            if entry and _failed(self.suites[key[:2]], map(_result, entry["cases"])):
                self._drop(key)
                failed.append(key[:2])
        self.save()
        return failed

    def save(self) -> None:
        """Merge this run's changes into the file, under its lock: another run may have saved meanwhile."""
        if not (self.changed or self.dropped):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            merged = _read(self.path)
            for key in self.dropped:
                merged.pop(key, None)
            for key in self.changed:
                merged[key] = self.entries[key]
            write_whole(self.path, {"suites": list(merged.values())} if merged else {})
        self.entries = merged
        self.changed, self.dropped = set(), set()

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self.path.with_name(f"{self.path.name}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _failed(suite: Suite, results: Iterable[CaseResult]) -> bool:
    """Whether a default case failed: a fact case's failure is a fact, not the suite's."""
    return any(result.outcome is Outcome.FAIL and not opt_in(suite, result.id) for result in results)


def _saved(result: CaseResult) -> dict:
    case = asdict(result) | {"outcome": str(result.outcome), "entity_ids": list(result.entity_ids)}
    for transient in ("unreachable", "devices"):  # never saved; a fact's devices go to _facts.md
        case.pop(transient)
    return case


def _result(case: dict) -> CaseResult:
    return CaseResult(
        case["id"],
        case["role"],
        case["domain"],
        Outcome(case["outcome"]),
        case["detail"],
        tuple(case["entity_ids"]),
        case["twin"],
        absent=case.get("absent", False),
        classes=tuple(case.get("classes", ())),
        inapplicable=case.get("inapplicable", case.get("absent", False)),
        declared=tuple(declared) if (declared := case.get("declared")) else None,
        reached=tuple(Reached(**device) for device in case.get("reached", ())),
    )
