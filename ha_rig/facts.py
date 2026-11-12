"""The facts the opt-in cases found, kept per integration in `_facts.md`: dated, append-only.

An observed case a person answered, and a firmware fact that passed or failed, each add one row: the date, the case,
its kind, the devices it ran on with their firmware, and `pass` or `fail`. A row is a fact written down: it stands
until evidence contradicts it (deviceside's docs/evidence.md, Verify, then trust), so a default run skips the opt-in
cases and never touches the file, and rows are only ever added. A test record lists an opt-in case as skipped and
points here, quoting the latest row on file for it; the rig decides nothing from the rows.

Pure: no HA, no files but the one `FactsFile` reads and writes.
"""

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

FILE = "_facts.md"  # deviceside's record readers take only <action>(-<option>).md, so they skip this
PASS, FAIL = "pass", "fail"
OBSERVED, FIRMWARE_FACT = "observed", "firmware fact"  # the kinds
HEADER = "| Date | Test | Kind | Devices | Result |"
ROW = re.compile(r"^\|(.*)\|$")


@dataclass(frozen=True, slots=True)
class Fact:
    """One row of `_facts.md`."""

    date: str  # ISO
    test: str  # the case's id
    kind: str  # OBSERVED or FIRMWARE_FACT
    devices: str  # the devices it ran on, as a record's Device row names them
    result: str  # PASS or FAIL

    def __str__(self) -> str:
        return f"last on file {self.date}: {self.result}"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _cells(line: str) -> list[str]:
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", ROW.match(line)[1])]


@dataclass
class FactsFile:
    """An integration's `_facts.md`: its rows, in the order they were added."""

    title: str
    rows: list[Fact] = field(default_factory=list)

    @classmethod
    def read(cls, path: Path, title: str) -> FactsFile:
        """The file at `path`, or an empty one titled `title` when there is none."""
        facts = cls(title)
        if not path.is_file():
            return facts
        for line in path.read_text().splitlines():
            if not ROW.match(line) or line == HEADER or line.startswith("|---"):
                continue
            if len(cells := _cells(line)) == 5:
                date, test, kind, devices, result = cells
                facts.rows.append(Fact(date, test.strip("`"), kind, devices, result))
        return facts

    def add(self, rows: Iterable[Fact]) -> None:
        """Append `rows`, leaving out any the file already holds word for word."""
        self.rows += [row for row in dict.fromkeys(rows) if row not in self.rows]

    def latest(self, test: str) -> Fact | None:
        """The latest row on file for the case `test`: the newest date, and of one date the last added."""
        latest = None
        for row in self.rows:
            if row.test == test and (latest is None or row.date >= latest.date):
                latest = row
        return latest

    def render(self) -> str:
        lines = [
            f"# {self.title} · facts",
            "",
            "What the rig's observed tests and firmware facts found, dated. Rows are only ever added: each is a fact, "
            "which stands until evidence contradicts it.",
            "",
            HEADER,
            "|---|---|---|---|---|",
        ]
        for row in self.rows:
            cells = [row.date, f"`{row.test}`", row.kind, row.devices, row.result]
            lines.append("| " + " | ".join(map(_cell, cells)) + " |")
        return "\n".join(lines) + "\n"

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.tmp")
        temp.write_text(self.render())
        os.replace(temp, path)
