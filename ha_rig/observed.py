"""Observed call patterns: what a person saw a maker's devices do, kept per integration in `_observed.md`.

An observed run proves a call pattern, not a test: from a starting state, a maker's devices respond as seen to a
sequence of calls, and to any sequence it begins with (a device can't know which calls are still to come). So a pass
stands, whichever test sends the pattern, across firmware, HA and code; an observed failure on a model blocks lends to
that model for sequences that begin with the failed one, until a newer pass on that model covers them.

A call is `(transport, command, params)` as deviceside's wire trace fires it (`deviceside_wire`), normalized and
hashed to six hex characters. A sequence is, per slot (a role or member slot, never an entity ID), the ordered call
hashes with consecutive repeats collapsed; interleaving between slots and timing are ignored.

Pure: no HA, no files but the one `ObservedFile` reads and renders.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FILE = "_observed.md"  # check_release.py --records reads only <action>(-<option>).md, so it skips this
PASS, FAIL = "pass", "fail"

type Call = tuple[str, str, Any]  # (transport, command, params)
type Sequence = tuple[tuple[str, tuple[str, ...]], ...]  # ((slot, (hash, ...)), ...), sorted by slot


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def short_hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()[:6]


def _sign(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return "0" if value == 0 else "+" if value > 0 else "-"
    if isinstance(value, Mapping):
        return {key: _sign(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_sign(item) for item in value]
    return value


def normalize(call: Call) -> Call:
    """The call as hashed: every number reduced to its sign (`+`, `-`, `0`), since the values a light is stopped at
    jitter between runs (timing), while whether a field is set, zero or negative does not. Keys, strings and booleans
    stay exact."""
    transport, command, params = call
    return transport, command, _sign(params)


def render_call(call: Call) -> str:
    """One call on a line: its transport, command and canonical params."""
    transport, command, params = call
    return f"{transport} {command} {_canonical(params)}"


def call_hash(call: Call) -> str:
    return short_hash(list(normalize(call)))


def sequence(calls: Iterable[tuple[str, Call]]) -> tuple[Sequence, dict[str, Call]]:
    """The sequence of `(slot, call)` pairs in the order sent, and the legend from each hash to the
    call as last sent (exact, not normalized)."""
    by_slot: dict[str, list[str]] = {}
    legend: dict[str, Call] = {}
    for slot, call in calls:
        digest = call_hash(call)
        legend[digest] = call
        hashes = by_slot.setdefault(slot, [])
        if not hashes or hashes[-1] != digest:
            hashes.append(digest)
    return tuple(sorted((slot, tuple(hashes)) for slot, hashes in by_slot.items())), legend


def begins(prefix: Sequence, whole: Sequence) -> bool:
    """Whether `whole` begins with `prefix`: the same slots, and each slot's calls in `prefix` a prefix of (or equal
    to) its calls in `whole`."""
    mine, theirs = dict(prefix), dict(whole)
    return mine.keys() == theirs.keys() and all(theirs[slot][: len(calls)] == calls for slot, calls in mine.items())


@dataclass(frozen=True, slots=True)
class Device:
    maker: str
    model: str
    firmware: str


@dataclass(frozen=True, slots=True)
class Trace:
    """What one case sent: its starting state's hash, its sequence, the legend of its calls and the devices behind its
    entities. Saved in the ledger with its case, so a resumed run lends and records as a whole one would."""

    state: str
    sequence: Sequence
    legend: dict[str, Call] = field(compare=False, hash=False)
    devices: tuple[Device, ...]

    def rows(self, observed_in: str, date: str, passed: bool) -> list[Observation]:
        """One row per device: what a person saw this case's calls do."""
        result = PASS if passed else FAIL
        return [Observation(device, self.state, self.sequence, observed_in, date, result) for device in self.devices]

    def to_json(self) -> dict:
        return {
            "state": self.state,
            "sequence": [[slot, list(hashes)] for slot, hashes in self.sequence],
            "legend": {digest: list(call) for digest, call in self.legend.items()},
            "devices": [[device.maker, device.model, device.firmware] for device in self.devices],
        }

    @classmethod
    def from_json(cls, data: dict) -> Trace:
        return cls(
            data["state"],
            tuple((slot, tuple(hashes)) for slot, hashes in data["sequence"]),
            {digest: tuple(call) for digest, call in data["legend"].items()},
            tuple(Device(*device) for device in data["devices"]),
        )


@dataclass(frozen=True, slots=True)
class Observation:
    """One row of `_observed.md`: a call pattern seen on a device, and what was seen."""

    device: Device
    state: str  # the starting state's hash
    sequence: Sequence
    observed_in: str  # the case id, for information only
    date: str  # ISO
    result: str  # PASS or FAIL

    @property
    def key(self) -> tuple[Device, str, Sequence]:
        return self.device, self.state, self.sequence


def covers(row: Observation, state: str, candidate: Sequence) -> bool:
    return row.result == PASS and row.state == state and begins(candidate, row.sequence)


def blocks(row: Observation, state: str, candidate: Sequence) -> bool:
    return row.result == FAIL and row.state == state and begins(row.sequence, candidate)


def lend(rows: Iterable[Observation], state: str, candidate: Sequence, devices: Iterable[Device]) -> Observation | None:
    """The pass row that lends `candidate`, from `state`, to a case on `devices`, or None. A pass lends when it covers
    the candidate and every device has its maker, and no fail on a case device's model blocks the candidate unless a
    pass on that model covering it is newer. Of the lending rows, the latest."""
    rows, devices = list(rows), list(devices)
    if not devices or not candidate:
        return None
    for model in {device.model for device in devices}:
        on_model = [row for row in rows if row.device.model == model]
        failed = max((row.date for row in on_model if blocks(row, state, candidate)), default=None)
        passed = max((row.date for row in on_model if covers(row, state, candidate)), default=None)
        if failed is not None and (passed is None or passed <= failed):
            return None
    makers = {device.maker for device in devices}
    lending = [row for row in rows if covers(row, state, candidate) and makers == {row.device.maker}]
    return max(lending, key=lambda row: row.date, default=None)


# --- The file ------------------------------------------------------------------------------------------------


HEADER = "| Maker | Model | Firmware | Starting state | Sequence | Observed in | Date | Result |"
LEGEND_HEADER = "| Call | Transport | Command | Params |"
ROW = re.compile(r"^\|(.*)\|$")


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _cells(line: str) -> list[str]:
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", ROW.match(line)[1])]


def _code(text: str) -> str:
    return text.removeprefix("`").removesuffix("`")


def render_sequence(seq: Sequence) -> str:
    return "<br>".join(f"`{slot}: {' '.join(hashes)}`" for slot, hashes in seq)


def parse_sequence(text: str) -> Sequence:
    slots = []
    for part in text.split("<br>"):
        slot, _, hashes = _code(part.strip()).partition(": ")
        slots.append((slot, tuple(hashes.split())))
    return tuple(sorted(slots))


@dataclass
class ObservedFile:
    """An integration's `_observed.md`: its rows, and the legend from each call hash to its call."""

    title: str
    rows: list[Observation] = field(default_factory=list)
    legend: dict[str, Call] = field(default_factory=dict)

    @classmethod
    def read(cls, path: Path, title: str) -> ObservedFile:
        """The file at `path`, or an empty one titled `title` when there is none."""
        if not path.is_file():
            return cls(title)
        observed = cls(title)
        section = None
        for line in path.read_text().splitlines():
            if line.startswith("## "):
                section = line[3:].strip()
            elif not ROW.match(line) or line.startswith("|---") or line in (HEADER, LEGEND_HEADER):
                continue
            elif section == "Observed":
                maker, model, firmware, state, seq, observed_in, date, result = _cells(line)
                device = Device(maker, model, _code(firmware))
                observed.rows.append(
                    Observation(device, _code(state), parse_sequence(seq), _code(observed_in), date, result)
                )
            elif section == "Calls":
                digest, transport, command, params = _cells(line)
                observed.legend[_code(digest)] = (transport, _code(command), json.loads(_code(params)))
        return observed

    def add(self, rows: Iterable[Observation], legend: Mapping[str, Call]) -> None:
        """Add `rows`, each replacing a row with its key (so only its date, observed-in and result change), and their
        calls to the legend. Nothing is pruned: a pattern can come back."""
        for new in rows:
            self.rows = [row for row in self.rows if row.key != new.key] + [new]
            for _, hashes in new.sequence:
                for digest in hashes:
                    if digest in legend:
                        self.legend[digest] = legend[digest]

    def render(self) -> str:
        lines = [
            f"# {self.title} · observed",
            "",
            "Call patterns a person watched a maker's devices respond to. A pass proves that the maker's devices, "
            "started from that state, respond as seen to that call sequence and to any sequence it begins with, "
            "whichever test sends it; it stands across firmware, Home Assistant and DeviceSide versions. A failure "
            "blocks the model it was seen on for sequences that begin with the failed one, until a newer pass covers "
            "them. Calls are listed by hash under Calls.",
            "",
            "## Observed",
            "",
            HEADER,
            "|---|---|---|---|---|---|---|---|",
        ]
        order = sorted(self.rows, key=lambda row: (row.device.maker, row.device.model, row.observed_in, row.date))
        for row in order:
            device = row.device
            cells = [
                device.maker,
                device.model,
                f"`{device.firmware}`",
                f"`{row.state}`",
                render_sequence(row.sequence),
                f"`{row.observed_in}`",
                row.date,
                row.result,
            ]
            lines.append("| " + " | ".join(map(_cell, cells)) + " |")
        lines += ["", "## Calls", "", LEGEND_HEADER, "|---|---|---|---|"]
        used = {digest for row in self.rows for _, hashes in row.sequence for digest in hashes}
        for digest in sorted(used & self.legend.keys()):
            transport, command, params = self.legend[digest]
            cells = [f"`{digest}`", transport, f"`{command}`", f"`{_canonical(params)}`"]
            lines.append("| " + " | ".join(map(_cell, cells)) + " |")
        return "\n".join(lines) + "\n"

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render())
