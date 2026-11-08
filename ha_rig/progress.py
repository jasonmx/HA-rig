"""The ledger: each suite's finished cases, on disk, so its coverage builds up over any number of runs.

A run saves each case the moment it finishes, and the facts each part of a suite took (HA's version, the devices behind
its entities), in `deviceside-rig-progress/<profile>.json` in deviceside's git common dir: one file per profile, shared
by every worktree. Its entries are per suite, keyed by the suite's fingerprint (ha_rig/coverage.py), so a suite's saved
cases hold only while the code they prove is unchanged, and runs of different code sit side by side. A later run joins
a suite's saved results to its own, so a suite finished across runs writes its record.

An entry goes when:
- HA runs another version than its cases ran on: a record names one;
- its suite's record is written, which carries its coverage from then on;
- the run that saved a failure of one of its cases ends: the suite reruns whole, so a record never covers up a failure;
- nothing has touched it for PRUNE_DAYS.

**Firmware facts.** A case whose test is a `firmware_fact` proves a device behavior, not the adapter's code, so its
pass is also kept apart from the entries, keyed by suite and case alone, with the devices its suite's part ran on. It
holds across code changes and HA versions, and outlives its suite's record, while the test is as written then and no
device of the same model now shows another firmware: coverage lends it to the suite's entry at its fingerprint now,
devices and all, so the next record carries it and names their firmware. It goes when the case fails or runs again,
its test changes, the profile moves it to other entities, or a later part of its suite shows that model at another
firmware; never for its age.

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

from .engine import CaseResult, Device, Outcome, Suite, SuiteResult
from .journal import write_whole

DIRECTORY = "deviceside-rig-progress"
PRUNE_DAYS = 30

Done = dict[tuple[str, str, str], CaseResult]  # (integration, suite name, case id): its result
EntryKey = tuple[str, str, str]  # (integration, suite name, fingerprint)
FactKey = tuple[str, str, str]  # (integration, suite name, case id): a firmware fact's pass


def path_for(directory: Path, profile: str) -> Path:
    return directory / DIRECTORY / f"{profile}.json"


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _key(entry: dict) -> EntryKey:
    return entry["integration"], entry["suite"], entry["fingerprint"]


def _fact_key(fact: dict) -> FactKey:
    return fact["integration"], fact["suite"], fact["case"]["id"]


def _read(path: Path) -> tuple[dict[EntryKey, dict], dict[FactKey, dict]]:
    try:
        saved = json.loads(path.read_text()) if path.exists() else {}
        entries = {_key(entry): entry for entry in saved.get("suites", [])}
        return entries, {_fact_key(fact): fact for fact in saved.get("firmware", [])}
    except ValueError, KeyError, TypeError, AttributeError:
        return {}, {}


def _moved(saved: Iterable[dict], now: Iterable[Device]) -> bool:
    """Whether a device of a model the fact ran on now shows another firmware, and none of that model its own."""
    now = [asdict(device) for device in now]
    for device in saved:
        same_model = [d for d in now if (d["model"], d["model_id"]) == (device["model"], device["model_id"])]
        if same_model and device not in same_model:
            return True
    return False


def _old(entry: dict, now: datetime.datetime) -> bool:
    try:
        touched = datetime.datetime.fromisoformat(entry.get("touched", ""))
    except ValueError:
        return True
    return now - touched > datetime.timedelta(days=PRUNE_DAYS)


class Progress:
    """The profile's ledger, as this run sees it: every entry on disk, and the suites the run `track`s, each at its
    fingerprint now. Its changes are merged into the file on each save."""

    def __init__(
        self, path: Path, entries: dict[EntryKey, dict] | None = None, facts: dict[FactKey, dict] | None = None
    ):
        self.path = path
        self.entries: dict[EntryKey, dict] = dict(entries or {})
        self.firmware: dict[FactKey, dict] = dict(facts or {})  # Firmware facts, in the module docstring
        self.facts_changed: set[FactKey] = set()
        self.facts_dropped: set[FactKey] = set()
        self.tracked: dict[tuple[str, str], str] = {}  # (integration, suite name): its fingerprint now
        self.ha_version: str | None = None
        self.changed: set[EntryKey] = set()
        self.dropped: set[EntryKey] = set()

    @classmethod
    def load(cls, directory: Path, profile: str) -> Progress:
        """The ledger on disk, its entries untouched for PRUNE_DAYS dropped (from the file at the next save)."""
        path = path_for(directory, profile)
        progress = cls(path, *_read(path))
        if path.exists() and not (progress.entries or progress.firmware) and path.read_text().strip():
            print(f"The ledger {path} is unreadable, so every saved case reruns; the next save replaces it.")
        now = datetime.datetime.now()
        for key in [key for key, entry in progress.entries.items() if _old(entry, now)]:
            progress._drop(key)
        return progress

    def track(self, suite: Suite, fingerprint: str) -> None:
        """Follow `suite` at `fingerprint`: its saved cases there are the run's, and its new ones are saved there."""
        self.tracked[suite.integration, suite.name] = fingerprint

    def key(self, suite: Suite) -> EntryKey | None:
        if (fingerprint := self.tracked.get((suite.integration, suite.name))) is None:
            return None
        return suite.integration, suite.name, fingerprint

    def entry(self, suite: Suite) -> dict | None:
        """The tracked suite's entry at its fingerprint now, if it has one."""
        return self.entries.get(self.key(suite) or ("", "", ""))

    def __bool__(self) -> bool:
        return bool(self.done())

    def tracked_keys(self) -> list[EntryKey]:
        return [(integration, name, fingerprint) for (integration, name), fingerprint in self.tracked.items()]

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
                "fingerprint": key[2],
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
        """Save one finished case, replacing any earlier result of it; an unreachable skip is never saved, as it
        proves nothing."""
        if result.unreachable:
            return
        entry = self._entry(suite)
        case = _saved(result)
        entry["cases"] = [saved for saved in entry["cases"] if saved["id"] != result.id] + [case]
        if (test := suite.test(result.id)) is not None and test.firmware_fact:
            self._drop_fact((suite.integration, suite.name, result.id))
            if result.outcome is Outcome.PASS:
                # Its devices come with the part's facts; until then it lends nothing (fact_results).
                fact = {"integration": suite.integration, "suite": suite.name, "case": case, "test": test.digest}
                self._set_fact(fact | {"devices": None})
        self.save()

    def facts(self, suite: Suite, ha_version: str | None, devices: Iterable[Device]) -> None:
        """Save the facts a part of `suite` took, joined to those of its earlier parts."""
        entry = self._entry(suite)
        entry["ha_version"] = entry["ha_version"] or ha_version
        devices = list(devices)
        for device in map(asdict, devices):
            if device not in entry["devices"]:
                entry["devices"].append(device)
        for key, fact in list(self.firmware.items()):
            if key[:2] != (suite.integration, suite.name):
                continue
            if fact["devices"] is None:
                if devices:  # a failed lookup names none: the fact stays pending, lending nothing
                    self._set_fact(fact | {"devices": [asdict(device) for device in devices]})
            elif _moved(fact["devices"], devices):
                self._drop_fact(key)
                # Not run this run (that would have left it pending), so the entry holds it lent, at the old
                # firmware, which the record would name: it reruns instead.
                now = [asdict(device) for device in devices]
                entry["cases"] = [case for case in entry["cases"] if case["id"] != key[2]]
                entry["devices"] = [d for d in entry["devices"] if d in now or d not in fact["devices"]]
        self.save()

    def fact_results(self, suite: Suite) -> dict[str, tuple[CaseResult, list[Device]]]:
        """The tracked suite's firmware facts that its tests still hold as written, by case id: each pass, with the
        devices it ran on."""
        found = {}
        for (integration, name, case_id), fact in self.firmware.items():
            test = suite.test(case_id)
            if (integration, name) != (suite.integration, suite.name) or fact["devices"] is None:
                continue
            if test is not None and test.firmware_fact and fact["test"] == test.digest:
                found[case_id] = (_result(fact["case"]), [Device(**device) for device in fact["devices"]])
        return found

    def lend(self, suite: Suite, results: Iterable[tuple[CaseResult, list[Device]]]) -> None:
        """Put firmware facts' passes, with their devices, into the tracked suite's entry at its fingerprint now, as
        if run there: its record carries them. Written at the next save."""
        results = list(results)
        if not results:
            return
        entry = self._entry(suite)
        lent = {result.id for result, _ in results}
        entry["cases"] = [case for case in entry["cases"] if case["id"] not in lent]
        for result, devices in results:
            entry["cases"].append(_saved(result))
            entry["devices"] += [d for d in map(asdict, devices) if d not in entry["devices"]]

    def forget_facts(self, suite: Suite, case_ids: Iterable[str]) -> None:
        """Drop the suite's firmware facts that no longer hold (their entities changed), until the next save."""
        for case_id in case_ids:
            self._drop_fact((suite.integration, suite.name, case_id))

    def _set_fact(self, fact: dict) -> None:
        key = _fact_key(fact)
        self.firmware[key] = fact | {"touched": _now()}
        self.facts_changed.add(key)
        self.facts_dropped.discard(key)

    def _drop_fact(self, key: FactKey) -> None:
        self.firmware.pop(key, None)
        self.facts_dropped.add(key)
        self.facts_changed.discard(key)

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
        """Drop the suite's entry at its fingerprint now (--fresh, or its record written), until the next save."""
        if (key := self.key(suite)) is not None:
            self._drop(key)

    def _drop(self, key: EntryKey) -> None:
        self.entries.pop(key, None)
        self.changed.discard(key)
        self.dropped.add(key)

    def drop_failed(self) -> list[tuple[str, str]]:
        """At the run's end: drop each tracked suite holding a failure, so it reruns whole; saved. Returns them."""
        failed = []
        for key in self.tracked_keys():
            entry = self.entries.get(key)
            if entry and any(case["outcome"] == Outcome.FAIL for case in entry["cases"]):
                self._drop(key)
                failed.append(key[:2])
        self.save()
        return failed

    def save(self) -> None:
        """Merge this run's changes into the file, under its lock: another run may have saved meanwhile."""
        if not (self.changed or self.dropped or self.facts_changed or self.facts_dropped):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            merged, facts = _read(self.path)
            for key in self.dropped:
                merged.pop(key, None)
            for key in self.changed:
                merged[key] = self.entries[key]
            for key in self.facts_dropped:
                facts.pop(key, None)
            for key in self.facts_changed:
                facts[key] = self.firmware[key]
            saved = {"suites": list(merged.values())} if merged else {}
            write_whole(self.path, saved | ({"firmware": list(facts.values())} if facts else {}))
        self.entries, self.firmware = merged, facts
        self.changed, self.dropped = set(), set()
        self.facts_changed, self.facts_dropped = set(), set()

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self.path.with_name(f"{self.path.name}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _saved(result: CaseResult) -> dict:
    case = asdict(result) | {"outcome": str(result.outcome), "entity_ids": list(result.entity_ids)}
    case.pop("unreachable")
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
    )
