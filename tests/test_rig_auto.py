"""The engine's entry-reload tracking and the snapshot of Shelly's auto config, against ShellyAuto: a write reloads
the device's entry shortly after, as core's Shelly integration does 60 s after a SetConfig."""

import asyncio
from dataclasses import replace

import pytest

from ha_rig import engine
from ha_rig.engine import Outcome, RunResult, load_step_file, run
from ha_rig.engine import Suite as EngineSuite
from ha_rig.journal import Journal
from tests.rig_fakes import FakeHA, Roster, ShellyAuto, planned_cases, profile

REGISTRY = Roster({"shelly": {"clear_auto_off", "get_attributes", "set_auto_off", "set_auto_on"}})
TOLERANCE = {"shelly": 0.2}
DEADLINE_S = 0.15
PLUG = "switch.plug"


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    monkeypatch.setitem(engine.ENTRY_RELOADS, "shelly", replace(engine.ENTRY_RELOADS["shelly"], deadline_s=DEADLINE_S))


def steps(tmp_path, action: str, body: str) -> EngineSuite:
    path = tmp_path / f"{action}.yaml"
    path.write_text(
        f"action: {action}\nrig_seconds: 5\ntests:\n  - name: t\n    roles: [switch_plug]\n    steps:\n{body}"
    )
    return EngineSuite(load_step_file(path), "shelly")


async def run_auto(tmp_path, ha: FakeHA, action: str, body: str, **kwargs) -> RunResult:
    return await run(
        [steps(tmp_path, action, body)],
        profile("shelly", switch_plug=PLUG),
        REGISTRY,
        ha,
        tolerance=TOLERANCE,
        out=lambda _: None,
        **kwargs,
    )


def outcome(result: RunResult) -> tuple[Outcome, str]:
    (case,) = result.suites[0].cases
    return case.outcome, case.detail


def sequence(ha: FakeHA) -> list[tuple[str, str]]:
    return [(domain, service) for domain, service, _ in ha.calls]


WRITE_TWICE = """\
      - set: {state: off}
      - call: {duration: 6}
      - expect_response: {status: expected, integration: self}
      - call: {duration: 6}
      - expect_response: {status: unchanged, detail: already_set}
"""


async def test_a_write_is_waited_out_and_the_restore_puts_the_config_back_before_the_state(tmp_path):
    shelly = ShellyAuto()
    ha = FakeHA({PLUG: {"state": "on"}}, shelly)

    result = await run_auto(tmp_path, ha, "set_auto_off", WRITE_TWICE)

    assert outcome(result) == (Outcome.PASS, "")
    # ShellyAuto raises on a call landing mid-reload, so the second call came after it.
    assert sequence(ha) == [
        ("deviceside", "get_attributes"),  # the snapshot's auto config
        ("switch", "turn_off"),
        ("deviceside", "set_auto_off"),
        ("reload", PLUG),
        ("deviceside", "set_auto_off"),  # unchanged: no reload
        ("deviceside", "clear_auto_off"),  # the restore: config first, and its reload waited out
        ("deviceside", "clear_auto_on"),
        ("reload", PLUG),
        ("switch", "turn_on"),
    ]
    assert shelly.config[PLUG] == {"auto_off": None, "auto_on": None}


async def test_a_set_waits_out_the_reload(tmp_path):
    ha = FakeHA({PLUG: {}}, ShellyAuto())
    body = "      - call: {duration: 6}\n      - set: {state: on}\n"

    result = await run_auto(tmp_path, ha, "set_auto_off", body)

    assert outcome(result) == (Outcome.PASS, "")
    assert sequence(ha)[1:4] == [("deviceside", "set_auto_off"), ("reload", PLUG), ("switch", "turn_on")]


async def test_the_case_end_waits_out_a_reload_no_step_did(tmp_path):
    ha = FakeHA({PLUG: {}}, ShellyAuto({PLUG: {"auto_off": {"duration": 6}, "auto_on": None}}))

    result = await run_auto(tmp_path, ha, "clear_auto_off", "      - call: {}\n")

    assert outcome(result) == (Outcome.PASS, "")
    # The case's own reload ends before the restore's set: it would land mid-reload otherwise.
    assert sequence(ha)[1:4] == [("deviceside", "clear_auto_off"), ("reload", PLUG), ("deviceside", "set_auto_off")]


async def test_an_unchanged_call_watches_for_no_reload(tmp_path):
    ha = FakeHA({PLUG: {}}, ShellyAuto({PLUG: {"auto_off": {"duration": 6}, "auto_on": None}}, reloads=False))
    body = "      - call: {duration: 6}\n      - expect_response: {status: unchanged}\n"

    result = await run_auto(tmp_path, ha, "set_auto_off", body)

    assert outcome(result) == (Outcome.PASS, "")  # no reload came, and none was waited for


async def test_a_write_with_no_reload_fails_and_journals_what_it_could_not_put_back(tmp_path):
    journal = Journal(tmp_path / "journal.json")
    shelly = ShellyAuto(reloads=False)
    ha = FakeHA({PLUG: {}}, shelly)

    result = await run_auto(tmp_path, ha, "set_auto_off", "      - call: {duration: 6}\n", journal=journal)

    assert outcome(result) == (Outcome.FAIL, f"case end: {PLUG}: no entry reload within {DEADLINE_S:g} s of the write")
    assert shelly.config[PLUG]["auto_off"] is None  # the restore's clear went through; its reload never came
    kept = journal.get("cases")[f"serial{engine.UNRESTORED}"]
    assert kept["auto"] == {PLUG: {"auto_off": None, "auto_on": None}}
    assert kept["integration"] == "shelly"


async def test_the_journal_holds_the_auto_config_before_the_case_writes_it(tmp_path):
    journal = Journal(tmp_path / "journal.json")
    shelly = ShellyAuto()
    held = []

    def respond(ha, action, data):
        if action == "set_auto_off":
            held.append(journal.get("cases")["serial"])
        return shelly(ha, action, data)

    ha = FakeHA({PLUG: {}}, respond)

    result = await run_auto(tmp_path, ha, "set_auto_off", "      - call: {duration: 6}\n", journal=journal)

    assert outcome(result) == (Outcome.PASS, "")
    assert held[0]["auto"] == {PLUG: {"auto_off": None, "auto_on": None}}
    assert held[0]["integration"] == "shelly"
    assert not journal.path.exists()


async def test_a_read_without_the_auto_rows_fails_before_the_case_writes(tmp_path):
    ha = FakeHA({PLUG: {}}, lambda ha, action, data: {PLUG: {"brightness": 40}})

    result = await run_auto(tmp_path, ha, "set_auto_off", "      - call: {duration: 6}\n")

    assert outcome(result)[0] is Outcome.FAIL
    assert "get_attributes read no auto_off or auto_on" in outcome(result)[1]
    assert "set_auto_off" not in [service for _, service, _ in ha.calls]


async def test_only_a_case_calling_an_entry_reload_action_reads_the_auto_config(tmp_path):
    ha = FakeHA({PLUG: {}}, ShellyAuto())

    result = await run_auto(tmp_path, ha, "get_attributes", "      - call: {}\n")

    assert outcome(result) == (Outcome.PASS, "")
    assert sequence(ha) == [("deviceside", "get_attributes"), ("switch", "turn_off")]  # the step's read alone


async def test_a_case_calling_one_as_another_action_reads_it(tmp_path):
    ha = FakeHA({PLUG: {}}, ShellyAuto())
    body = "      - call: {action: set_auto_on, duration: 9}\n      - call: {}\n"

    result = await run_auto(tmp_path, ha, "get_attributes", body)

    assert outcome(result) == (Outcome.PASS, "")
    assert sequence(ha)[:3] == [("deviceside", "get_attributes"), ("deviceside", "set_auto_on"), ("reload", PLUG)]


@pytest.mark.parametrize(
    ("config", "result"),
    [
        ({"auto_off": {"duration": 6}, "auto_on": None}, (Outcome.PASS, "")),
        (None, (Outcome.FAIL, f"step 2 (expect_no_reload): {PLUG}: went unavailable within 0.3 s, an entry reload")),
    ],
)
async def test_expect_no_reload(tmp_path, config, result):
    ha = FakeHA({PLUG: {}}, ShellyAuto({PLUG: config} if config else None))
    body = "      - call: {duration: 6}\n      - expect_no_reload: {for: 0.3}\n"

    assert outcome(await run_auto(tmp_path, ha, "set_auto_off", body)) == result


async def test_an_observed_run_waits_out_expect_no_reload(tmp_path):
    ha = FakeHA({PLUG: {}}, ShellyAuto())
    case_run = engine._CaseRun(
        planned_cases(steps(tmp_path, "set_auto_off", "      - wait: 0\n"), profile("shelly", switch_plug=PLUG))[0],
        ha,
        REGISTRY,
        0.2,
        None,
    )
    loop = asyncio.get_running_loop()
    started = loop.time()

    await case_run._relaxed_expect_no_reload({"for": engine.Expr.parse(0.1)}, None)

    assert loop.time() - started >= 0.1


async def test_a_rejected_restore_is_journaled_for_recover(tmp_path):
    journal = Journal(tmp_path / "journal.json")
    shelly = ShellyAuto()

    def respond(ha, action, data):
        if action == "clear_auto_off":
            return {PLUG: {"status": "rejected", "integration": "shelly"}}
        return shelly(ha, action, data)

    ha = FakeHA({PLUG: {}}, respond)

    result = await run_auto(tmp_path, ha, "set_auto_off", "      - call: {duration: 6}\n", journal=journal)

    assert outcome(result) == (Outcome.PASS, "")  # the case passed; only its restore failed
    assert journal.get("cases")[f"serial{engine.UNRESTORED}"]["auto"] == {PLUG: {"auto_off": None, "auto_on": None}}


async def test_a_watcher_that_errors_fails_the_case_not_the_run(tmp_path, monkeypatch):
    ha = FakeHA({PLUG: {}}, ShellyAuto())

    async def broken(entity_id, predicate, timeout):
        raise ConnectionError("websocket closed")

    monkeypatch.setattr(ha, "wait_for", broken)

    result = await run_auto(tmp_path, ha, "set_auto_off", "      - call: {duration: 6}\n")

    assert outcome(result) == (Outcome.FAIL, "case end: websocket closed")
    assert ("switch", "turn_off") in sequence(ha)  # the restore still ran
