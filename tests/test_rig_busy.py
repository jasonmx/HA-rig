"""Z2M's coordinator answering BUSY to a groupcast: the rig's `set` sent again, a DeviceSide call's case run again,
Z2M group cases paced, the log feed that renews itself and a group made only once Z2M counts its bulbs online. Against
the fakes, whose Z2M log is a feed the test writes BUSY lines into."""

import asyncio
import contextlib
import datetime
import json
from pathlib import Path

import pytest

from ha_rig import client, engine, roam
from ha_rig.client import MQTTFeed
from ha_rig.engine import CaseResult, Outcome, merge, run
from ha_rig.profile import RoamGroup
from ha_rig.progress import Progress
from ha_rig.roam import GroupError, Roamer
from ha_rig.runlogs import RunLogs
from tests import rig_fakes as fakes
from tests.rig_fakes import FakeHA, profile
from tests.test_rig_engine import REGISTRY, TOLERANCE, header, outcomes, suite

BUSY_LINE = json.dumps(
    {
        "level": "error",
        "namespace": "z2m",
        "message": "Publish 'set' 'state' to 'ds-rig-room' failed: 'Error: Failed to send: status=BUSY'",
    }
)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(engine, "BUSY_LAG_S", 0.05)
    monkeypatch.setattr(engine, "BUSY_RERUN_WAIT_S", 0)


class Room(FakeHA):
    """A Z2M group light.room of light.a. Each brightness `set` to it that the coordinator refuses logs BUSY and
    moves nothing; DeviceSide's `turn_on_for` is refused the same way while `refused_calls` lasts."""

    def __init__(self, refused_sets: int = 0, refused_calls: int = 0):
        super().__init__(
            {
                "light.room": {"state": "on", "attributes": {"entity_id": ["light.a"], "brightness": 255}},
                "light.a": {"state": "on", "attributes": {"brightness": 255}},
            },
            self.answer,
        )
        self.refused_sets, self.refused_calls = refused_sets, refused_calls
        self.sets: list[dict] = []

    def answer(self, _ha, action, data):
        if action == "get_attributes":
            return {}
        self.calls_seen = getattr(self, "calls_seen", 0) + 1
        if self.refused_calls > 0:
            self.refused_calls -= 1
            self.z2m_logging.put_nowait(BUSY_LINE)
            raise fakes.HAError({"code": "home_assistant_error", "translation_key": "targets_failed", "message": "x"})
        return {data["entity_id"]: {"status": "sent", "integration": "zigbee2mqtt"}}

    async def call_service(self, domain, service, data=None, return_response=False):
        if domain == "light" and service == "turn_on" and "brightness_pct" in (data or {}):
            self.calls.append((domain, service, dict(data)))
            self.sets.append(dict(data))
            if self.refused_sets > 0:
                self.refused_sets -= 1
                self.z2m_logging.put_nowait(BUSY_LINE)
                return None
            self.states["light.room"]["attributes"]["brightness"] = 153
            self.set("light.a", "on", brightness=153)
            return None
        return await super().call_service(domain, service, data, return_response)


async def run_room(tmp_path: Path, ha: Room, text: str, integration: str = "zigbee2mqtt") -> tuple[list[str], dict]:
    printed: list[str] = []
    result = await run(
        [suite(tmp_path, text, integration)],
        profile(integration, group="light.room"),
        REGISTRY,
        ha,
        admin=ha,
        tolerance=TOLERANCE,
        out=printed.append,
    )
    return printed, outcomes(result)


SET_STEP = header("group") + "      - set: {state: on, brightness_pct: 60}\n"
SET_IN_SETUP = (
    "action: turn_on_for\nrig_seconds: 5\ntests:\n  - name: t\n    roles: [group]\n"
    "    setup:\n      - set: {state: on, brightness_pct: 60}\n    steps:\n      - expect_state: {state: on}\n"
)


@pytest.mark.parametrize("text", [SET_STEP, SET_IN_SETUP], ids=["steps", "setup"])
async def test_a_set_the_coordinator_refused_with_busy_is_sent_once_more(tmp_path, text):
    """Catches the resend, in both phases (#57's retry covers setup only, and not for BUSY): one refusal."""
    ha = Room(refused_sets=1)
    printed, got = await run_room(tmp_path, ha, text)
    assert got["t[group]"] == (Outcome.PASS, "")
    assert len(ha.sets) == 2
    [line] = [line for line in printed if "BUSY" in line]
    assert "t[group]" in line and "sent the set again" in line


async def test_a_second_busy_fails_the_step(tmp_path):
    """Catches the bound: the set goes out twice, and the second refusal is the step's failure."""
    ha = Room(refused_sets=2)
    _, got = await run_room(tmp_path, ha, SET_STEP)
    outcome, detail = got["t[group]"]
    assert outcome is Outcome.FAIL and detail.startswith("step 1 (set)")
    assert len(ha.sets) == 2


async def test_a_set_that_missed_without_busy_is_not_sent_again_in_steps(tmp_path):
    """Catches the key: a drop Z2M never logged as BUSY fails at once, as before (#57)."""
    ha = Room(refused_sets=1)
    ha.z2m_logging.put_nowait = lambda line: None  # the refusal is never logged
    _, got = await run_room(tmp_path, ha, SET_STEP)
    assert got["t[group]"][0] is Outcome.FAIL
    assert len(ha.sets) == 1


async def test_busy_on_another_integration_resends_nothing(tmp_path):
    """Catches the scope: a Shelly or ZHA case never watches Z2M's log."""
    ha = Room(refused_sets=1)
    _, got = await run_room(tmp_path, ha, SET_STEP, "zha")
    assert got["t[group]"][0] is Outcome.FAIL
    assert len(ha.sets) == 1


CALL = header("group") + "      - call: {duration: 0.1}\n"


async def test_a_call_that_met_busy_runs_the_case_again(tmp_path):
    """Catches the rerun: the first run's call was refused, so it isn't the case's result; the second passes."""
    ha = Room(refused_calls=1)
    printed, got = await run_room(tmp_path, ha, CALL)
    assert got["t[group]"] == (Outcome.PASS, "")
    assert ha.calls_seen == 2
    assert any("coordinator busy" in line and "runs again" in line for line in printed)


async def test_a_second_busy_is_not_run_never_fail(tmp_path):
    """Catches the verdict: two refusals leave the case `not run`, not `fail`, which a failed call would read as."""
    ha = Room(refused_calls=2)
    _, got = await run_room(tmp_path, ha, CALL)
    assert got["t[group]"] == (Outcome.NOT_RUN, "coordinator busy")
    assert ha.calls_seen == 2


async def test_a_busy_before_the_call_is_not_the_calls(tmp_path):
    """Catches the window: a set refused and sent again, then a call that goes through, runs the case once."""
    ha = Room(refused_sets=1)
    text = header("group") + "      - set: {state: on, brightness_pct: 60}\n      - call: {duration: 0.1}\n"
    _, got = await run_room(tmp_path, ha, text)
    assert got["t[group]"] == (Outcome.PASS, "")
    assert ha.calls_seen == 1


@pytest.mark.parametrize(("after_s", "runs"), [(0.0, 2), (0.3, 1)], ids=["within_the_lag", "after_it"])
async def test_the_busy_window_is_the_call_steps_own_send_to_its_answer(tmp_path, after_s, runs):
    """Catches the window's source: the rig's own times for the call step, with no DeviceSide trace. A BUSY logged
    within BUSY_LAG_S of the answer is the call's; one later belongs to something else."""
    ha = Room()
    answer = ha.answer

    def answered_then_busy(_ha, action, data):
        reply = answer(_ha, action, data)
        if ha.calls_seen == 1:
            asyncio.get_running_loop().call_later(after_s, ha.z2m_logging.put_nowait, BUSY_LINE)
        return reply

    ha.respond = answered_then_busy
    _, got = await run_room(tmp_path, ha, CALL + "      - wait: 0.5\n")
    assert got["t[group]"] == (Outcome.PASS, "")
    assert ha.calls_seen == runs


def test_a_case_not_run_is_never_saved_and_leaves_the_suite_incomplete(tmp_path):
    from tests.test_progress import SUITE  # noqa: PLC0415

    result = CaseResult("a[switch_plug]", "switch_plug", "switch", Outcome.NOT_RUN, "coordinator busy")
    progress = Progress.load(tmp_path, "lab")
    progress.track(SUITE, "fp")
    progress.record(SUITE, result)
    assert not (progress.entry(SUITE) or {}).get("cases")
    planned = [case.id for case in engine.cases(SUITE, profile(switch_plug="switch.plug"), None, {"shelly"})]
    assert planned
    parts = [engine.SuiteResult(SUITE, [CaseResult(i, "switch_plug", "switch", Outcome.PASS) for i in planned], False)]
    parts[0].cases[0] = result
    [joined] = merge(
        parts, profile(switch_plug="switch.plug"), fakes.Roster({"shelly": {"turn_on_for", "get_attributes"}})
    ).suites
    assert not joined.complete


# --- pacing --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("integration", "role", "paced"),
    [("zigbee2mqtt", "group", True), ("zigbee2mqtt", "light_dimmable", False), ("zha", "group", False)],
)
async def test_only_a_z2m_group_case_waits_after_a_restore_that_put_nothing_back(
    tmp_path, monkeypatch, integration, role, paced
):
    """Catches the pacing's reach: nothing was moved, so only the group case on Z2M still waits."""
    pauses = []
    real_sleep = asyncio.sleep

    async def sleep(seconds, *args):
        pauses.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(engine, "RESTORE_SETTLE_S", 1.5)
    monkeypatch.setattr(engine.asyncio, "sleep", sleep)
    ha = Room()
    text = header(role) + "      - expect_state: {state: on}\n"
    ha.states["light.lamp"] = {"entity_id": "light.lamp", "state": "on", "attributes": {}}
    printed: list[str] = []
    await run(
        [suite(tmp_path, text, integration)],
        profile(integration, **{role: "light.room" if role == "group" else "light.lamp"}),
        REGISTRY,
        ha,
        admin=ha,
        tolerance=TOLERANCE,
        out=printed.append,
    )
    assert pauses.count(1.5) == (1 if paced else 0)


# --- the log feed ----------------------------------------------------------------------------------------------------


class Dropping(FakeHA):
    """A broker that quietly drops the first subscription to Z2M's log: nothing reaches it, and nothing says so.
    `live` is set once a later subscription is in place, so a test writes its line when one can hear it."""

    def __init__(self):
        super().__init__({})
        self.subscriptions = 0
        self.live = asyncio.Event()

    def _mqtt_messages(self, topic):
        if not topic.endswith("/logging"):
            return super()._mqtt_messages(topic)
        self.subscriptions += 1
        return self.dead() if self.subscriptions == 1 else self.renewed(topic)

    @contextlib.asynccontextmanager
    async def dead(self):
        yield asyncio.Queue()

    @contextlib.asynccontextmanager
    async def renewed(self, topic):
        async with FakeHA._mqtt_messages(self, topic) as queue:
            self.live.set()
            yield queue


async def until(done, timeout_s: float = 5) -> None:
    """Yield to the loop until `done()`; the deadline only stops a hang."""
    async with asyncio.timeout(timeout_s):
        while not done():
            await asyncio.sleep(0)


async def test_a_quiet_feed_subscribes_afresh_and_gets_what_came_after():
    ha = Dropping()
    async with ha.mqtt_feed("zigbee2mqtt/bridge/logging", quiet_s=0.01) as feed:
        getting = asyncio.create_task(feed.get())
        await asyncio.wait_for(ha.live.wait(), 5)
        ha.z2m_logging.put_nowait("after the drop")
        assert await asyncio.wait_for(getting, 5) == "after the drop"
        assert feed.renewed >= 1 and isinstance(feed, MQTTFeed)


async def test_the_z2m_log_capture_goes_on_after_its_subscription_dropped(tmp_path, monkeypatch):
    """Catches the silent log: the captured file has the line a dropped subscription would have missed, and says so."""
    monkeypatch.setattr(client, "MQTT_QUIET_S", 0.01)
    ha = Dropping()
    logs = RunLogs(tmp_path, "rig run", datetime.datetime.now(), out=lambda _: None)
    await logs.start_z2m(ha)
    await asyncio.wait_for(ha.live.wait(), 5)
    ha.z2m_logging.put_nowait('{"level":"info","namespace":"z2m","message":"later"}')
    await until(lambda: "later" in logs.z2m_path.read_text())
    await logs.stop_z2m()
    text = logs.z2m_path.read_text()
    assert "later" in text and "renewed its Z2M log subscription" in text


@pytest.mark.parametrize(
    ("old", "fresh", "joined"),
    [
        (["a", "b"], [], ["a", "b"]),  # the quiet wait ran out on lines the old subscription held
        (["a", "b", "c"], ["b", "c", "d"], ["a", "b", "c", "d"]),  # both live for b and c
        ([], ["d"], ["d"]),  # the old one was dead
        (["a"], ["b"], ["a", "b"]),  # it died before the new one went live
    ],
)
def test_a_renewal_keeps_each_line_once(old, fresh, joined):
    """Catches the swap: a line only the replaced subscription held is kept, and one both held is not doubled."""
    assert client._join(old, fresh) == joined


async def test_a_line_waiting_when_the_quiet_wait_runs_out_is_not_lost():
    """Catches the renewal race: a line already queued on the old subscription when it was replaced still arrives."""
    ha = FakeHA({})
    async with ha.mqtt_feed("zigbee2mqtt/bridge/logging", quiet_s=60) as feed:
        ha.z2m_logging.put_nowait("queued")
        await feed.renew()
        ha.z2m_logging.put_nowait("after")
        async with asyncio.timeout(5):  # a lost line leaves the second get waiting out the quiet minute
            assert [await feed.get(), await feed.get()] == ["queued", "after"]
        assert feed.renewed == 1


async def test_a_busy_line_is_noted_with_the_target_and_its_time():
    ha = FakeHA({})
    await ha.watch_z2m_busy()
    started = asyncio.get_running_loop().time()
    ha.z2m_logging.put_nowait('{"level":"info","namespace":"z2m","message":"Publish \'set\' to \'x\' ok"}')
    ha.z2m_logging.put_nowait("not json")
    ha.z2m_logging.put_nowait(BUSY_LINE)
    await asyncio.sleep(0.02)
    await ha.stop_z2m_busy()
    assert ha.z2m_busy_between(started, started + 1) == ["ds-rig-room"]
    assert ha.z2m_busy_between(started + 5, started + 6) == []


# --- group availability ----------------------------------------------------------------------------------------------

Z2M_GROUP = RoamGroup("light.test_group", frozenset({"group"}), ("light.lab_a", "light.lab_b"))


@pytest.fixture
def lab(monkeypatch) -> fakes.Lab:
    monkeypatch.setattr(roam, "POLL_S", 0.001)
    monkeypatch.setattr(roam, "Z2M_ONLINE_S", 0.05)
    lab = fakes.Lab(FakeHA({}), fakes.FakeBridge(), {fakes.LAB_A: "light.lab_a", fakes.LAB_B: "light.lab_b"})
    return lab


async def test_a_z2m_group_waits_for_a_member_to_be_online(lab):
    """Catches the member wait: Z2M's availability says offline until the member answers, and the group goes on then."""
    moving = Roamer(lab.ha, lab.bridge, fakes.LAB_BULBS, None, out=lambda _: None, groups=(Z2M_GROUP,))
    await moving.go("zigbee2mqtt")
    member = fakes.hex_ieee(fakes.LAB_B)
    lab.ha.z2m_offline.add(fakes.normalize_ieee(member))
    asyncio.get_running_loop().call_later(0.02, lab.ha.z2m_offline.clear)
    await moving.make_groups("zigbee2mqtt")
    assert "ds-rig-test-group" in lab.ha.z2m_groups


async def test_a_member_that_stays_offline_fails_the_group_by_name(lab):
    moving = Roamer(lab.ha, lab.bridge, fakes.LAB_BULBS, None, out=lambda _: None, groups=(Z2M_GROUP,))
    await moving.go("zigbee2mqtt")
    lab.ha.z2m_offline.add(fakes.normalize_ieee(fakes.hex_ieee(fakes.LAB_A)))
    with pytest.raises(GroupError, match=r"light\.lab_a, a member of ds-rig-test-group.*offline"):
        await moving.make_groups("zigbee2mqtt")
    assert lab.ha.z2m_groups == {}


async def test_a_group_whose_availability_never_says_online_fails_the_setup(lab):
    moving = Roamer(lab.ha, lab.bridge, fakes.LAB_BULBS, None, out=lambda _: None, groups=(Z2M_GROUP,))
    await moving.go("zigbee2mqtt")
    lab.ha.z2m_group_offline.add("ds-rig-test-group")
    with pytest.raises(GroupError, match="the group ds-rig-test-group is not published"):
        await moving.make_groups("zigbee2mqtt")


async def test_nothing_waits_where_z2m_availability_is_off(lab):
    moving = Roamer(lab.ha, lab.bridge, fakes.LAB_BULBS, None, out=lambda _: None, groups=(Z2M_GROUP,))
    await moving.go("zigbee2mqtt")
    lab.ha.z2m_availability_on = False
    lab.ha.z2m_offline.add(fakes.normalize_ieee(fakes.hex_ieee(fakes.LAB_A)))
    await moving.make_groups("zigbee2mqtt")
    assert "ds-rig-test-group" in lab.ha.z2m_groups
