"""The facts the fact cases found, kept per integration in `_facts.md`, or `_facts.json` in a checkout whose records
are JSON (ha_rig/schema.py): dated, append-only.

A fact case (engine.Case.fact: the watched case of a test with `observe:`, or any case of a `firmware_fact` test) runs
only with --facts. Each that finds something appends one row as it ends: the date, the suite, the case, its kind, which
says how the fact was seen (`automated` for a firmware fact's automated case that passed or failed, `observed` for a
watched case a person answered), the devices it ran on with their firmware, and `pass` or `fail`. A row is a fact
written down: it stands until evidence contradicts it (deviceside's docs/evidence.md, Verify, then trust). The file is
only ever appended to, never rewritten, so no run erases a fact and a line the rig can't read stays as it is. A test
record lists fact cases as skipped and points here, quoting the latest row on file for each; the rig decides nothing
from the rows.

`_facts.json` holds the same rows as deviceside's schema says (`facts`), each device with its model ID, firmware and
class. A row is added by rewriting the file with every row already there left as it is, a converted one's `name`
included; a file the rig can't read as one is left alone. A JSON record quotes no fact.

Pure: no HA, no files but the one it reads and appends to.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .schema import FORMAT, RecordSchema

FILE = "_facts.md"  # deviceside's record readers take only <action>(-<option>).md, so they skip this
JSON_FILE = "_facts.json"  # and its readers of JSON records skip a file named with a leading `_`
PASS, FAIL = "pass", "fail"
AUTOMATED, OBSERVED = "automated", "observed"  # how a fact was seen
HEADER = "| Date | Suite | Test | Kind | Devices | Result |"
ROW = re.compile(r"^\|(.*)\|$")


@dataclass(frozen=True, slots=True)
class Fact:
    """One row of `_facts.md`."""

    date: str  # ISO
    suite: str  # the suite's name (`set_auto_off`, `stop-predicted_state`): case ids repeat across suites
    test: str  # the case's id
    kind: str  # how it was seen: AUTOMATED or OBSERVED
    devices: str  # the devices it ran on, as a record's Device row names them
    result: str  # PASS or FAIL

    def __str__(self) -> str:
        return f"last on file {self.date}, on {self.devices}: {self.result}"

    def row(self) -> str:
        cells = [self.date, f"`{self.suite}`", f"`{self.test}`", self.kind, self.devices, self.result]
        return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


def heading(title: str) -> str:
    """What a new file starts with: its title, a sentence and the table's header."""
    return (
        f"# {title} · facts\n\n"
        "What the rig's fact cases found, dated. Rows are only ever added: each is a fact, which stands until evidence "
        f"contradicts it.\n\n{HEADER}\n|---|---|---|---|---|---|\n"
    )


def append(path: Path, title: str, fact: Fact) -> None:
    """Append `fact` to the file at `path`, starting it with its heading when it is new or empty."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        if file.tell() == 0:
            file.write(heading(title))
        file.write(fact.row() + "\n")


def append_json(path: Path, integration: str, row: dict, schema: RecordSchema) -> None:
    """Add `row` to the `_facts.json` at `path`, starting it when there is none, and rewrite the file once the schema
    takes it, every row already there as it was. Raises ValueError, writing nothing, when the file there isn't one
    the rig can read, or the schema refuses the result (InvalidRecord)."""
    if path.is_file():
        document = json.loads(path.read_text())
        if not isinstance(document, dict) or not isinstance(document.get("facts"), list):
            raise ValueError(f"{path.name} holds no list of facts")
    else:
        document = {"format": FORMAT, "integration": integration, "facts": []}
    document["facts"].append(row)
    schema.write(path, document)


def _cells(line: str) -> list[str]:
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", ROW.match(line)[1])]


@dataclass(frozen=True, slots=True)
class FactsFile:
    """An integration's `_facts.md` as read: the rows it could parse, in file order."""

    rows: tuple[Fact, ...] = field(default_factory=tuple)

    @classmethod
    def read(cls, path: Path) -> FactsFile:
        """The file at `path`, empty when there is none; a line that isn't a six-cell row is left out, and so is a
        `_facts.json` row that isn't a fact (or the whole file, when it isn't JSON)."""
        if not path.is_file():
            return cls()
        if path.suffix == ".json":
            return cls._read_json(path)
        rows = []
        for line in path.read_text().splitlines():
            if ROW.match(line) and line != HEADER and not line.startswith("|---") and len(cells := _cells(line)) == 6:
                date, suite, test, kind, devices, result = cells
                rows.append(Fact(date, suite.strip("`"), test.strip("`"), kind, devices, result))
        return cls(tuple(rows))

    @classmethod
    def _read_json(cls, path: Path) -> FactsFile:
        try:
            listed = json.loads(path.read_text()).get("facts", [])
        except ValueError, AttributeError:
            return cls()
        rows = []
        for row in listed if isinstance(listed, list) else []:
            try:
                devices = "; ".join(_device(device) for device in row["devices"])
                rows.append(Fact(row["date"], row["suite"], row["test"], row["kind"], devices, row["result"]))
            except KeyError, TypeError:
                continue
        return cls(tuple(rows))

    def latest(self, suite: str, test: str) -> Fact | None:
        """The last row on file for the case `test` of the suite named `suite`."""
        return next((row for row in reversed(self.rows) if (row.suite, row.test) == (suite, test)), None)


def _device(device: dict) -> str:
    """A `_facts.json` row's device as a `_facts.md` row names one."""
    return f"`{device['model']}`, firmware `{device.get('firmware') or 'unknown'}`"
