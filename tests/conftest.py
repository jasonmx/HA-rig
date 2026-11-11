"""Fixtures every rig test shares: no real cmux, no settle waits, no wait between a Z2M bulb's first reads, no timers
left running, and no household terms written into the deviceside checkout the tests import."""

import asyncio
import sys
from collections.abc import Iterator

import pytest

from ha_rig import engine, locks, roam, target, terms
from ha_rig.target import deviceside_repo
from tests.rig_fakes import PENDING

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
def no_settle(monkeypatch) -> None:
    """No settle wait before a run's first case or after it puts a device back; a test that needs one sets its own."""
    monkeypatch.setattr(engine, "SETUP_SETTLE_S", 0)
    monkeypatch.setattr(engine, "RESTORE_SETTLE_S", 0)


@pytest.fixture(autouse=True)
async def cancel_pending():
    """Device timers and ramps outlive the run that started them; HA's test plugin fails on lingering ones."""
    yield
    for pending in PENDING:
        pending.cancel()
    await asyncio.gather(*(task for task in PENDING if isinstance(task, asyncio.Task)), return_exceptions=True)
    PENDING.clear()


@pytest.fixture(autouse=True)
def no_terms_written(monkeypatch) -> None:
    """A run writes the household terms into deviceside's main checkout; a test's run doesn't."""
    monkeypatch.setattr(terms, "write", lambda repo, found=None: repo)


REAL_REFUSE_MOVED_FILES = target.refuse_moved_files  # the autouse fixture swaps the module's for a no-op


@pytest.fixture(autouse=True)
def no_moved_files_check(monkeypatch) -> None:
    """The real deviceside checkout the tests import may still hold a household file the rig refuses."""
    monkeypatch.setattr(target, "refuse_moved_files", lambda repo, rig_checkout: None)


REAL_REFUSE_UNOWNED = target.refuse_unowned_checkout


@pytest.fixture(autouse=True)
def no_ownership_check(monkeypatch) -> None:
    """The checkout the tests run from is not a PR's worktree in CI, and no test shells out to gh."""
    monkeypatch.setattr(target, "refuse_unowned_checkout", lambda repo, env=None: None)


@pytest.fixture(autouse=True)
def no_approval(monkeypatch) -> None:
    """No test inherits an approval for restricted work, or the variable it replaced, from the shell running it."""
    monkeypatch.delenv("DEVICE_SIDE_RIG_RESTRICTED", raising=False)
    monkeypatch.delenv("DEVICE_SIDE_RIG_APPROVED", raising=False)


@pytest.fixture(autouse=True)
def dispatched_scope(monkeypatch, request) -> None:
    """A case run without the admin connection draws no scope event, and a record without one is not written
    (write_records). Most tests are not about that: give each passing case the one event DeviceSide's dispatcher
    would fire. `@pytest.mark.untraced` keeps a test's cases as the run left them."""
    if request.node.get_closest_marker("untraced"):
        return
    attach = engine._CaseRun.attach

    async def attach_with_scope(self, result, slots, traced) -> None:
        await attach(self, result, slots, traced)
        if result.outcome is engine.Outcome.PASS and not result.scope:
            suite = self.case.suite
            result.scope = ((suite.action, suite.integration, result.domain or "", None, ()),)

    monkeypatch.setattr(engine._CaseRun, "attach", attach_with_scope)
