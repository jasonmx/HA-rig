"""scripts/rig.py with a roaming profile: phases by the cycle, a move before each and home after the last, a crash in
each phase, --roam-check, and --recover bringing the bulbs home. Against the Lab fake; no rig is touched."""

import asyncio
import contextlib
import importlib.util
import io
import sys
import time
from collections.abc import Callable, Coroutine
from pathlib import Path

import pytest
from custom_components.deviceside.core.registry import AdapterRegistry

from ha_rig import engine, roam
from ha_rig.engine import load_step_file
from ha_rig.home import HomeRecord
from ha_rig.journal import LEGACY_NAME, Journal
from ha_rig.profile import Profile, RigEntity
from ha_rig.roam import Roamer
from tests import rig_fakes as fakes

RIG_HOME = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("rig_for_roaming", RIG_HOME / "scripts" / "rig.py")
rig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rig)

A, B, BULBS, lab_profile = fakes.LAB_A, fakes.LAB_B, fakes.LAB_BULBS, fakes.lab_profile
ENV = {"HA_URL": "http://ha", "HA_TOKEN": "t", "HA_ADMIN_TOKEN": "a", "HUE_BRIDGE_HOST": "h", "HUE_APP_KEY": "k"}


@pytest.fixture(autouse=True)
def reports_in_tmp(tmp_path, monkeypatch):
    """A run's report and captured logs go to the test's folder, not the system temp one."""
    monkeypatch.setattr(rig, "REPORT_DIR", tmp_path / "reports")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(roam, "POLL_S", 0)


@pytest.fixture
def lab(monkeypatch) -> fakes.Lab:
    lab = fakes.lab_on_hue(fakes.FakeHA({"automation.lab": {"state": "on"}}))
    monkeypatch.setattr(rig, "connect", fakes.connected(lab.ha))
    monkeypatch.setattr(rig, "HueBridge", lambda host, key: lab.bridge)
    return lab


def lab_with_pause(tmp_path: Path, networks: str = "hue, zha, zigbee2mqtt") -> Profile:
    path = lab_profile(tmp_path, networks=networks)
    path.write_text("pause: [automation.lab]\n" + path.read_text())
    return Profile.load(path)


def suites(tmp_path: Path, *integrations: str) -> list[rig.Suite]:
    path = tmp_path / "steps.yaml"
    path.write_text(fakes.RESTORED_MIDWAY)
    return [rig.Suite(load_step_file(path), integration) for integration in integrations]


def fake_phases(monkeypatch, lab: fakes.Lab, fail_on: str | None = None) -> list[tuple[list[str], list]]:
    """engine.run and wait_for_targets as recorders: each phase's integrations and where the bulbs were."""
    ran: list[tuple[list[str], list]] = []

    async def run(phase_suites, *args, **kwargs):
        ran.append(([suite.integration for suite in phase_suites], await fakes.where(lab)))
        if fail_on in [suite.integration for suite in phase_suites]:
            raise RuntimeError(f"crash on {fail_on}")
        return engine.RunResult([])

    async def wait_for_targets(*args, **kwargs):
        pass

    monkeypatch.setattr(rig, "run", run)
    monkeypatch.setattr(rig, "wait_for_targets", wait_for_targets)
    monkeypatch.setattr(rig, "write_report", lambda result, directory, title: directory / "report.md")
    return ran


def test_a_profile_roams_only_for_a_zha_or_z2m_suite_or_the_check(tmp_path):
    profile = lab_with_pause(tmp_path)
    assert not rig.roams(profile, suites(tmp_path, "shelly", "hue"))
    assert rig.roams(profile, suites(tmp_path, "zigbee2mqtt"))
    assert rig.roams(profile, [], roam_check=True)
    assert not rig.roams(fakes.profile("zha"), suites(tmp_path, "zha"))


def test_phases_follow_the_cycle_with_the_fixed_integrations_on_hue(tmp_path):
    profile = lab_with_pause(tmp_path)
    run = suites(tmp_path, "zigbee2mqtt", "shelly", "zha", "hue", "matter")
    assert [(network, [s.integration for s in group]) for network, group in rig.phases(run, profile, True)] == [
        ("hue", ["shelly", "hue", "matter"]),
        ("zha", ["zha"]),
        ("zigbee2mqtt", ["zigbee2mqtt"]),
    ]
    assert rig.phases(run, profile, False) == [(None, run)]


def test_a_suite_on_an_undeclared_network_runs_at_home_and_never_moves_the_bulbs(tmp_path):
    profile = lab_with_pause(tmp_path, networks="hue, zha")
    run = suites(tmp_path, "zigbee2mqtt", "zha")
    assert [(network, [s.integration for s in group]) for network, group in rig.phases(run, profile, True)] == [
        ("hue", ["zigbee2mqtt"]),
        ("zha", ["zha"]),
    ]
    assert not rig.roams(profile, suites(tmp_path, "zigbee2mqtt"))
    assert [network for network, _ in rig.phases([], profile, True)] == ["hue", "zha"]  # the check: declared only


def test_phases_skip_a_network_with_no_suites_and_the_check_visits_every_one(tmp_path):
    profile = lab_with_pause(tmp_path)
    run = suites(tmp_path, "zigbee2mqtt", "shelly")
    assert [network for network, _ in rig.phases(run, profile, True)] == ["hue", "zigbee2mqtt"]  # straight past ZHA
    assert [network for network, _ in rig.phases([], profile, True)] == ["hue", "zha", "zigbee2mqtt"]
    lines = roam.plan_lines(profile.roam, route=["hue", "zigbee2mqtt"])
    assert lines[0] == "Roams light.lab_a, light.lab_b together: Hue -> Z2M -> Hue."


async def test_a_roaming_run_moves_before_each_phase_and_ends_home(tmp_path, monkeypatch, lab):
    ran = fake_phases(monkeypatch, lab)
    journal = Journal(tmp_path / "journal.json")
    run = suites(tmp_path, "zigbee2mqtt", "shelly", "zha")

    assert await rig.run_rig(ENV, run, lab_with_pause(tmp_path), None, None, True, None, journal, roaming=True) == 0

    assert ran == [(["shelly"], ["hue", "hue"]), (["zha"], ["zha", "zha"]), (["zigbee2mqtt"], ["zigbee2mqtt"] * 2)]
    assert await fakes.where(lab) == ["hue", "hue"]
    assert lab.ha.states["automation.lab"]["state"] == "on"
    assert not journal.path.exists()


@pytest.mark.parametrize("phase", ["shelly", "zha", "zigbee2mqtt"])
async def test_a_crash_in_any_phase_still_moves_the_bulbs_home(tmp_path, monkeypatch, lab, phase):
    fake_phases(monkeypatch, lab, fail_on=phase)
    journal = Journal(tmp_path / "journal.json")
    run = suites(tmp_path, "shelly", "zha", "zigbee2mqtt")

    with pytest.raises(RuntimeError, match=f"crash on {phase}"):
        await rig.run_rig(ENV, run, lab_with_pause(tmp_path), None, None, True, None, journal, roaming=True)

    assert await fakes.where(lab) == ["hue", "hue"]
    assert len(lab.bridge.resources["room"]["r1"]["children"]) == 3
    assert not journal.path.exists()


async def test_a_failed_move_stops_the_run_and_leaves_the_bulbs_for_recover(tmp_path, monkeypatch, lab):
    fake_phases(monkeypatch, lab)
    monkeypatch.setattr(roam, "JOIN_S", 0.02)
    lab.stuck.add(B[2:])
    journal = Journal(tmp_path / "journal.json")

    with pytest.raises(SystemExit, match="Roaming stopped: light.lab_b did not join ZHA"):
        await rig.run_rig(
            ENV, suites(tmp_path, "zha"), lab_with_pause(tmp_path), None, None, True, None, journal, roaming=True
        )

    assert await fakes.where(lab) == ["zha", None]
    assert lab.ha.states["automation.lab"]["state"] == "on"
    assert set(Journal(journal.path).sections) == {"profile", "roam"}


async def test_a_failed_move_still_reports_the_phases_before_it(tmp_path, monkeypatch, lab):
    fake_phases(monkeypatch, lab)
    passed = object()  # the Hue phase's suite result, as the 3 Nov run's Hue suites passed before the move to Z2M

    async def run(phase_suites, *args, **kwargs):
        return engine.RunResult([passed] if phase_suites[0].integration == "shelly" else [])

    reported = []

    async def report(result, *args):
        reported.append(result.suites)

    monkeypatch.setattr(rig, "run", run)
    monkeypatch.setattr(rig, "report", report)
    monkeypatch.setattr(roam, "JOIN_S", 0.02)
    lab.stuck.add(B[2:])
    journal = Journal(tmp_path / "journal.json")

    with pytest.raises(SystemExit, match="Roaming stopped: light.lab_b did not join ZHA"):
        await rig.run_rig(
            ENV,
            suites(tmp_path, "shelly", "zha"),
            lab_with_pause(tmp_path),
            None,
            None,
            True,
            None,
            journal,
            roaming=True,
        )

    assert reported == [[passed]]


async def test_a_move_home_that_fails_after_the_last_phase_stops_the_run(tmp_path, monkeypatch, lab):
    fake_phases(monkeypatch, lab)
    joins = lab.opened

    def opened(network):
        if network == "hue":
            lab.stuck.add(A[2:])
        joins(network)

    lab.opened = opened
    lab.ha.on_open = opened
    monkeypatch.setattr(roam, "JOIN_S", 0.02)

    journal = Journal(tmp_path / "journal.json")
    with pytest.raises(SystemExit, match="did not join Hue"):
        await rig.run_rig(
            ENV, suites(tmp_path, "zha"), lab_with_pause(tmp_path), None, None, True, None, journal, roaming=True
        )
    assert HomeRecord(tmp_path).get(A[2:])["hue"]


async def test_a_run_whose_bulbs_cant_be_put_back_as_they_were_fails(monkeypatch, lab, tmp_path, capsys):
    fake_phases(monkeypatch, lab)
    call_service = lab.ha.call_service

    async def refuse_lights(domain, service, data=None, return_response=False):
        if domain == "light":
            raise RuntimeError("light call failed")
        return await call_service(domain, service, data, return_response)

    lab.ha.call_service = refuse_lights
    assert await rig.run_rig(ENV, [], lab_with_pause(tmp_path), None, None, True, None, None, roaming=True) == 1
    assert await fakes.where(lab) == ["hue", "hue"]
    assert "light.lab_a's home isn't all back" in capsys.readouterr().out


async def test_roam_check_runs_the_cycle_with_no_suites(monkeypatch, lab, tmp_path):
    ran = fake_phases(monkeypatch, lab)
    assert await rig.run_rig(ENV, [], lab_with_pause(tmp_path), None, None, True, None, None, roaming=True) == 0
    assert ran == []
    assert lab.searches == 1
    assert await fakes.where(lab) == ["hue", "hue"]


def test_main_refuses_roam_check_on_a_profile_with_no_roam(tmp_path, monkeypatch):
    path = tmp_path / "house.yaml"
    path.write_text("shelly:\n  - entity_id: switch.plug\n    roles: [switch_plug]\n")
    monkeypatch.setattr(rig, "choose_profile", lambda name, env: path)
    monkeypatch.setattr(rig, "load_env", lambda: {})
    monkeypatch.setattr(rig, "git", lambda *args, check=True: str(tmp_path) if "--git-common-dir" in args else "wt")
    with pytest.raises(SystemExit, match="--roam-check needs a profile with roam:, and house has none"):
        rig.main(["--roam-check", "--dry-run"])


def test_main_dry_runs_roam_check_with_the_moves_and_no_deploy(tmp_path, monkeypatch, capsys):
    path = lab_profile(tmp_path)
    monkeypatch.setattr(rig, "choose_profile", lambda name, env: path)
    monkeypatch.setattr(rig, "load_env", lambda: {})
    monkeypatch.setattr(rig, "git", lambda *args, check=True: str(tmp_path) if "--git-common-dir" in args else "wt")
    deploy = rig.Deploy("c1", "tree", "old", "old", "wt")
    monkeypatch.setattr(rig, "deploy_state", lambda status_cmd: deploy)

    assert rig.main(["--roam-check", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "  roams    light.lab_a" in out
    assert "Roams light.lab_a, light.lab_b together: Hue -> ZHA -> Z2M -> Hue." in out
    assert "Adapters on ZHA:\n  none\n" in out
    assert "Deploy: not needed, no restart" in out
    assert f"Estimated duration: {rig.ROAM_ESTIMATE_S} s." in out


def test_the_plan_lists_each_phase_and_the_moves(tmp_path, capsys):
    profile = lab_with_pause(tmp_path)
    registry = AdapterRegistry.discover()
    run = rig.select(rig.load_step_files(fakes.STEPS), profile, None, ["turn_on_for"])
    rig.print_phases(profile, run, registry, None, rig.roams(profile, run))
    out = capsys.readouterr().out
    assert out.index("Adapters on Hue:") < out.index("Adapters on ZHA:") < out.index("Adapters on Z2M:")
    assert "zha / turn_on_for -> light.lab_b, light.lab_a" in out
    assert rig.estimate(run, registry, False, True) == rig.estimate(run, registry, False) + rig.ROAM_ESTIMATE_S


def test_roaming_needs_the_admin_token_and_the_bridge():
    for missing in ("HA_ADMIN_TOKEN", "HUE_APP_KEY"):
        env = {key: value for key, value in ENV.items() if key != missing}
        with pytest.raises(SystemExit, match="Roaming needs HA_ADMIN_TOKEN, HUE_BRIDGE_HOST and HUE_APP_KEY"):
            rig.check_tokens(env, [], AdapterRegistry.discover(), roaming=True)
    rig.check_tokens(ENV, [], AdapterRegistry.discover(), roaming=True)


async def aborted_on_zha(lab: fakes.Lab, path: Path) -> Journal:
    """What a run killed on ZHA leaves: the bulbs there, their home saved, the automation paused."""
    journal = Journal(path)
    journal.update(profile="lab", paused=["automation.lab"])
    lab.ha.states["automation.lab"]["state"] = "off"
    await Roamer(lab.ha, lab.bridge, BULBS, journal, out=lambda _: None).go("zha")
    return journal


async def test_recover_moves_the_bulbs_home_then_resumes_the_automations(tmp_path, lab):
    journal = await aborted_on_zha(lab, tmp_path / "journal.json")
    printed = []

    await rig.recover(lab.ha, lab.ha, Journal(journal.path), printed.append, bridge=lab.bridge)

    assert await fakes.where(lab) == ["hue", "hue"]
    assert "Moving the roaming bulbs home to Hue." in printed
    assert lab.ha.states["automation.lab"]["state"] == "on"
    assert lab.ha.calls[-1][:2] == ("automation", "turn_on")
    assert not journal.path.exists()


async def test_recover_puts_back_each_bulbs_pre_run_state(tmp_path, lab):
    lab.ha.set("light.lab_a", "on", brightness=120, color_mode="xy", xy_color=(0.3, 0.32), color_temp_kelvin=None)
    lab.ha.set("light.lab_b", "on", brightness=40, color_mode="color_temp", color_temp_kelvin=2700, xy_color=(0.4, 0.4))
    journal = await aborted_on_zha(lab, tmp_path / "journal.json")
    lab.ha.set("light.lab_a", "on", brightness=255)  # what a join leaves
    lab.ha.set("light.lab_b", "off")

    await rig.recover(lab.ha, lab.ha, Journal(journal.path), lambda _: None, bridge=lab.bridge)

    a, b = lab.ha.states["light.lab_a"], lab.ha.states["light.lab_b"]
    assert a["state"] == "on" and a["attributes"] == {"brightness": 120, "xy_color": [0.3, 0.32]}
    assert b["state"] == "on" and b["attributes"] == {"brightness": 40, "color_temp_kelvin": 2700}
    assert HomeRecord(tmp_path).all() == {} and not journal.path.exists()


async def test_a_bulb_off_before_the_run_ends_off(tmp_path, lab):
    journal = await aborted_on_zha(lab, tmp_path / "journal.json")  # both bulbs off, as the lab fake starts them
    lab.ha.set("light.lab_a", "on", brightness=255)
    await rig.recover(lab.ha, lab.ha, Journal(journal.path), lambda _: None, bridge=lab.bridge)
    assert lab.ha.states["light.lab_a"]["state"] == "off"


async def test_a_state_that_wont_go_back_keeps_the_home_record_and_the_journal(tmp_path, lab):
    journal = await aborted_on_zha(lab, tmp_path / "journal.json")
    respond = lab.ha.call_service

    async def refuse_lights(domain, service, data=None, return_response=False):
        if domain == "light":
            raise RuntimeError("light.turn_off failed")
        return await respond(domain, service, data, return_response)

    lab.ha.call_service = refuse_lights
    printed = []
    await rig.recover(lab.ha, lab.ha, Journal(journal.path), printed.append, bridge=lab.bridge)
    assert await fakes.where(lab) == ["hue", "hue"]
    assert set(HomeRecord(tmp_path).all()) == {A[2:], B[2:]}
    assert "roam" in Journal(journal.path).sections
    assert any("home isn't all back; the home record keeps it" in line for line in printed)

    lab.ha.call_service = respond
    await rig.recover(lab.ha, lab.ha, Journal(journal.path), lambda _: None, bridge=lab.bridge)
    assert HomeRecord(tmp_path).all() == {} and not journal.path.exists()


def test_main_recover_builds_the_bridge_and_brings_the_bulbs_home(tmp_path, monkeypatch, lab):
    asyncio.run(aborted_on_zha(lab, tmp_path / LEGACY_NAME))
    monkeypatch.setattr(rig, "git", lambda *args, check=True: str(tmp_path) if "--git-common-dir" in args else "wt")
    monkeypatch.setattr(rig, "load_env", lambda: ENV)
    monkeypatch.setenv("DEVICE_SIDE_RIG_RESTRICTED", "1")  # the journal from before per-run ones is restricted

    assert rig.main(["--recover"]) == 0
    assert asyncio.run(fakes.where(lab)) == ["hue", "hue"]


async def test_recover_stops_on_a_bulb_on_no_network_keeping_the_journal(tmp_path, lab):
    journal = await aborted_on_zha(lab, tmp_path / "journal.json")
    await lab.ha.zha_remove("00:17:88:01:0a:bc:de:f1")

    with pytest.raises(SystemExit, match=r"Recovery stopped: Not on any Zigbee network: light.lab_b\. Reset it"):
        await rig.recover(lab.ha, lab.ha, Journal(journal.path), lambda _: None, bridge=lab.bridge)

    assert set(Journal(journal.path).sections) == {"profile", "paused", "roam"}
    assert lab.ha.states["automation.lab"]["state"] == "off"


async def test_recover_without_the_bridge_keeps_the_bulbs_in_the_journal(tmp_path, lab):
    journal = await aborted_on_zha(lab, tmp_path / "journal.json")
    with pytest.raises(SystemExit, match="needs HA_ADMIN_TOKEN, HUE_BRIDGE_HOST and HUE_APP_KEY"):
        await rig.recover(lab.ha, lab.ha, Journal(journal.path), lambda _: None)
    assert await fakes.where(lab) == ["zha", "zha"]
    assert "roam" in Journal(journal.path).sections


GROUPS_YAML = (
    "  groups:\n"
    "    - entity_id: light.test_group\n      roles: [group]\n      members: all\n      scene: scene.test_relax\n"
)


def lab_with_groups(tmp_path: Path) -> Profile:
    path = lab_profile(tmp_path, GROUPS_YAML)
    path.write_text("pause: [automation.lab]\n" + path.read_text())
    return Profile.load(path)


def record_groups(monkeypatch, lab: fakes.Lab, fail_on: str | None = None) -> list[tuple[list[str], list, list]]:
    """fake_phases, also noting which rig-made entities HA had during each phase."""
    ran = fake_phases(monkeypatch, lab, fail_on)
    recorded: list[tuple[list[str], list, list]] = []
    run = rig.run

    async def run_noting(phase_suites, *args, **kwargs):
        made = sorted(e for e in lab.ha.entity_meta if e in ("light.test_group", "scene.test_relax"))
        try:
            return await run(phase_suites, *args, **kwargs)
        finally:
            recorded.append((*ran[-1], made))

    monkeypatch.setattr(rig, "run", run_noting)
    return recorded


def test_groups_are_made_when_the_run_roams_or_runs_a_hue_suite(tmp_path):
    profile = lab_with_groups(tmp_path)
    assert rig.makes_groups(profile, suites(tmp_path, "hue"))
    assert rig.makes_groups(profile, suites(tmp_path, "zha"))
    assert rig.makes_groups(profile, [], roam_check=True)
    assert not rig.makes_groups(profile, suites(tmp_path, "shelly"))
    assert not rig.makes_groups(lab_with_pause(tmp_path), suites(tmp_path, "hue"))


async def test_a_roaming_run_makes_the_groups_in_each_phase_and_removes_them(tmp_path, monkeypatch, lab):
    monkeypatch.setattr(roam, "GONE_S", 0)
    recorded = record_groups(monkeypatch, lab)
    journal = Journal(tmp_path / "journal.json")
    run = suites(tmp_path, "hue", "zha", "zigbee2mqtt")

    code = await rig.run_rig(
        ENV, run, lab_with_groups(tmp_path), None, None, True, None, journal, roaming=True, grouping=True
    )

    assert code == 0
    assert [(phase[0], phase[2]) for phase in recorded] == [
        (["hue"], ["light.test_group", "scene.test_relax"]),
        (["zha"], ["light.test_group"]),
        (["zigbee2mqtt"], ["light.test_group"]),
    ]
    assert "light.test_group" not in lab.ha.entity_meta
    assert lab.ha.zha_group_names == {} and lab.ha.z2m_groups == {}
    assert not journal.path.exists()


async def test_a_hue_run_makes_the_groups_on_hue_without_moving_the_bulbs(tmp_path, monkeypatch, lab):
    monkeypatch.setattr(roam, "GONE_S", 0)
    recorded = record_groups(monkeypatch, lab)
    joins = lab.joins

    code = await rig.run_rig(
        ENV, suites(tmp_path, "hue"), lab_with_groups(tmp_path), None, None, True, None, None, grouping=True
    )

    assert code == 0
    assert recorded == [(["hue"], ["hue", "hue"], ["light.test_group", "scene.test_relax"])]
    assert lab.joins == joins
    assert "light.test_group" not in lab.ha.entity_meta


async def test_roam_check_makes_and_removes_the_groups_on_every_visit(tmp_path, monkeypatch, lab):
    monkeypatch.setattr(roam, "GONE_S", 0)
    made = []
    make = Roamer.make_groups

    async def noting(self, network):
        await make(self, network)
        made.append((network, sorted(e for e in lab.ha.entity_meta if e.startswith(("light.test_group", "scene.")))))

    monkeypatch.setattr(Roamer, "make_groups", noting)
    code = await rig.run_rig(ENV, [], lab_with_groups(tmp_path), None, None, True, None, None, True, True)
    assert code == 0
    assert made == [
        ("hue", ["light.test_group", "scene.test_relax"]),
        ("zha", ["light.test_group"]),
        ("zigbee2mqtt", ["light.test_group"]),
    ]
    assert "light.test_group" not in lab.ha.entity_meta


async def test_a_group_that_cant_be_made_stops_the_run_and_removes_the_ones_made(tmp_path, monkeypatch, lab):
    monkeypatch.setattr(roam, "GONE_S", 0)
    fake_phases(monkeypatch, lab)
    lab.ha.register("scene.test_relax", None, "template", "someone-elses")
    journal = Journal(tmp_path / "journal.json")

    with pytest.raises(SystemExit, match="Roaming stopped: scene.test_relax is already in HA's entity registry"):
        await rig.run_rig(
            ENV, suites(tmp_path, "hue"), lab_with_groups(tmp_path), None, None, True, None, journal, grouping=True
        )

    assert "light.test_group" not in lab.ha.entity_meta
    assert {item["metadata"]["name"] for item in lab.bridge.resources["zone"].values() if "metadata" in item} == set()
    assert lab.ha.states["automation.lab"]["state"] == "on"


def test_the_plan_lists_the_groups_and_whether_the_bulbs_move(tmp_path, capsys):
    profile = lab_with_groups(tmp_path)
    run = rig.select(rig.load_step_files(fakes.STEPS), profile, ["hue"], ["set_scene"])
    rig.print_phases(profile, run, AdapterRegistry.discover(), None, False, True)
    out = capsys.readouterr().out
    assert "Roaming: none, as no ZHA or Z2M suite runs; the bulbs stay on Hue." in out
    assert "Makes ds-rig-test-group on Hue as light.test_group (light.lab_a, light.lab_b), with scene" in out
    assert "hue / set_scene -> scene.test_relax" in out
    rig.print_profile(profile)
    assert "  ds-rig   scene.test_relax" in capsys.readouterr().out


async def test_a_group_that_cant_be_made_away_from_home_still_brings_the_bulbs_home(tmp_path, monkeypatch, lab):
    monkeypatch.setattr(roam, "GONE_S", 0)
    fake_phases(monkeypatch, lab)
    journal = Journal(tmp_path / "journal.json")
    await lab.ha.zha_group_add("ds-rig-test-group")  # a leftover no run journaled

    with pytest.raises(SystemExit, match="Roaming stopped: ZHA already has a group named ds-rig-test-group"):
        await rig.run_rig(
            ENV, suites(tmp_path, "zha"), lab_with_groups(tmp_path), None, None, True, None, journal, True, True
        )

    assert await fakes.where(lab) == ["hue", "hue"]
    assert not journal.path.exists()


async def test_a_group_timeout_in_a_later_phase_reports_the_phases_that_passed(tmp_path, monkeypatch, lab):
    monkeypatch.setattr(roam, "GONE_S", 0)
    fake_phases(monkeypatch, lab)
    passed = object()  # the Hue phase's suite result, as on 4 Nov, when Z2M then timed out adding a bulb to the group

    async def run(phase_suites, *args, **kwargs):
        return engine.RunResult([passed] if phase_suites[0].integration == "hue" else [])

    reported = []

    async def report(result, *args):
        reported.append(result.suites)

    async def z2m_group_add(name, members=()):
        raise TimeoutError(f"Zigbee2MQTT did not add {members[0]} to {name} within 30 s")

    monkeypatch.setattr(rig, "run", run)
    monkeypatch.setattr(rig, "report", report)
    monkeypatch.setattr(lab.ha, "z2m_group_add", z2m_group_add)
    journal = Journal(tmp_path / "journal.json")

    with pytest.raises(SystemExit, match="Roaming stopped: couldn't make ds-rig-test-group on Z2M: Zigbee2MQTT did"):
        await rig.run_rig(
            ENV,
            suites(tmp_path, "hue", "zigbee2mqtt"),
            lab_with_groups(tmp_path),
            None,
            None,
            True,
            None,
            journal,
            roaming=True,
            grouping=True,
        )

    assert reported == [[passed]]
    assert await fakes.where(lab) == ["hue", "hue"]
    assert "light.test_group" not in lab.ha.entity_meta
    assert not journal.path.exists()


async def test_a_leftover_rig_zone_isnt_saved_as_a_bulbs_home(lab):
    lab.bridge.resources["zone"]["z9"] = {
        "id": "z9",
        "metadata": {"name": "ds-rig-test-group"},
        "children": [{"rid": lab.ha.entity_meta["light.lab_a"]["unique_id"], "rtype": "light"}],
    }
    moving = Roamer(lab.ha, lab.bridge, BULBS, out=lambda _: None)
    await moving.go("zha")
    assert moving.homes.get(A[2:])["hue"]["zones"] == ["z1"]


# --- the device check: every phase's devices before the gate, and --check -------------------------------------------

PLUG_AND_BULB = """
action: turn_on_for
rig_seconds: 5
tests:
  - name: t
    roles: [switch_plug, light_ct]
    steps:
      - set: {state: off}
"""


def plug_up(lab: fakes.Lab, platform: str = "shelly") -> None:
    """The plug is on, and registered on `platform`."""
    lab.ha.states["switch.plug"] = {"entity_id": "switch.plug", "state": "on", "attributes": {}}
    lab.ha.entity_devices["switch.plug"] = "plug-device"
    lab.ha.entity_meta["switch.plug"] = {"platform": platform, "unique_id": "plug", "disabled_by": None}


def leave_hue(lab: fakes.Lab, device_id: str) -> None:
    """The bridge deletes the bulb, which then searches for a network."""
    lab.bridge._delete("device", lab.bridge.resources["device"], device_id)


def check_suites(tmp_path: Path) -> list[rig.Suite]:
    path = tmp_path / "check.yaml"
    path.write_text(PLUG_AND_BULB)
    return [rig.Suite(load_step_file(path), integration) for integration in ("shelly", "zigbee2mqtt")]


async def check(tmp_path: Path, roaming: bool = True) -> list[str]:
    return await rig.check_devices(
        ENV, check_suites(tmp_path), Profile.load(lab_profile(tmp_path)), AdapterRegistry.discover(), None, roaming
    )


async def test_the_check_passes_with_every_fixed_entity_up_and_the_bulbs_home(tmp_path, lab, capsys):
    plug_up(lab)

    assert await check(tmp_path) == []

    out = capsys.readouterr().out
    assert "Checking profile lab: 1 entities, 2 roaming." in out  # a roaming bulb is no fixed entity
    assert "  light.lab_a: on Hue\n" in out
    assert "Every device is ready." in out


async def test_the_check_names_an_unavailable_or_missing_entity_and_a_bulb_on_no_network(tmp_path, lab, capsys):
    plug_up(lab)
    lab.ha.states["switch.plug"]["state"] = "unavailable"
    leave_hue(lab, "hue-device-2")  # lab_b leaves Hue and joins nothing

    problems = await check(tmp_path)

    assert problems[0] == "switch.plug: unavailable; check it is powered"
    assert problems[1].startswith("light.lab_b: not on any Zigbee network.")
    assert "2 problem(s): nothing was changed." in capsys.readouterr().out
    assert lab.ha.calls == []

    del lab.ha.states["switch.plug"]
    assert (await check(tmp_path))[0] == "switch.plug: not in HA; check profile lab"


async def test_ignoring_unreachables_an_unavailable_fixed_entity_goes_offline_and_its_cases_skip(tmp_path, lab, capsys):
    plug_up(lab)
    lab.ha.states["switch.plug"]["state"] = "unavailable"
    profile = Profile.load(lab_profile(tmp_path))
    suites = check_suites(tmp_path)
    registry = AdapterRegistry.discover()

    assert await rig.check_devices(ENV, suites, profile, registry, None, True, ignore_unreachables=True) == []
    assert profile.offline == {"switch.plug"}
    out = capsys.readouterr().out
    assert "switch.plug: unavailable; its cases skip (--ignore-unreachables)" in out
    rig.print_unreachable(suites, profile, registry, None)
    assert "  shelly / turn_on_for t[switch_plug]: switch.plug unreachable" in capsys.readouterr().out
    assert "switch.plug" not in rig.targets(suites, profile, registry, None)  # wait_for_targets waits for none

    lab.ha.states["light.lab_a"] = {"entity_id": "light.lab_a", "state": "unavailable", "attributes": {}}
    problems = await rig.check_devices(ENV, suites, profile, registry, None, True, ignore_unreachables=True)
    assert problems == ["light.lab_a: on Hue, unavailable; check it is powered"]  # a roaming bulb stays strict
    del lab.ha.states["switch.plug"]
    problems = await rig.check_devices(ENV, suites, profile, registry, None, False, ignore_unreachables=True)
    assert problems == ["switch.plug: not in HA; check profile lab"]


async def test_the_check_looks_only_at_the_cases_left_to_run(tmp_path, lab, capsys):
    lab.ha.states["switch.plug"] = {"entity_id": "switch.plug", "state": "unavailable", "attributes": {}}
    suites = check_suites(tmp_path)
    profile = Profile.load(lab_profile(tmp_path))
    done = {("shelly", "turn_on_for", "t[switch_plug]"): None}
    assert await rig.check_devices(ENV, suites, profile, AdapterRegistry.discover(), None, False, done=done) == []
    assert "Checking profile lab: 0 entities." in capsys.readouterr().out


async def test_the_check_says_a_bulb_away_from_hue_will_be_moved_and_finds_one_unpowered(tmp_path, lab, capsys):
    plug_up(lab)
    leave_hue(lab, "hue-device-2")
    lab.opened("zha")  # lab_b joins ZHA, under an entity ID of ZHA's own

    assert await check(tmp_path) == []
    assert "light.lab_b: on ZHA. The run moves it from there" in capsys.readouterr().out

    lab.ha.states["light.lab_a"] = {"entity_id": "light.lab_a", "state": "unavailable", "attributes": {}}
    assert await check(tmp_path) == ["light.lab_a: on Hue, unavailable; check it is powered"]


async def test_the_check_says_a_run_puts_back_a_home_the_home_record_holds(tmp_path, lab, capsys):
    plug_up(lab)
    leave_hue(lab, "hue-device-2")
    lab.opened("zha")
    profile = Profile.load(lab_profile(tmp_path))

    assert (
        await rig.check_devices(
            ENV, check_suites(tmp_path), profile, AdapterRegistry.discover(), None, True, {"light.lab_b"}
        )
        == []
    )
    assert "light.lab_b: on ZHA; the home record has its Hue home for a run to restore" in capsys.readouterr().out


async def test_the_check_reports_a_network_it_cannot_ask(tmp_path, lab):
    plug_up(lab)

    async def unreachable(ieee):
        raise fakes.BridgeError("bridge unreachable")

    lab.bridge.device_id_by_ieee = unreachable
    problems = await check(tmp_path)
    assert problems == [
        "light.lab_a: can't tell which network it is on: bridge unreachable",
        "light.lab_b: can't tell which network it is on: bridge unreachable",
    ]


async def test_the_check_looks_for_no_bulb_when_the_run_does_not_roam(tmp_path, lab, capsys):
    plug_up(lab)
    leave_hue(lab, "hue-device-2")
    assert await check(tmp_path, roaming=False) == []
    assert "Checking profile lab: 1 entities." in capsys.readouterr().out


async def test_the_check_needs_the_admin_token_and_bridge_to_find_the_bulbs():
    problems = await rig.check_roaming({}, None, [], fakes.FakeHA({}))
    assert problems == [
        "Can't find the roaming bulbs: that needs HA_ADMIN_TOKEN, HUE_BRIDGE_HOST and HUE_APP_KEY in .env.rig"
    ]


async def test_the_check_needs_ha_in_the_env(tmp_path):
    problems = await rig.check_devices({}, [], Profile(name="house"), AdapterRegistry.discover(), None, False)
    assert problems == ["Can't check the devices: HA_URL and HA_TOKEN must be set in .env.rig"]


async def test_the_check_reports_an_unreachable_ha(tmp_path, monkeypatch):
    async def refused(env):
        raise OSError("connection refused")

    monkeypatch.setattr(rig, "connect", refused)
    assert await check(tmp_path) == ["Can't reach HA at http://ha: connection refused"]


def main_with(tmp_path: Path, monkeypatch, problems: list[str]) -> list:
    """main's preflight against lab_profile, with check_devices answering `problems`; returns what it was asked."""
    path = lab_profile(tmp_path)
    asked = []

    async def check_devices(
        env, suites, profile, registry, keyword, roaming, homes=frozenset(), done=None, ignore_unreachables=False
    ):
        asked.append(([suite.integration for suite in suites], roaming))
        return problems

    monkeypatch.setattr(rig, "choose_profile", lambda name, env: path)
    monkeypatch.setattr(rig, "load_env", lambda: dict(ENV))
    monkeypatch.setattr(rig, "git", lambda *args, check=True: str(tmp_path) if "--git-common-dir" in args else "wt")
    monkeypatch.setattr(rig, "check_devices", check_devices)
    monkeypatch.setattr(rig, "ha_version", lambda env: "2026.10.1")
    monkeypatch.setattr(rig, "deploy_state", lambda status_cmd: rig.Deploy("c1", "tree", "tree", "tree", "wt"))
    return asked


def test_check_alone_takes_no_lock_and_deploys_nothing(tmp_path, monkeypatch, capsys):
    asked = main_with(tmp_path, monkeypatch, ["switch.plug: unavailable; check it is powered"])
    monkeypatch.setattr(rig, "deploy_state", lambda status_cmd: pytest.fail("--check read the deploy"))
    monkeypatch.setattr(rig.locks, "hold", lambda *args, **kwargs: pytest.fail("--check took a lock"))

    assert rig.main(["--check", "--integration", "zigbee2mqtt"]) == 1
    assert [(set(integrations), roaming) for integrations, roaming in asked] == [({"zigbee2mqtt"}, True)]
    assert "Proceed?" not in capsys.readouterr().out


def test_check_names_a_run_holding_a_lock_it_would_claim(tmp_path, monkeypatch, capsys):
    main_with(tmp_path, monkeypatch, [])
    with rig.locks.hold(rig.Claim(integrations=frozenset({"zha"})), "wt/other", tmp_path, out=lambda _: None):
        assert rig.main(["--check"]) == 0
    assert "  zha: wt/other (pid" in capsys.readouterr().out
    assert rig.locks.held_by(tmp_path / rig.locks.integration_lock("zha")) is None


def test_check_refuses_another_mode(capsys):
    with pytest.raises(SystemExit):
        rig.main(["--check", "--recover"])
    assert "--check can't be combined with --recover" in capsys.readouterr().err


def test_a_run_stops_before_the_gate_on_a_device_problem(tmp_path, monkeypatch):
    main_with(tmp_path, monkeypatch, ["switch.plug: unavailable; check it is powered"])
    monkeypatch.setattr(rig, "proceed", lambda decision: pytest.fail("asked to proceed"))

    with pytest.raises(SystemExit, match="Power or fix the devices above, then run again."):
        rig.main([])


def test_a_dry_run_shows_the_check_and_still_passes(tmp_path, monkeypatch, capsys):
    asked = main_with(tmp_path, monkeypatch, ["switch.plug: unavailable; check it is powered"])
    assert rig.main(["--dry-run"]) == 0
    assert asked and asked[0][1] is True  # the lab's bulbs roam for its ZHA and Z2M suites


SENSOR = "binary_sensor.rig_restricted"


def clear_run(tmp_path, monkeypatch, state: str) -> list:
    """main_with, with RIG_RESTRICTED_ENTITY reading `state` and the approved run stubbed; returns the watcher's
    arming."""
    main_with(tmp_path, monkeypatch, [])
    monkeypatch.setattr(rig, "load_env", lambda: {**ENV, "RIG_RESTRICTED_ENTITY": SENSOR})
    monkeypatch.setattr(rig, "_get", lambda url, token: {"state": state})
    seen = []
    monkeypatch.setattr(
        rig,
        "watching",
        lambda env, sensor, clear=False: seen.append((sensor is not None, clear)) or contextlib.nullcontext(),
    )
    monkeypatch.setattr(rig, "run_approved", lambda *args: 0)
    return seen


def pre_authorize(tmp_path) -> None:
    with (tmp_path / "lab.yaml").open("a") as profile:
        profile.write("pre_authorized: true\n")


def test_a_dry_run_prints_the_decision(tmp_path, monkeypatch, capsys):
    clear_run(tmp_path, monkeypatch, "on")
    assert rig.main(["--dry-run"]) == 0
    assert (
        f"Authorization: Restricted, and {SENSOR} is on. Needs DEVICE_SIDE_RIG_RESTRICTED=1" in capsys.readouterr().out
    )
    monkeypatch.setattr(rig, "_get", lambda url, token: {"state": "off"})
    assert rig.main(["--dry-run"]) == 0
    assert f"Authorization: Restricted; {SENSOR} is off, so it may go." in capsys.readouterr().out
    pre_authorize(tmp_path)
    assert rig.main(["--dry-run"]) == 0
    assert "Authorization: profile lab is pre-authorized. Unrestricted." in capsys.readouterr().out


# How the run is given its go-ahead; `""` gives none, "y" and "n" answer at a terminal.
RUN_GIVEN = ("", "approved", "y", "n", "pre-authorized")


@pytest.mark.parametrize(
    ("state", "given", "code", "armed", "says"),
    [
        pytest.param(
            "unavailable", "", 1, [], f"Restricted, and {SENSOR} is unavailable. Needs", id="not off: refused"
        ),
        pytest.param("off", "", 0, [(True, True)], None, id="off: goes unasked, watched"),
        # Approved, it is watched with no read of off yet: it waits for one before an on counts.
        pytest.param("on", "approved", 0, [(True, False)], None, id="approved: goes while on, watched for on"),
        pytest.param("on", "y", 0, [(True, False)], f"Restricted: {SENSOR} is on. Proceed anyway? [y/N]", id="yes"),
        pytest.param("off", "n", 1, [], "Proceed? [y/N]", id="a terminal asks a clear run if it is ready"),
        pytest.param("on", "pre-authorized", 0, [(False, False)], None, id="pre-authorized: no read, not watched"),
    ],
)
def test_the_run_gate(tmp_path, monkeypatch, capsys, state, given, code, armed, says):
    """An agent's run (no terminal) is never asked; a human's answer at one is the approval."""
    assert given in RUN_GIVEN
    seen = clear_run(tmp_path, monkeypatch, state)
    if given == "approved":
        monkeypatch.setenv("DEVICE_SIDE_RIG_RESTRICTED", "1")
    if given in ("y", "n"):
        monkeypatch.setattr(rig, "tty", lambda: True)
        monkeypatch.setattr(sys, "stdin", io.StringIO(f"{given}\n"))
    if given == "pre-authorized":
        pre_authorize(tmp_path)
        monkeypatch.setattr(rig, "_get", lambda url, token: pytest.fail("read the sensor"))
    assert rig.main([]) == code
    assert seen == armed
    out = capsys.readouterr().out
    if says is not None:
        assert says in out
    assert ("Proceed" in out) is (given in ("y", "n"))


def test_a_run_plans_lanes_unless_serial(tmp_path, monkeypatch, capsys):
    main_with(tmp_path, monkeypatch, [])
    assert rig.main(["--dry-run"]) == 0
    assert "In parallel, one lane per integration:" in capsys.readouterr().out
    assert rig.main(["--dry-run", "--serial"]) == 0
    assert "In parallel" not in capsys.readouterr().out


def test_roam_check_skips_the_device_check(tmp_path, monkeypatch):
    asked = main_with(tmp_path, monkeypatch, [])
    assert rig.main(["--roam-check", "--dry-run"]) == 0
    assert asked == []


async def test_connect_closes_the_first_session_when_the_admin_one_fails(monkeypatch):
    closed = []

    class Client:
        def __init__(self, url, token):
            self.token = token

        async def connect(self):
            if self.token == "a":
                raise RuntimeError("WebSocket auth failed")

        async def close(self):
            closed.append(self.token)

    monkeypatch.setattr(rig, "HAClient", Client)
    with pytest.raises(RuntimeError, match="auth failed"):
        await rig.connect(ENV)
    assert closed == ["t"]


async def test_the_check_reports_an_entity_on_another_platform(tmp_path, lab):
    plug_up(lab, "hue")

    assert await check(tmp_path, roaming=False) == [
        "switch.plug: a hue entity, not shelly; pair it back or fix profile lab"
    ]


async def test_the_check_reports_an_entity_the_registry_lacks(tmp_path, lab):
    plug_up(lab)
    lab.ha.entity_devices.pop("switch.plug")
    lab.ha.entity_meta.pop("switch.plug")

    problems = await check(tmp_path, roaming=False)

    assert len(problems) == 1 and problems[0].startswith("switch.plug: not in the entity registry")


WRAPPED = """
action: turn_on_for
rig_seconds: 5
tests:
  - name: t
    roles: [light_wrapped]
    steps:
      - set: {state: off}
"""


@pytest.mark.parametrize(
    ("integration", "role", "platform", "problem"),
    [
        ("shelly", "light_wrapped", "switch_as_x", False),
        ("shelly", "light_wrapped", "shelly", True),
        ("zigbee2mqtt", "switch_plug", "mqtt", False),
        ("zha", "switch_plug", "hue", True),
        ("zha", "switch_plug", "zha", False),
    ],
)
async def test_each_integrations_entity_must_be_on_its_ha_platform(tmp_path, lab, integration, role, platform, problem):
    path = tmp_path / "steps.yaml"
    path.write_text(WRAPPED.replace("light_wrapped", role))
    profile = Profile({integration: [RigEntity("switch.plug", frozenset({role}))]}, name="lab")
    plug_up(lab, platform)

    problems = await rig.check_devices(
        ENV, [rig.Suite(load_step_file(path), integration)], profile, AdapterRegistry.discover(), None, False
    )

    assert bool(problems) is problem


def budget_run(tmp_path, lab, budget):
    return rig.run_rig(
        ENV,
        suites(tmp_path, "zha"),
        lab_with_pause(tmp_path),
        None,
        None,
        True,
        None,
        None,
        roaming=True,
        budget=budget,
    )


async def test_a_budget_with_under_a_minute_left_makes_no_move(tmp_path, monkeypatch, lab):
    ran = fake_phases(monkeypatch, lab)
    budget = engine.Budget(time.monotonic() + rig.MOVE_LEAD_S - 10)

    await budget_run(tmp_path, lab, budget)

    assert budget.stopped
    assert all("zha" not in integrations for integrations, _ in ran)
    assert await fakes.where(lab) == ["hue", "hue"]


async def test_a_budget_spent_during_a_move_still_runs_the_phases_first_case(tmp_path, monkeypatch, lab):
    budget = engine.Budget(time.monotonic() + rig.MOVE_LEAD_S + 5)
    seen = []

    async def run(phase_suites, *args, **kwargs):
        seen.append(([s.integration for s in phase_suites], await fakes.where(lab), budget.stops(device_case=True)))
        return engine.RunResult([])

    async def wait_for_targets(*args, **kwargs):
        pass

    monkeypatch.setattr(rig, "run", run)
    monkeypatch.setattr(rig, "wait_for_targets", wait_for_targets)
    monkeypatch.setattr(rig, "write_report", lambda result, directory, title: directory / "report.md")
    real_go = Roamer.go

    async def slow_go(self, to):
        await real_go(self, to)
        if to == "zha":
            budget.deadline = time.monotonic() - 1  # spent during the move

    monkeypatch.setattr(Roamer, "go", slow_go)

    await budget_run(tmp_path, lab, budget)

    assert ("zha" in str(seen)) and seen[-1][2] is False


def signal_mid_move(
    monkeypatch, lab: fakes.Lab, tmp_path: Path, at: str, then: Callable[[], None] = lambda: None
) -> tuple[Coroutine, Journal, list]:
    """A run from Hue through ZHA to Z2M, to await in the test's task, which is cancelled as cancel_on_signals
    cancels the run's on a Ctrl-C: `at` the release from ZHA, or as Z2M opens for the join, both on the way to Z2M; or
    `home`, at the release from Z2M on the way back; `then` runs as it comes. Returns the run, its journal, and a list
    that holds True once the signal came."""
    fake_phases(monkeypatch, lab)
    journal = Journal(tmp_path / "journal.json")
    profile = lab_with_pause(tmp_path)
    run = rig.run_rig(
        ENV, suites(tmp_path, "zha", "zigbee2mqtt"), profile, None, None, True, None, journal, roaming=True
    )
    task = asyncio.current_task()
    signalled: list[bool] = []

    def signal() -> None:
        if not signalled:
            signalled.append(True)
            task.cancel()
            then()

    left, opened = lab.ha.on_leave, lab.ha.on_open

    def leave(integration, ieee):
        left(integration, ieee)
        if (at, integration) in {("release", "zha"), ("home", "zigbee2mqtt")}:
            signal()

    def open_(network):
        opened(network)
        if at == "join" and network == "zigbee2mqtt":
            signal()

    lab.ha.on_leave, lab.ha.on_open = leave, open_
    return run, journal, signalled


def heeding_a_pending_cancel(lab: fakes.Lab) -> None:
    """HA calls that stop at a cancel the task still carries, as an await that defers to task.cancelling() does: the
    way home must not run in a task that carries the signal's. The fakes' calls rarely suspend, so without this a
    cancel finds no await to land on before the bulbs are home, which is why the fakes never showed issue #11."""
    call_service = lab.ha.call_service

    async def heeding(*args, **kwargs):
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        return await call_service(*args, **kwargs)

    lab.ha.call_service = heeding


@pytest.mark.parametrize("at", ["release", "join", "home"])
@pytest.mark.parametrize("heeds", [False, True])
async def test_a_signal_mid_move_still_brings_the_bulbs_home(tmp_path, monkeypatch, lab, at, heeds):
    """Issue #11: one Ctrl-C mid-move, away or on the way home, ends the run with the bulbs home and no roam left."""
    run, journal, signalled = signal_mid_move(monkeypatch, lab, tmp_path, at)
    if heeds:
        heeding_a_pending_cancel(lab)

    with pytest.raises(asyncio.CancelledError):
        await run

    assert signalled
    assert await fakes.where(lab) == ["hue", "hue"]
    assert lab.ha.states["automation.lab"]["state"] == "on"
    assert not journal.path.exists()


async def test_a_second_signal_mid_move_leaves_the_bulbs_where_they_are_and_keeps_the_journal(
    tmp_path, monkeypatch, lab
):
    """The first signal started the way home; a second one, which cancels the move itself as asyncio.run does once the
    default handlers are back, stops the run at once: no bulb is moved on, and the journal keeps where they are."""
    armed: list[bool] = []
    run, journal, _ = signal_mid_move(monkeypatch, lab, tmp_path, "join", then=lambda: armed.append(True))
    left = lab.ha.on_leave
    stopped: list[bool] = []

    def leave(integration, ieee):
        left(integration, ieee)
        if armed and not stopped:  # the first release on the way home
            stopped.append(True)
            asyncio.current_task().cancel()  # the way home's own task: sheltered from the first signal only

    lab.ha.on_leave = leave
    heeding_a_pending_cancel(lab)

    try:
        with pytest.raises(asyncio.CancelledError):
            await run
    finally:  # the process would be gone; the run's log readers would not
        for task in asyncio.all_tasks() - {asyncio.current_task()}:
            task.cancel()

    assert stopped
    assert await fakes.where(lab) != ["hue", "hue"]
    assert "roam" in Journal(journal.path).sections


async def test_a_signal_on_the_way_home_lets_the_cleanup_after_it_finish(tmp_path, monkeypatch, lab):
    """The signal that came during the move home stops the run only once everything is put back."""
    run, _, signalled = signal_mid_move(monkeypatch, lab, tmp_path, "home")
    closed = []
    monkeypatch.setattr(lab.bridge, "close", lambda: closed.append(True) or asyncio.sleep(0))

    with pytest.raises(asyncio.CancelledError):
        await run

    assert signalled and closed
    assert lab.ha.states["automation.lab"]["state"] == "on"


@pytest.mark.parametrize("fails", ["join", "cancel"])
async def test_an_abort_that_cant_bring_the_bulbs_home_says_so_and_keeps_the_journal(
    tmp_path, monkeypatch, lab, capsys, fails
):
    """After a Ctrl-C, a move home that fails (a bulb that doesn't join Hue, or a cancel no signal sent) exits nonzero,
    saying the bulbs aren't home, with the journal kept for --recover: not "Aborted." as though all is well."""
    if fails == "join":
        monkeypatch.setattr(roam, "JOIN_S", 0.02)
        run, journal, _ = signal_mid_move(monkeypatch, lab, tmp_path, "join", then=lambda: lab.stuck.add(A[2:]))
    else:
        run, journal, _ = signal_mid_move(monkeypatch, lab, tmp_path, "join")
        release = roam.Roamer.release

        async def stray(self, network, bulb):
            if network == "zigbee2mqtt":
                raise asyncio.CancelledError
            await release(self, network, bulb)

        monkeypatch.setattr(roam.Roamer, "release", stray)

    with pytest.raises(SystemExit, match="Aborted with the roaming bulbs not home"):
        await run

    assert "Could not move the roaming bulbs home" in capsys.readouterr().out
    assert "roam" in Journal(journal.path).sections
    assert lab.ha.states["automation.lab"]["state"] == "on"
