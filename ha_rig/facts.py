"""The facts the fact cases found, kept per integration in `_facts.md`: dated, append-only.

A fact case (engine.Case.fact: the watched case of a test with `observe:`, or any case of a `firmware_fact` test) runs
only with --facts. Each that finds something appends one row as it ends: the date, the case, its kind, which says how
the fact was seen (`automated` for a firmware fact's automated case that passed or failed, `observed` for a watched case
a person answered), the devices it ran on with their firmware, and `pass` or `fail`. A row is a fact written down: it
stands until evidence contradicts it (deviceside's docs/evidence.md, Verify, then trust). The file is only ever appended
to, never rewritten, so no run erases a fact and a line the rig can't read stays as it is. A test record lists fact
cases as skipped and points here, quoting the latest row on file for each; the rig decides nothing from the rows.

Pure: no HA, no files but the one it reads and appends to.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

FILE = "_facts.md"  # deviceside's record readers take only <action>(-<option>).md, so they skip this
PASS, FAIL = "pass", "fail"
AUTOMATED, OBSERVED = "automated", "observed"  # how a fact was seen
HEADER = "| Date | Test | Kind | Devices | Result |"
ROW = re.compile(r"^\|(.*)\|$")


@dataclass(frozen=True, slots=True)
class Fact:
    """One row of `_facts.md`."""

    date: str  # ISO
    test: str  # the case's id
    kind: str  # how it was seen: AUTOMATED or OBSERVED
    devices: str  # the devices it ran on, as a record's Device row names them
    result: str  # PASS or FAIL

    def __str__(self) -> str:
        return f"last on file {self.date}, on {self.devices}: {self.result}"

    def row(self) -> str:
        cells = [self.date, f"`{self.test}`", self.kind, self.devices, self.result]
        return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


def heading(title: str) -> str:
    """What a new file starts with: its title, a sentence and the table's header."""
    return (
        f"# {title} · facts\n\n"
        "What the rig's fact cases found, dated. Rows are only ever added: each is a fact, which stands until evidence "
        f"contradicts it.\n\n{HEADER}\n|---|---|---|---|---|\n"
    )


def append(path: Path, title: str, fact: Fact) -> None:
    """Append `fact` to the file at `path`, starting it with its heading when it is new or empty."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        if file.tell() == 0:
            file.write(heading(title))
        file.write(fact.row() + "\n")


def _cells(line: str) -> list[str]:
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", ROW.match(line)[1])]


@dataclass(frozen=True, slots=True)
class FactsFile:
    """An integration's `_facts.md` as read: the rows it could parse, in file order."""

    rows: tuple[Fact, ...] = field(default_factory=tuple)

    @classmethod
    def read(cls, path: Path) -> FactsFile:
        """The file at `path`, empty when there is none; a line that isn't a five-cell row is left out."""
        if not path.is_file():
            return cls()
        rows = []
        for line in path.read_text().splitlines():
            if ROW.match(line) and line != HEADER and not line.startswith("|---") and len(cells := _cells(line)) == 5:
                date, test, kind, devices, result = cells
                rows.append(Fact(date, test.strip("`"), kind, devices, result))
        return cls(tuple(rows))

    def latest(self, test: str) -> Fact | None:
        """The last row on file for the case `test`."""
        return next((row for row in reversed(self.rows) if row.test == test), None)
