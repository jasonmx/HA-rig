"""A run's coverage of DeviceSide's `core/`, measured in the live HA process by the rig-only `rig_coverage` component
(ha_component/rig_coverage) and written into each record as deviceside's `## Shared coverage` section, so one passing
record can clear a `core/` change for every adapter (deviceside `scripts/check_release.py`, "Shared code").

The section is best-effort: a helper that is absent or fails, or lines that are empty, mean no section and a reason
printed, never an empty table.
"""

import re
from collections.abc import Mapping, Sequence

from .client import HAClient

DOMAIN = "rig_coverage"
CORE = re.compile(r"custom_components/deviceside/(core/[\w/]+\.py)$")
HEADING = "## Shared coverage"
HEADER = ("| Module | Executed lines |", "|---|---|")

Rows = list[tuple[str, str]]  # (`core/<module>.py`, `12-40, 55`)


def ranges(lines: Sequence[int]) -> str:
    """`12-40, 55`: the sorted lines, runs collapsed."""
    parts: list[str] = []
    ordered = sorted(set(lines))
    start = 0
    while start < len(ordered):
        end = start
        while end + 1 < len(ordered) and ordered[end + 1] == ordered[end] + 1:
            end += 1
        parts.append(str(ordered[start]) if end == start else f"{ordered[start]}-{ordered[end]}")
        start = end + 1
    return ", ".join(parts)


def rows(executed: Mapping[str, Sequence[int]]) -> Rows:
    """Each `core/` module that executed a line, by module; paths elsewhere and modules with no lines are dropped."""
    found = {
        match[1]: ranges(lines)
        for path, lines in executed.items()
        if lines and (match := CORE.search(path.replace("\\", "/")))
    }
    return sorted(found.items())


def section(rows_: Rows) -> list[str]:
    """The record's section, led by a blank line; empty for no rows."""
    if not rows_:
        return []
    return ["", HEADING, "", *HEADER, *(f"| `{module}` | {lines} |" for module, lines in rows_)]


async def start(admin: HAClient) -> str | None:
    """Begin measuring from empty; None when it began, else why it did not."""
    try:
        await admin.call_service(DOMAIN, "start")
    except Exception as err:  # noqa: BLE001 - a missing helper is the common case; the run goes on without it
        return f"{DOMAIN}.start failed ({err})"
    return None


async def stop(admin: HAClient) -> tuple[Rows, str | None]:
    """Stop measuring: the rows, and why there are none (None when there are some)."""
    try:
        response = await admin.call_service(DOMAIN, "stop", return_response=True)
    except Exception as err:  # noqa: BLE001
        return [], f"{DOMAIN}.stop failed ({err})"
    if not (found := rows(response or {})):
        return [], "the helper returned no core/ lines"
    return found, None
