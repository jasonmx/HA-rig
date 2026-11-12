"""What a person saw in an observed test, kept per integration in `_observed.md` as dated facts.

Each attended answer adds one row: the date, the case, the devices it ran on and `pass` or `fail`. A row is a fact
written down: it stands until evidence contradicts it (deviceside's docs/evidence.md, Verify, then trust), so nothing
reruns an observed test by itself. A maintainer asks for one with `--observed` when a change or a bug calls for it.

Pure: no HA, no files but the one `ObservedFile` reads and writes.
"""

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

FILE = "_observed.md"  # deviceside's record readers take only <action>(-<option>).md, so they skip this
PASS, FAIL = "pass", "fail"
HEADER = "| Date | Test | Devices | Result |"
ROW = re.compile(r"^\|(.*)\|$")


@dataclass(frozen=True, slots=True)
class Observation:
    """One row of `_observed.md`."""

    date: str  # ISO
    test: str  # the observed case's id
    devices: str  # the devices it ran on, as a record's Device row names them
    result: str  # PASS or FAIL


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _cells(line: str) -> list[str]:
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", ROW.match(line)[1])]


@dataclass
class ObservedFile:
    """An integration's `_observed.md`: its rows."""

    title: str
    rows: list[Observation] = field(default_factory=list)

    @classmethod
    def read(cls, path: Path, title: str) -> ObservedFile:
        """The file at `path`, or an empty one titled `title` when there is none."""
        observed = cls(title)
        if not path.is_file():
            return observed
        for line in path.read_text().splitlines():
            if not ROW.match(line) or line == HEADER or line.startswith("|---"):
                continue
            if len(cells := _cells(line)) == 4:
                date, test, devices, result = cells
                observed.rows.append(Observation(date, test.strip("`"), devices, result))
        return observed

    def add(self, rows: Iterable[Observation]) -> None:
        """Add `rows`, leaving out any the file already holds."""
        self.rows += [row for row in dict.fromkeys(rows) if row not in self.rows]

    def render(self) -> str:
        lines = [
            f"# {self.title} · observed",
            "",
            "What a person saw in an observed rig test, dated. Each row is a fact, which stands until evidence "
            "contradicts it.",
            "",
            HEADER,
            "|---|---|---|---|",
        ]
        for row in sorted(self.rows, key=lambda row: (row.test, row.date, row.devices)):
            cells = [row.date, f"`{row.test}`", row.devices, row.result]
            lines.append("| " + " | ".join(map(_cell, cells)) + " |")
        return "\n".join(lines) + "\n"

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.tmp")
        temp.write_text(self.render())
        os.replace(temp, path)
