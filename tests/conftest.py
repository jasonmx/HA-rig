"""Fixtures every rig test shares: no real cmux, no wait between a Z2M bulb's first reads, and no household terms
written into the device-side checkout the tests import."""

import sys
from collections.abc import Iterator

import pytest

from ha_rig import locks, roam, terms
from ha_rig.target import device_side_repo

# The rig imports device-side's registry and actions; appended, so this repo's own `tests` package wins.
sys.path.append(str(device_side_repo()))

REAL_WORKSPACE_COLOR = locks.workspace_color  # the autouse fixture swaps the module's for a recorder


@pytest.fixture(autouse=True, scope="session")
def outside_cmux() -> Iterator[None]:
    """Keep the real cmux out of reach where a test's monkeypatch.undo() also drops workspace_colors' recorder."""
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("CMUX_WORKSPACE_ID", raising=False)
        yield


@pytest.fixture(autouse=True)
def workspace_colors(monkeypatch) -> list[str | None]:
    """Record the cmux workspace colors a run sets, so no test reaches the real cmux."""
    colors: list[str | None] = []
    monkeypatch.setattr(locks, "workspace_color", colors.append)
    return colors


@pytest.fixture(autouse=True)
def quick_prime(monkeypatch) -> None:
    """No wait between a Z2M bulb's first reads (Roamer.prime_z2m): the fakes don't answer device_side's get."""
    monkeypatch.setattr(roam, "PRIME_GAP_S", 0)


@pytest.fixture(autouse=True)
def no_terms_written(monkeypatch) -> None:
    """A run writes the household terms into device-side's main checkout; a test's run doesn't."""
    monkeypatch.setattr(terms, "write", lambda repo, found=None: repo)
