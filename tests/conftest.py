"""Fixtures every rig test shares: no real cmux, no wait between a Z2M bulb's first reads, and no household terms
written into the deviceside checkout the tests import."""

import sys
from collections.abc import Iterator

import pytest

from ha_rig import locks, roam, target, terms
from ha_rig.target import deviceside_repo

# The rig imports deviceside's registry and actions; appended, so this repo's own `tests` package wins.
sys.path.append(str(deviceside_repo()))

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
    """No wait between a Z2M bulb's first reads (Roamer.prime_z2m): the fakes don't answer DeviceSide's get."""
    monkeypatch.setattr(roam, "PRIME_GAP_S", 0)


@pytest.fixture(autouse=True)
def no_terms_written(monkeypatch) -> None:
    """A run writes the household terms into deviceside's main checkout; a test's run doesn't."""
    monkeypatch.setattr(terms, "write", lambda repo, found=None: repo)


REAL_REFUSE_MOVED_FILES = target.refuse_moved_files  # the autouse fixture swaps the module's for a no-op


@pytest.fixture(autouse=True)
def no_moved_files_check(monkeypatch) -> None:
    """The real deviceside checkout the tests import may still hold a household file the rig refuses."""
    monkeypatch.setattr(target, "refuse_moved_files", lambda repo, rig_checkout: None)


@pytest.fixture(autouse=True)
def no_approval(monkeypatch) -> None:
    """No test inherits an approval for restricted work, or the variable it replaced, from the shell running it."""
    monkeypatch.delenv("DEVICE_SIDE_RIG_RESTRICTED", raising=False)
    monkeypatch.delenv("DEVICE_SIDE_RIG_APPROVED", raising=False)
