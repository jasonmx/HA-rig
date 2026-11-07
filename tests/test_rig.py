"""scripts/rig.py's preflight and gate. Nothing here touches the rig: no ssh, no HA."""

import asyncio
import importlib.util
import itertools
import json
import os
import signal
import threading
import types
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest
from custom_components.deviceside.core.registry import AdapterRegistry

from ha_rig import baseline, engine
from ha_rig.engine import load_step_file, load_step_files
from ha_rig.journal import LEGACY_NAME, Journal
from ha_rig.profile import EXAMPLE, Profile, RigEntity, load_env, main_checkout, profile_paths
from ha_rig.progress import path_for
from tests import rig_fakes as fakes
from tests.rig_fakes import write

RIG_HOME = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("rig", RIG_HOME / "scripts" / "rig.py")
rig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rig)


@pytest.fixture(autouse=True)
def reports_in_tmp(tmp_path, monkeypatch):
    """A run's report and captured logs go to the test's folder, not the system temp one."""
    monkeypatch.setattr(rig, "REPORT_DIR", tmp_path / "reports")


@pytest.fixture
def profile(tmp_path) -> Profile:
    return Profile.load(
        write(
            tmp_path,
            "house.yaml",
            "shelly:\n  - entity_id: switch.plug\n    roles: [switch_plug]\nhue:\n  - entity_id: light.lamp\n"
            "    roles: [light_color]\n",
        )
    )


def test_profile_rejects_unknown_role(tmp_path):
    path = write(tmp_path, "house.yaml", "shelly:\n  - entity_id: switch.plug\n    roles: [plug]\n")
    with pytest.raises(ValueError, match="unknown roles"):
        Profile.load(path)


def test_profile_rejects_unknown_integration(tmp_path):
    path = write(tmp_path, "house.yaml", "z2m:\n  - entity_id: switch.plug\n    roles: [switch_plug]\n")
    with pytest.raises(ValueError, match="unknown integration 'z2m'"):
        Profile.load(path)


@pytest.mark.parametrize(
    ("text", "message"),
    [("pause: [switch.a]1\n", "house.yaml: not valid YAML"), ("- switch.a\n", "house.yaml: must be a mapping")],
)
def test_profile_names_its_file_when_it_cannot_be_read(tmp_path, text, message):
    with pytest.raises(ValueError, match=message):
        Profile.load(write(tmp_path, "house.yaml", text))


def test_profile_maps_each_powered_entity_to_its_supply(tmp_path):
    path = write(
        tmp_path,
        "house.yaml",
        "zha:\n  - entity_id: light.bulb\n    roles: [light_ct]\n    powered_by: switch.plug\n"
        "  - entity_id: switch.plug\n    roles: [switch_plug]\n",
    )
    assert Profile.load(path).power() == {"light.bulb": "switch.plug"}


def test_profile_rejects_an_unlisted_supply(tmp_path):
    path = write(
        tmp_path, "house.yaml", "zha:\n  - entity_id: light.bulb\n    roles: [light_ct]\n    powered_by: switch.x\n"
    )
    with pytest.raises(ValueError, match="powered_by switch.x, which the profile doesn't list"):
        Profile.load(path)


def test_profile_reads_a_groups_members(tmp_path):
    path = write(
        tmp_path,
        "house.yaml",
        "zigbee2mqtt:\n  - entity_id: light.g\n    roles: [group]\n    members: [light.a, light.b]\n",
    )
    profile = Profile.load(path)
    assert profile.members("zigbee2mqtt", "light.g") == ("light.a", "light.b")
    assert profile.members("zigbee2mqtt", "light.other") == ()


def test_profile_reads_a_group_colors_members(tmp_path):
    path = write(
        tmp_path,
        "house.yaml",
        "zigbee2mqtt:\n  - entity_id: light.g\n    roles: [group_color]\n    members: [light.a]\n",
    )
    assert Profile.load(path).members("zigbee2mqtt", "light.g") == ("light.a",)


def test_profile_rejects_members_without_the_group_role(tmp_path):
    path = write(
        tmp_path, "house.yaml", "zha:\n  - entity_id: light.a\n    roles: [light_ct]\n    members: [light.b]\n"
    )
    with pytest.raises(ValueError, match="lists members but has no group role"):
        Profile.load(path)


def test_profile_rejects_members_given_as_one_string(tmp_path):
    path = write(tmp_path, "house.yaml", "zha:\n  - entity_id: light.g\n    roles: [group]\n    members: light.a\n")
    with pytest.raises(ValueError, match="members must be a list of entity IDs"):
        Profile.load(path)


def test_profile_entity_by_role(profile):
    assert profile.entity("shelly", "switch_plug") == "switch.plug"
    assert profile.entity("shelly", "light_dimmable") is None
    assert profile.entity("zha", "switch_plug") is None


def test_load_env_reads_file_and_env_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("RIG_SYNC_CMD", raising=False)
    monkeypatch.delenv("RIG_STATUS_CMD", raising=False)
    monkeypatch.delenv("RIG_RESTART_CMD", raising=False)
    monkeypatch.setenv("HA_TOKEN", "from-env")
    env = load_env(write(tmp_path, ".env.rig", "# comment\nHA_URL='http://ha:8123'\nHA_TOKEN=from-file\n"))
    assert env == {"HA_URL": "http://ha:8123", "HA_TOKEN": "from-env"}


def test_load_env_reads_command_vars(tmp_path, monkeypatch):
    for key in ("HA_URL", "HA_TOKEN", "RIG_SYNC_CMD", "RIG_STATUS_CMD", "RIG_RESTART_CMD"):
        monkeypatch.delenv(key, raising=False)
    env = load_env(
        write(
            tmp_path,
            ".env.rig",
            "HA_URL=http://ha:8123\nHA_TOKEN=tok\nRIG_SYNC_CMD=ssh myhost sync\nRIG_STATUS_CMD=ssh myhost status\n",
        )
    )
    assert env["RIG_SYNC_CMD"] == "ssh myhost sync"
    assert env["RIG_STATUS_CMD"] == "ssh myhost status"
    assert "RIG_RESTART_CMD" not in env


def test_select_plan_lines_and_estimate(profile):
    step_files = [step_file for step_file in load_step_files() if step_file.action in ("turn_off_for", "turn_on_for")]
    suites = rig.select(step_files, profile, None)
    assert [(suite.integration, suite.action) for suite in suites] == [
        ("shelly", "turn_off_for"),
        ("hue", "turn_off_for"),
        ("shelly", "turn_on_for"),
        ("hue", "turn_on_for"),
    ]
    assert [suite.integration for suite in rig.select(step_files, profile, ["hue"])] == ["hue", "hue"]
    narrowed = rig.select(step_files, profile, ["hue"], ["turn_on_for"])
    assert [(suite.integration, suite.action) for suite in narrowed] == [("hue", "turn_on_for")]

    registry = AdapterRegistry.discover()
    shelly_on = [suite for suite in suites if suite.integration == "shelly" and suite.action == "turn_on_for"]
    assert rig.plan_lines(shelly_on + suites[1:2], profile, registry) == [
        "shelly / turn_on_for -> switch.plug",
        "    on_then_off[switch_plug]: switch.plug",
        "    on_then_off[light_onoff]: no light_onoff entity in the profile",
        "    on_then_off[light_wrapped]: no light_wrapped entity in the profile",
        "    drops_brightness_on_a_wrapper[light_wrapped]: no light_wrapped entity in the profile",
        "    drops_brightness_on_a_relay_light[light_onoff]: no light_onoff entity in the profile",
        "    bulb_on_then_off[light_dimmable]: no light_dimmable entity in the profile",
        "    bulb_brightness_then_off[light_dimmable]: no light_dimmable entity in the profile",
        "    bulb_color_temp_then_off[light_ct]: no light_ct entity in the profile",
        "    bulb_transition_then_off[light_dimmable]: no light_dimmable entity in the profile",
        "    rgbcct_bulb_rgb_then_off[light_color]: no light_color entity in the profile",
        "    rgbcct_bulb_hs_then_off[light_color]: no light_color entity in the profile",
        "    rgbcct_bulb_xy_then_off[light_color]: no light_color entity in the profile",
        "hue / turn_off_for (no adapter, will skip)",
    ]
    assert rig.plan_lines(shelly_on, profile, registry, "on_then") == [
        "shelly / turn_on_for -> switch.plug",
        "    on_then_off[switch_plug]: switch.plug",
        "    on_then_off[light_onoff]: no light_onoff entity in the profile",
        "    on_then_off[light_wrapped]: no light_wrapped entity in the profile",
        "    bulb_on_then_off[light_dimmable]: no light_dimmable entity in the profile",
        "    bulb_transition_then_off[light_dimmable]: no light_dimmable entity in the profile",
    ]

    shelly_seconds = sum(step_file.rig_seconds for step_file in step_files)
    assert rig.estimate(suites, registry, restarting=False) == shelly_seconds
    assert rig.estimate(suites, registry, restarting=True) == shelly_seconds + rig.RESTART_ESTIMATE_S


@pytest.mark.parametrize(("answer", "approved"), [("y", True), ("YES", True), ("", False), ("n", False)])
def test_a_prompt_needs_an_explicit_yes(answer, approved, capsys):
    assert rig.proceed(rig.Decision(True, "Proceed?", False, ""), lambda _: answer) is approved
    assert ("Not approved; nothing changed." in capsys.readouterr().out) is not approved


def test_a_prompt_without_a_terminal_is_a_no():
    def no_tty(_):
        raise EOFError

    assert rig.proceed(rig.Decision(True, "Proceed?", False, ""), no_tty) is False


def test_a_refusal_says_why_and_asks_nothing(capsys):
    assert (
        rig.proceed(rig.Decision(False, None, True, "Restricted, and x is on."), lambda _: pytest.fail("asked"))
        is False
    )
    assert "Restricted, and x is on. Nothing changed." in capsys.readouterr().out


def test_a_decision_with_no_prompt_goes_unasked():
    assert rig.proceed(rig.Decision(True, None, False, ""), lambda _: pytest.fail("asked")) is True


def test_the_approval_is_the_new_variable_and_the_old_one_is_refused(monkeypatch):
    assert rig.approved() is False
    monkeypatch.setenv("DEVICE_SIDE_RIG_RESTRICTED", "1")
    assert rig.approved() is True
    monkeypatch.setenv("DEVICE_SIDE_RIG_APPROVED", "1")
    with pytest.raises(SystemExit, match="DEVICE_SIDE_RIG_APPROVED is gone.*DEVICE_SIDE_RIG_RESTRICTED=1"):
        rig.approved()


def test_main_refuses_the_old_variable_in_every_mode(monkeypatch):
    monkeypatch.setenv("DEVICE_SIDE_RIG_APPROVED", "1")
    monkeypatch.setattr(rig, "wait_or_run", lambda args: pytest.fail("ran"))
    for argv in ([], ["--deploy-only"], ["--recover"]):
        with pytest.raises(SystemExit, match="DEVICE_SIDE_RIG_APPROVED is gone"):
            rig.main(argv)


def fake_ha(monkeypatch, replies):
    """Each _get call pops the next reply; an exception instance is raised instead of returned."""

    def _get(url, token):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(rig, "_get", _get)


SENSOR = "binary_sensor.rig_restricted"
CLEAR_ENV = {"HA_URL": "http://ha", "HA_TOKEN": "t", "RIG_RESTRICTED_ENTITY": SENSOR}


def not_found():
    return urllib.error.HTTPError("http://ha", 404, "Not Found", {}, None)


@pytest.mark.parametrize(
    ("reply", "clear", "reason"),
    [
        ({"state": "off"}, True, "binary_sensor.rig_restricted is off"),
        ({"state": "on"}, False, "binary_sensor.rig_restricted is on"),
        ({"state": "unknown"}, False, "binary_sensor.rig_restricted is unknown"),
        ({"state": "unavailable"}, False, "binary_sensor.rig_restricted is unavailable"),
        ({"state": "off", "attributes": {"restored": True}}, False, "still being restored"),
        (urllib.error.URLError("refused"), False, "can't read binary_sensor.rig_restricted"),
        (None, False, "binary_sensor.rig_restricted is not in HA"),
    ],
)
def test_rig_clear_only_on_on(monkeypatch, reply, clear, reason):
    fake_ha(monkeypatch, [not_found() if reply is None else reply])
    answer = rig.rig_clear(CLEAR_ENV, SENSOR)
    assert answer[0] is clear
    assert reason in answer[1]


def test_rig_clear_reads_the_named_entity(monkeypatch):
    urls = []
    monkeypatch.setattr(rig, "_get", lambda url, token: urls.append((url, token)) or {"state": "off"})
    rig.rig_clear(CLEAR_ENV, SENSOR)
    assert urls == [("http://ha/api/states/binary_sensor.rig_restricted", "t")]


@pytest.mark.parametrize(
    ("env", "sensor", "noted"),
    [
        (CLEAR_ENV, SENSOR, False),
        ({"RIG_BLOCKER_ENTITY": "binary_sensor.old"}, "binary_sensor.old", True),
        ({**CLEAR_ENV, "RIG_BLOCKER_ENTITY": "binary_sensor.old"}, SENSOR, False),
        ({"RIG_RESTRICTED_ENTITY": "", "RIG_BLOCKER_ENTITY": ""}, None, False),
    ],
)
def test_the_restricted_sensor_falls_back_to_its_old_name(env, sensor, noted, capsys):
    assert rig.restricted_sensor(env) == sensor
    assert ("RIG_BLOCKER_ENTITY in .env.rig is now RIG_RESTRICTED_ENTITY" in capsys.readouterr().out) is noted


def test_authority_reads_the_sensor_once_and_only_for_restricted_work(monkeypatch):
    reads = []
    monkeypatch.setattr(rig, "_get", lambda url, token: reads.append(url) or {"state": "off"})
    authority = rig.Authority(CLEAR_ENV)
    assert authority.decide(restricted=False).reason == "Unrestricted."
    assert reads == []
    assert authority.decide(restricted=True).go
    assert authority.decide(restricted=True).watch
    assert reads == ["http://ha/api/states/binary_sensor.rig_restricted"]


def test_profile_pre_authorized_must_be_a_bool(tmp_path):
    with pytest.raises(ValueError, match="pre_authorized must be true or false, got 'yes please'"):
        Profile.load(write(tmp_path, "house.yaml", "pre_authorized: yes please\nshelly:\n"))


def watch(monkeypatch, replies, armed=True, clear=False):
    """Run the watcher over `replies`, one per poll, until it signals or they run out; the signals it sent."""
    sent = []
    done = threading.Event()
    replies = list(replies)

    def _get(url, token):
        if not replies:
            done.set()
            return {"state": "off"}
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def kill(pid, signum):
        sent.append((pid, signum))
        done.set()

    monkeypatch.setattr(rig, "_get", _get)
    monkeypatch.setattr(rig.os, "kill", kill)
    with rig.watching(CLEAR_ENV, SENSOR if armed else None, clear, interval=0.001):
        done.wait(0 if not armed else 5)
    return sent


def test_two_reads_not_clear_in_a_row_abort_the_run(monkeypatch, capsys):
    assert watch(monkeypatch, [{"state": "off"}, {"state": "on"}, {"state": "unknown"}]) == [
        (os.getpid(), signal.SIGTERM)
    ]
    assert "Rig authorization revoked (binary_sensor.rig_restricted is unknown); aborting." in capsys.readouterr().out


def test_one_read_not_clear_does_not(monkeypatch):
    assert watch(monkeypatch, [{"state": "on"}, {"state": "off"}, {"state": "on"}, {"state": "off"}]) == []


DOWN = urllib.error.URLError("refused")
ABORTED = [(os.getpid(), signal.SIGTERM)]


@pytest.mark.parametrize(
    ("states", "aborts"),
    [
        (["off", "on", "on"], True),
        (["on", "on", "on"], False),  # approved while on: it never turned on
        (["on", "off", "on", "on"], True),
        (["off", DOWN, "on", "on"], True),
        (["off", "on", DOWN, "on"], True),  # a failed read doesn't reset the two
        (["unknown", "unavailable", "on"], False),
    ],
)
def test_the_watcher_aborts_when_the_sensor_turns_on(monkeypatch, states, aborts):
    replies = [state if isinstance(state, Exception) else {"state": state} for state in states]
    assert watch(monkeypatch, replies) == (ABORTED if aborts else [])


def test_the_gates_read_of_off_counts_as_one_since_the_start(monkeypatch):
    assert watch(monkeypatch, [{"state": "on"}, {"state": "on"}], clear=True) == ABORTED


def test_failed_reads_neither_count_nor_reset(monkeypatch):
    assert watch(monkeypatch, [DOWN, DOWN, DOWN, {"state": "on"}, DOWN, {"state": "on"}], clear=True) == ABORTED
    restored = {"state": "unavailable", "attributes": {"restored": True}}
    assert watch(monkeypatch, [restored, restored, not_found(), not_found()], clear=True) == []


def test_an_unarmed_watcher_reads_nothing(monkeypatch):
    assert watch(monkeypatch, [{"state": "on"}, {"state": "on"}], armed=False) == []


def fake_clock(monkeypatch):
    """rig's own clock ticks 10 s per read; patching time.monotonic itself would break the event loop."""
    ticks = itertools.count(0, 10)
    monkeypatch.setattr(rig, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))


def test_wait_until_ready_returns_once_the_service_is_listed(monkeypatch):
    services = [{"domain": "deviceside", "services": {"turn_on_for": {}}}]
    running = {"state": "RUNNING"}
    fake_ha(monkeypatch, [rig.urllib.error.URLError("down"), running, None, running, services])
    rig.wait_until_ready("http://ha", "token", timeout=60, sleep=lambda _: None)


def test_wait_until_ready_reports_a_crash_after_running_as_not_running(monkeypatch):
    fake_clock(monkeypatch)
    fake_ha(monkeypatch, [{"state": "RUNNING"}, [], *[OSError("refused")] * 5])
    with pytest.raises(SystemExit, match="did not report RUNNING"):
        rig.wait_until_ready("http://ha", "token", timeout=35, sleep=lambda _: None)


def test_wait_until_ready_says_why_the_service_is_missing(monkeypatch):
    fake_clock(monkeypatch)
    fake_ha(monkeypatch, [{"state": "RUNNING"}, []] * 5)

    async def config_entries(url, token):
        assert token == "admin"
        return [{"domain": "deviceside", "state": "not_loaded"}, {"domain": "mqtt", "state": "setup_in_progress"}]

    monkeypatch.setattr(rig, "config_entries", config_entries)
    with pytest.raises(SystemExit, match="entry is not_loaded; it waits for mqtt"):
        rig.wait_until_ready("http://ha", "token", "admin", timeout=35, sleep=lambda _: None)


def test_wait_until_ready_says_why_ha_is_still_starting(monkeypatch):
    fake_clock(monkeypatch)
    fake_ha(monkeypatch, [{"state": "NOT_RUNNING"}] * 5)
    monkeypatch.setattr(rig, "diagnose", lambda url, admin_token: "zha (setup_in_progress).")
    with pytest.raises(SystemExit, match=r"still starting after 35s \(NOT_RUNNING\)\. zha \(setup_in_progress\)"):
        rig.wait_until_ready("http://ha", "token", "admin", timeout=35, sleep=lambda _: None)


WAITS_FOR = ["hue", "zha"]


@pytest.mark.parametrize(
    ("entries", "says"),
    [
        ([], "On a first load, add the integration"),
        (
            [
                {"domain": "deviceside", "state": "not_loaded"},
                {"domain": "hue", "state": "loaded"},
                {"domain": "zha", "state": "setup_in_progress"},
                {"domain": "zha", "state": "not_loaded", "disabled_by": "user"},
                {"domain": "met", "state": "setup_error"},
            ],
            "DeviceSide's entry is not_loaded; it waits for zha (setup_in_progress). "
            "DeviceSide actions are unavailable in the house until this is fixed.",
        ),
        (
            [{"domain": "deviceside", "state": "setup_error"}, {"domain": "hue", "state": "loaded"}],
            "DeviceSide's entry is setup_error; none of the integrations it waits for (hue, zha) is stuck; "
            "check HA's logs.",
        ),
    ],
)
def test_explain_a_missing_service(entries, says):
    assert says in rig.explain(entries, WAITS_FOR)


def test_diagnose_without_an_admin_token_says_what_it_could_not_check():
    assert "Without HA_ADMIN_TOKEN" in rig.diagnose("http://ha", None)


def test_diagnose_says_when_it_cannot_read_the_entries(monkeypatch):
    async def config_entries(url, token):
        raise OSError("refused")

    monkeypatch.setattr(rig, "config_entries", config_entries)
    assert rig.diagnose("http://ha", "admin").startswith("Could not read config entries")


@pytest.mark.parametrize(
    ("output", "code", "trees"),
    [
        ("synced aaa\nloaded bbb\n", 0, ("aaa", "bbb")),
        ("synced aaa\n", 0, ("aaa", None)),
        ("aaa\n", 0, ("aaa", "aaa")),
        ("", 91, (None, None)),
    ],
)
def test_deployed_trees_reads_both_formats(tmp_path, output, code, trees):
    script = write(tmp_path, "status.sh", f"printf '{output}'\nexit {code}\n")
    assert rig.deployed_trees(f"sh {script}") == trees


def test_deployed_trees_stops_on_a_failing_status_command(tmp_path):
    script = write(tmp_path, "status.sh", "echo boom >&2\nexit 2\n")
    with pytest.raises(SystemExit, match="failed \\(2\\): boom"):
        rig.deployed_trees(f"sh {script}")


def fake_git(monkeypatch, *, dirty="", remote="abc refs/heads/b", remote_tree="tree1", deployed=("old", "old")):
    replies = {
        "rev-parse --abbrev-ref HEAD": "b",
        "rev-parse HEAD": "c1",
        f"rev-parse c1:{rig.COMPONENT}": "tree1",
        f"status --porcelain -- {rig.COMPONENT}": dirty,
        "ls-remote origin refs/heads/b": remote,
        f"rev-parse --quiet --verify abc:{rig.COMPONENT}": remote_tree,
    }
    monkeypatch.setattr(rig, "git", lambda *args, check=True: replies[" ".join(args)])
    monkeypatch.setattr(rig, "deployed_trees", lambda status_cmd: deployed)


@pytest.mark.parametrize(
    ("kwargs", "needed", "blocker"),
    [
        ({}, True, None),
        ({"deployed": ("tree1", "tree1")}, False, None),
        ({"deployed": ("tree1", "old")}, True, None),  # synced but never loaded: a failed restart
        ({"deployed": (None, None)}, True, None),
        ({"dirty": " M x.py"}, True, "uncommitted changes"),
        ({"remote": ""}, True, "not on origin"),
        ({"remote_tree": "tree0"}, True, "does not match HEAD"),
    ],
)
def test_deploy_state(monkeypatch, kwargs, needed, blocker):
    fake_git(monkeypatch, **kwargs)
    state = rig.deploy_state("ssh myhost status")
    assert (state.commit, state.local_tree) == ("c1", "tree1")
    assert state.needed is needed
    assert (state.blocker is None) if blocker is None else (blocker in state.blocker)


@pytest.mark.parametrize(
    ("deployed", "deploying", "loaded_after", "commit"),
    [
        (("tree1", "tree1"), False, None, "c1"),  # HA already runs this checkout
        (("old", "old"), False, None, None),  # --no-deploy: HA runs another tree
        (("old", "old"), True, "tree1", "c1"),
        (("old", "old"), True, "tree2", None),  # origin moved between the plan and the deploy
        (("old", "old"), True, None, "c1"),  # the status command can't say what HA loaded
    ],
)
def test_tested_commit_names_the_tree_ha_loaded(monkeypatch, deployed, deploying, loaded_after, commit):
    fake_git(monkeypatch, deployed=deployed)
    deploy = rig.deploy_state("ssh myhost status")
    monkeypatch.setattr(rig, "deployed_trees", lambda status_cmd: (loaded_after, loaded_after))
    assert rig.tested_commit("ssh myhost status", deploy, deploying) == commit


def test_tested_commit_trusts_a_deploy_without_a_status_command(monkeypatch):
    fake_git(monkeypatch, deployed=(None, None))
    assert rig.tested_commit("", rig.deploy_state(""), deploying=True) == "c1"


def run_git(*args: str) -> None:
    rig.subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
    )


def test_main_checkout_from_a_linked_worktree(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    main, linked = tmp_path / "main", tmp_path / "linked"
    run_git("init", "-q", str(main))
    run_git("-C", str(main), "commit", "-q", "--allow-empty", "-m", "root")
    run_git("-C", str(main), "worktree", "add", "-q", str(linked))
    assert main_checkout(linked).resolve() == main.resolve()
    assert main_checkout(main).resolve() == main.resolve()


def test_main_checkout_falls_back_outside_a_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert main_checkout(tmp_path) == tmp_path
    monkeypatch.setenv("PATH", "")
    assert main_checkout(tmp_path) == tmp_path


def test_check_tokens_stops_a_zha_run_without_an_admin_token():
    registry = AdapterRegistry.discover()
    step_file = next(f for f in load_step_files() if f.action == "turn_on_for")
    zha, shelly = rig.Suite(step_file, "zha"), rig.Suite(step_file, "shelly")
    env = {"HA_URL": "http://ha:8123", "HA_TOKEN": "tok"}
    with pytest.raises(SystemExit, match="HA_URL and HA_TOKEN"):
        rig.check_tokens({}, [shelly], registry)
    with pytest.raises(SystemExit, match="zha needs HA_ADMIN_TOKEN"):
        rig.check_tokens(env, [shelly, zha], registry)
    rig.check_tokens(env, [shelly], registry)
    rig.check_tokens({**env, "HA_ADMIN_TOKEN": "admin"}, [zha], registry)


def test_profile_reads_its_pause_list_and_is_named_for_its_file(tmp_path):
    path = write(tmp_path, "lab.yaml", "pause: [switch.al, automation.motion]\nshelly:\n")
    profile = Profile.load(path)
    assert (profile.name, profile.pause, profile.integrations) == (
        "lab",
        {"all": ("switch.al", "automation.motion")},
        {"shelly": []},
    )
    assert profile.paused_by({"shelly"}) == profile.paused_by() == ("switch.al", "automation.motion")


def test_a_pause_mapping_pauses_all_and_the_held_integrations_lists(tmp_path):
    path = write(
        tmp_path,
        "lab.yaml",
        "pause:\n  all: [automation.motion]\n  hue: [switch.al, automation.motion]\n  zha: [switch.zha]\n",
    )
    profile = Profile.load(path)
    assert profile.paused_by({"shelly"}) == ("automation.motion",)
    assert profile.paused_by({"hue"}) == ("automation.motion", "switch.al")
    assert profile.paused_by() == ("automation.motion", "switch.al", "switch.zha")


@pytest.mark.parametrize(
    ("pause", "message"),
    [
        ("[switch.al, light.hall]", "automations and switches only, got light.hall"),
        ("switch.al", "pause must be a list of entity IDs"),
        ("{hue: switch.al}", "pause must be a list of entity IDs"),
        ("{hues: [switch.al]}", "pause has unknown keys \\['hues'\\]"),
    ],
)
def test_profile_refuses_a_bad_pause_list(tmp_path, pause, message):
    with pytest.raises(ValueError, match=message):
        Profile.load(write(tmp_path, "lab.yaml", f"pause: {pause}\n"))


def profiles(tmp_path: Path, *names: str) -> Path:
    directory = tmp_path / "profiles"
    directory.mkdir()
    for name in names:
        write(directory, f"{name}.yaml", "shelly:\n")
    return directory


def test_choose_profile_takes_the_named_one_or_the_only_one(tmp_path):
    directory = profiles(tmp_path, "house", "lab")
    assert rig.choose_profile("lab", {}, directory) == directory / "lab.yaml"
    (directory / "lab.yaml").unlink()
    assert rig.choose_profile(None, {"RIG_PAUSE": ""}, directory) == directory / "house.yaml"


@pytest.mark.parametrize(
    ("names", "name", "env", "message"),
    [
        (("house", "lab"), None, {}, "Choose a profile with --profile: house, lab"),
        (("house",), "lab", {}, "No profile 'lab' in .*; there are: house"),
        ((), None, {}, "No profile in .*; copy .*profile.example.yaml"),
        (("house",), None, {"RIG_PAUSE": "switch.al"}, "RIG_PAUSE has moved from .env.rig into the profile"),
    ],
)
def test_choose_profile_refuses(tmp_path, names, name, env, message):
    with pytest.raises(SystemExit, match=message):
        rig.choose_profile(name, env, profiles(tmp_path, *names))


def test_check_tokens_stops_a_hue_bridge_step_without_the_bridge_settings(tmp_path):
    registry = AdapterRegistry.discover()
    path = tmp_path / "steps.yaml"
    path.write_text(
        "action: set_scene\nrig_seconds: 5\ntests:\n  - name: t\n    roles: [light_onoff]\n"
        "    steps:\n      - bridge: {put: {on: {on: true}}}\n"
    )
    step_file = load_step_file(path)
    env = {"HA_URL": "http://ha:8123", "HA_TOKEN": "tok", "HA_ADMIN_TOKEN": "admin"}
    with pytest.raises(SystemExit, match="HUE_BRIDGE_HOST and HUE_APP_KEY"):
        rig.check_tokens(env, [rig.Suite(step_file, "hue")], registry)
    with pytest.raises(SystemExit, match="HUE_BRIDGE_HOST and HUE_APP_KEY"):
        rig.check_tokens({**env, "HUE_BRIDGE_HOST": "bridge"}, [rig.Suite(step_file, "hue")], registry)
    ready = {**env, "HUE_BRIDGE_HOST": "bridge", "HUE_APP_KEY": "key"}
    rig.check_tokens(ready, [rig.Suite(step_file, "hue")], registry)
    rig.check_tokens(env, [rig.Suite(step_file, "shelly")], registry)


def test_check_tokens_stops_a_run_of_an_adapter_with_options_without_an_admin_token():
    # Its options are pinned through DeviceSide's options flow, which is admin only.
    registry = AdapterRegistry.discover()
    step_file = next(f for f in load_step_files() if f.action == "raise_brightness")
    env = {"HA_URL": "http://ha:8123", "HA_TOKEN": "tok", "HUE_BRIDGE_HOST": "bridge", "HUE_APP_KEY": "key"}
    with pytest.raises(SystemExit, match="hue needs HA_ADMIN_TOKEN"):
        rig.check_tokens(env, [rig.Suite(step_file, "hue")], registry)
    rig.check_tokens({**env, "HA_ADMIN_TOKEN": "admin"}, [rig.Suite(step_file, "hue")], registry)


class RigHA(fakes.FakeAdmin):
    async def close(self):
        pass


def rig_ha(monkeypatch, light: str, opt: bool, automation: str) -> RigHA:
    """One fake for both connections, with a light, DeviceSide's options and an automation to pause."""
    monkeypatch.setattr(engine, "OPTIONS_RELOAD_S", 0)
    ha = RigHA(
        {"light.a": {"state": light, "attributes": {"brightness": 200}}, "automation.motion": {"state": automation}},
        options={"opt": opt},
    )
    ha.respond = lambda ha, action, data: {data["entity_id"]: {"status": "sent", "integration": "shelly"}}

    async def connect(env):
        return ha, ha

    monkeypatch.setattr(rig, "connect", connect)
    return ha


def put_back(ha: RigHA) -> bool:
    return (
        ha.states["light.a"] == {"entity_id": "light.a", "state": "on", "attributes": {"brightness": 200}}
        and ha.states["automation.motion"]["state"] == "on"
        and ha.options == {"opt": True, "other": False}
    )


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
async def test_a_signal_mid_case_puts_everything_back_and_leaves_no_journal(tmp_path, monkeypatch, capsys, signum):
    ha = rig_ha(monkeypatch, light="on", opt=True, automation="on")
    journal = Journal(tmp_path / LEGACY_NAME)
    steps = write(tmp_path, "steps.yaml", fakes.WAITING_MIDWAY)
    run_rig = rig.run_rig(
        {},
        [rig.Suite(load_step_file(steps), "shelly")],
        fakes.profile("shelly", ("automation.motion",), light_ct="light.a"),
        fakes.OPTION_REGISTRY,
        None,
        True,
        "c1",
        journal,
    )
    task = asyncio.create_task(run_rig)
    while ha.states["light.a"]["state"] != "off":
        await asyncio.sleep(0.005)
    assert set(journal.sections) == {"profile", "options", "cases"}
    assert json.loads((tmp_path / baseline.NAME).read_text()) == {
        "automation:automation.motion": {"original": "on", "holders": [f"{journal.name}#pause"]}
    }
    assert set(journal.get("cases")) == {"serial"}

    os.kill(os.getpid(), signum)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "Aborting: putting devices back" in capsys.readouterr().out
    assert put_back(ha)
    assert not journal.path.exists()
    assert not (tmp_path / baseline.NAME).exists()


@pytest.mark.parametrize(("commit", "sources"), [("c1", ["rig run (`scripts/rig.py`), commit `c1short`"]), (None, [])])
async def test_records_cite_the_commit_ha_loaded_not_head_at_the_end(tmp_path, monkeypatch, capsys, commit, sources):
    rig_ha(monkeypatch, light="on", opt=True, automation="on")
    head = {"HEAD": "c1short"}
    monkeypatch.setattr(rig, "git", lambda *args, check=True: head.get(args[-1], f"{args[-1]}short"))

    async def run(*args, **kwargs):
        head["HEAD"] = "c2short"  # the session commits mid-run
        return engine.RunResult([])

    written = []

    async def write_records(result, registry, records_dir, date, source, profile):
        written.append(source)
        assert profile == "house"
        return []

    monkeypatch.setattr(rig, "run", run)
    monkeypatch.setattr(rig, "write_report", lambda result, directory, title: tmp_path / "report.md")
    monkeypatch.setattr(rig, "write_records", write_records)

    suites = target_suite(tmp_path)
    assert await rig.run_rig({}, suites, fakes.profile("shelly"), fakes.OPTION_REGISTRY, None, True, commit) == 0
    assert written == sources
    assert ("No test records" in capsys.readouterr().out) is (commit is None)


LIGHT_A_ON = {"entity_id": "light.a", "state": "on", "attributes": {"brightness": 200}}


def aborted_journal(path: Path) -> Journal:
    """What a run killed mid-case leaves: the light it turned off, the option it saved and the automation it paused."""
    journal = Journal(path)
    journal.update(
        profile="lab",
        paused=["automation.motion"],
        options={"opt": True, "other": False},
        cases={"serial": {"taken": {"light.a": LIGHT_A_ON}, "stored": {}, "power": {}}},
    )
    return journal


async def test_recover_puts_back_every_lanes_case(tmp_path, monkeypatch):
    ha = rig_ha(monkeypatch, light="off", opt=True, automation="on")
    ha.set("light.b", "off")
    journal = Journal(tmp_path / LEGACY_NAME)
    light_b_on = {"entity_id": "light.b", "state": "on", "attributes": {"brightness": 90}}
    journal.update(
        cases={
            "hue": {"taken": {"light.a": LIGHT_A_ON}, "stored": {}, "power": {}},
            "zha": {"taken": {"light.b": light_b_on}, "stored": {}, "power": {}},
        }
    )

    await rig.recover(ha, ha, journal, out=lambda _: None)

    assert ha.states["light.a"] == LIGHT_A_ON
    assert ha.states["light.b"] == light_b_on
    assert not journal.path.exists()


async def test_recover_puts_back_a_cases_auto_config_and_waits_out_its_reload(monkeypatch):
    monkeypatch.setitem(engine.ENTRY_RELOADS, "shelly", replace(engine.ENTRY_RELOADS["shelly"], deadline_s=0.5))
    shelly = fakes.ShellyAuto({"switch.plug": {"auto_off": {"duration": 6}, "auto_on": None}})
    ha = fakes.FakeHA({"switch.plug": {"state": "on"}}, shelly)
    entry = {
        "taken": {"switch.plug": {"entity_id": "switch.plug", "state": "off", "attributes": {}}},
        "stored": {},
        "power": {},
        "auto": {"switch.plug": {"auto_off": None, "auto_on": {"duration": 30}}},
        "integration": "shelly",
    }
    printed = []

    await rig.recover_case(ha, entry, printed.append).restore()

    assert shelly.config["switch.plug"] == {"auto_off": None, "auto_on": {"duration": 30}}
    assert [call[:2] for call in ha.calls] == [
        ("deviceside", "clear_auto_off"),
        ("deviceside", "set_auto_on"),
        ("reload", "switch.plug"),  # one reload for both writes, waited out before the state goes back
        ("switch", "turn_off"),
    ]
    assert "Putting switch.plug's auto_off off, auto_on 30 s back, waiting out the entry reload a change makes." in (
        printed
    )


async def test_recover_puts_back_a_case_a_journal_from_before_lanes_left(tmp_path, monkeypatch):
    ha = rig_ha(monkeypatch, light="off", opt=True, automation="on")
    journal = Journal(tmp_path / LEGACY_NAME)
    journal.update(taken={"light.a": LIGHT_A_ON}, stored={}, power={})
    printed = []

    await rig.recover(ha, ha, journal, out=printed.append)

    assert ha.states["light.a"] == LIGHT_A_ON
    assert "Putting light.a back on." in printed
    assert not journal.path.exists()


async def test_recover_resumes_what_the_orphan_holds_paused_in_the_baseline(tmp_path, monkeypatch):
    ha = rig_ha(monkeypatch, light="on", opt=True, automation="on")
    orphan = Journal.start(tmp_path)
    orphan.update(profile="lab")
    await baseline.Baseline.of(ha, orphan, "pause", lambda _: None).pause(["automation.motion"])
    assert ha.states["automation.motion"]["state"] == "off"

    await rig.recover(ha, ha, orphan, out=lambda _: None)  # it holds the orphan's lock meanwhile

    assert ha.states["automation.motion"]["state"] == "on"
    assert not (tmp_path / baseline.NAME).exists()
    assert not orphan.path.exists()


async def test_recover_puts_back_what_an_aborted_run_left(tmp_path, monkeypatch):
    ha = rig_ha(monkeypatch, light="off", opt=False, automation="off")
    printed = []

    await rig.recover(ha, ha, aborted_journal(tmp_path / LEGACY_NAME), out=printed.append)

    assert put_back(ha)
    assert [call[:2] for call in ha.calls] == [("light", "turn_on"), ("automation", "turn_on")]
    assert "Putting light.a back on." in printed
    assert printed[0].startswith("An aborted rig run (profile lab) left changes")
    assert not (tmp_path / LEGACY_NAME).exists()


async def test_recover_without_an_admin_token_leaves_the_options_in_the_journal(tmp_path, monkeypatch):
    ha = rig_ha(monkeypatch, light="off", opt=False, automation="off")
    journal = aborted_journal(tmp_path / LEGACY_NAME)

    await rig.recover(ha, None, journal, out=lambda _: None)

    assert ha.options == {"opt": False, "other": False}
    assert Journal(journal.path).sections == {"profile": "lab", "options": {"opt": True, "other": False}}


async def test_recover_enables_a_disabled_entry_before_it_puts_the_cases_back(tmp_path, monkeypatch):
    # The case's restore may write stored attributes back through DeviceSide, which a disabled entry refuses.
    ha = rig_ha(monkeypatch, light="off", opt=True, automation="on")
    ha.disabled = True
    journal = Journal(tmp_path / LEGACY_NAME)
    journal.update(entry="e1", cases={"serial": {"taken": {"light.a": LIGHT_A_ON}, "stored": {}, "power": {}}})
    printed = []

    await rig.recover(ha, ha, journal, out=printed.append)

    assert ha.moves == ["enable"]
    assert printed[1] == "Enabled DeviceSide's entry again."
    assert ha.states["light.a"] == LIGHT_A_ON
    assert not journal.path.exists()


async def test_recover_says_when_the_entry_will_not_enable(tmp_path, monkeypatch):
    ha = rig_ha(monkeypatch, light="on", opt=True, automation="on")
    ha.disabled = ha.stuck = True
    journal = Journal(tmp_path / LEGACY_NAME)
    journal.update(entry="e1")
    printed = []

    await rig.recover(ha, ha, journal, out=printed.append)

    assert printed[1] == "Could not enable DeviceSide's entry again; enable it in HA: setup failed"
    assert not journal.path.exists()


async def test_recover_without_an_admin_token_leaves_the_entry_disabled_in_the_journal(tmp_path, monkeypatch):
    ha = rig_ha(monkeypatch, light="on", opt=True, automation="on")
    journal = Journal(tmp_path / LEGACY_NAME)
    journal.update(entry="e1")

    await rig.recover(ha, None, journal, out=lambda _: None)

    assert Journal(journal.path).sections == {"entry": "e1"}


def test_check_tokens_stops_a_run_with_entry_steps_without_an_admin_token():
    registry = AdapterRegistry.discover()
    step_file = next(f for f in load_step_files() if f.name == "lifecycle")
    env = {"HA_URL": "http://ha:8123", "HA_TOKEN": "tok"}
    with pytest.raises(SystemExit, match="shelly needs HA_ADMIN_TOKEN"):
        rig.check_tokens(env, [rig.Suite(step_file, "shelly")], registry)
    rig.check_tokens({**env, "HA_ADMIN_TOKEN": "admin"}, [rig.Suite(step_file, "shelly")], registry)


def test_action_picks_a_named_file_by_its_name_not_its_action():
    step_files = load_step_files()
    profile = fakes.profile("shelly", switch_plug="switch.plug")
    assert {suite.name for suite in rig.select(step_files, profile, None, ["lifecycle"])} == {"lifecycle"}
    assert {suite.name for suite in rig.select(step_files, profile, None, ["get_attributes"])} == {"get_attributes"}


def test_main_recovers_before_the_plan_and_recover_alone_stops_there(tmp_path, monkeypatch, capsys):
    ha = rig_ha(monkeypatch, light="off", opt=False, automation="off")
    aborted_journal(tmp_path / LEGACY_NAME)
    monkeypatch.setattr(rig, "git", lambda *args, check=True: str(tmp_path) if "--git-common-dir" in args else "wt/x")
    monkeypatch.setattr(rig, "load_env", lambda: {"HA_URL": "http://ha:8123", "HA_TOKEN": "tok"})

    assert rig.main(["--recover"]) == 1  # the journal from before per-run journals is restricted
    assert (
        "(profile lab, restricted): sensor off or DEVICE_SIDE_RIG_RESTRICTED=1 restores it" in capsys.readouterr().out
    )
    monkeypatch.setenv("DEVICE_SIDE_RIG_RESTRICTED", "1")
    assert rig.main(["--dry-run", "--recover"]) == 1
    assert "--recover puts them back." in capsys.readouterr().out
    assert rig.main(["--recover"]) == 0
    assert put_back(ha)
    assert rig.main(["--recover"]) == 0
    assert "No aborted run left anything to put back." in capsys.readouterr().out


@pytest.mark.parametrize(
    ("error", "line"),
    [
        (asyncio.CancelledError, "Aborted."),
        (KeyboardInterrupt, "Aborted before putting everything back; the next run puts back the rest."),
    ],
)
def test_a_stopped_run_ends_on_one_line_not_a_traceback(capsys, error, line):
    async def stopped():
        raise error

    assert rig.run_tests(stopped()) == 1
    assert capsys.readouterr().out == f"{line}\n"


DEPLOY_ENV = {"RIG_SYNC_CMD": "sync", "RIG_RESTART_CMD": "restart", "HA_URL": "http://ha/", "HA_TOKEN": "tok"}


def fake_deploy(monkeypatch, signals: int = 0, returncode: int = 0) -> list[str]:
    """deploy.sh, receiving `signals` SIGTERMs as it runs, and wait_until_ready; returns what ran, in order."""
    ran = []

    def run(command, check, env, start_new_session):
        assert start_new_session  # a terminal Ctrl-C must not reach it
        for _ in range(signals):
            os.kill(os.getpid(), signal.SIGTERM)
        ran.append("deploy")
        if returncode:
            raise rig.subprocess.CalledProcessError(returncode, command)

    monkeypatch.setattr(rig.subprocess, "run", run)
    monkeypatch.setattr(rig, "wait_until_ready", lambda url, token, admin_token, timeout: ran.append(f"wait {timeout}"))
    return ran


def test_a_signal_mid_deploy_stops_the_run_once_ha_is_back(monkeypatch, capsys):
    ran = fake_deploy(monkeypatch, signals=1)
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(rig.Aborted, match="after the deploy"):
        rig.deploy_and_wait(DEPLOY_ENV, "b")
    assert ran == ["deploy", f"wait {rig.READY_TIMEOUT_S}"]
    assert "Stopping once the deploy and HA's restart finish" in capsys.readouterr().out
    assert signal.getsignal(signal.SIGTERM) is before


def test_a_second_signal_mid_deploy_stops_at_once(monkeypatch):
    ran = fake_deploy(monkeypatch, signals=2)
    with pytest.raises(rig.Aborted, match="mid-deploy"):
        rig.deploy_and_wait(DEPLOY_ENV, "b")
    assert ran == []


def test_a_failed_deploy_ends_on_one_line(monkeypatch):
    fake_deploy(monkeypatch, returncode=3)
    with pytest.raises(SystemExit, match=r"^Deploy failed \(exit 3\); HA may be mid-restart"):
        rig.deploy_and_wait(DEPLOY_ENV, "b")


def test_without_a_deploy_it_waits_briefly_and_a_signal_stops_at_once(monkeypatch):
    ran = fake_deploy(monkeypatch)
    rig.deploy_and_wait(DEPLOY_ENV, None)
    assert ran == ["wait 15"]

    def wait(url, token, admin_token, timeout):
        os.kill(os.getpid(), signal.SIGTERM)
        pytest.fail("not stopped")

    monkeypatch.setattr(rig, "wait_until_ready", wait)
    with pytest.raises(rig.Aborted, match="^Aborted.$"), rig.end_on_signals():
        rig.deploy_and_wait(DEPLOY_ENV, None)


# --- --deploy-only ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        ["--profile", "house"],
        ["--no-deploy"],
        ["--check"],
        ["--roam-check"],
        ["--recover"],
        ["--wait-free"],
        ["--integration", "hue"],
        ["--action", "turn_on_for"],
        ["-k", "x"],
        ["--serial"],
        ["--unattended"],
        ["--debug-logs"],
        ["--hold", "hue", "--", "true"],
    ],
)
def test_deploy_only_takes_only_dry_run(extra, capsys):
    with pytest.raises(SystemExit):
        rig.main(["--deploy-only", *extra])
    assert "--deploy-only takes only --dry-run" in capsys.readouterr().err


def deploy_only(monkeypatch, tmp_path, *states: bool) -> list:
    """main's --deploy-only with the deploy state needed or not at each read, in turn; returns what happened."""
    happened: list = []
    reads = iter(states)

    def state(status_cmd):
        return (
            rig.Deploy(commit="c1", local_tree="tree1", synced_tree="old", loaded_tree="old", branch="wt/b")
            if next(reads)
            else rig.Deploy(commit="c1", local_tree="tree1", synced_tree="tree1", loaded_tree="tree1", branch="wt/b")
        )

    class Held:
        def close(self):
            happened.append("released")

    def hold(claim, holder, directory):
        happened.append(("hold", claim, holder))
        return Held()

    def no_profile(path):
        raise AssertionError("--deploy-only loads no profile")

    monkeypatch.setattr(rig, "load_env", lambda: DEPLOY_ENV)
    monkeypatch.setattr(rig, "deploy_state", state)
    monkeypatch.setattr(rig, "common_dir", lambda: tmp_path)
    monkeypatch.setattr(rig, "git", lambda *args, check=True: "c1short")
    monkeypatch.setattr(rig.locks, "hold", hold)
    monkeypatch.setattr(rig, "deploy_and_wait", lambda env, branch: happened.append(("deploy", branch)))
    monkeypatch.setattr(rig.Profile, "load", no_profile)
    return happened


def test_deploy_only_does_nothing_when_ha_runs_the_tree(monkeypatch, tmp_path, capsys):
    happened = deploy_only(monkeypatch, tmp_path, False)
    assert rig.main(["--deploy-only"]) == 0
    assert happened == []
    assert "nothing to deploy" in capsys.readouterr().out


def test_deploy_only_holds_ha_exclusively_deploys_and_releases(monkeypatch, tmp_path, capsys):
    happened = deploy_only(monkeypatch, tmp_path, True, True)
    assert rig.main(["--deploy-only"]) == 0
    assert happened == [("hold", rig.Claim("exclusive", frozenset()), "wt/b"), ("deploy", "wt/b"), "released"]
    out = capsys.readouterr().out
    assert "Deploy: sync wt/b, then restart Home Assistant" in out
    assert "Deployed wt/b (c1short); HA is back. Locks released." in out


def test_deploy_only_skips_a_deploy_another_run_made_while_it_waited(monkeypatch, tmp_path, capsys):
    happened = deploy_only(monkeypatch, tmp_path, True, False)
    assert rig.main(["--deploy-only"]) == 0
    assert happened == [("hold", rig.Claim("exclusive", frozenset()), "wt/b"), "released"]
    assert "Another run deployed this checkout's tree meanwhile" in capsys.readouterr().out


def test_deploy_only_dry_run_prints_the_state_and_takes_no_lock(monkeypatch, tmp_path, capsys):
    happened = deploy_only(monkeypatch, tmp_path, True)
    assert rig.main(["--deploy-only", "--dry-run"]) == 0
    assert happened == []
    assert "Integration: local tree1, synced old, loaded old" in capsys.readouterr().out


def test_deploy_only_stops_on_a_deploy_blocker(monkeypatch, tmp_path):
    happened = deploy_only(monkeypatch, tmp_path, True)
    blocked = rig.Deploy("c1", "tree1", "old", "old", "wt/b", blocker="wt/b is not on origin; push it first")
    monkeypatch.setattr(rig, "deploy_state", lambda status_cmd: blocked)
    with pytest.raises(SystemExit, match="Cannot deploy: wt/b is not on origin"):
        rig.main(["--deploy-only"])
    assert happened == []


def test_a_signal_before_the_tests_ends_the_run_on_one_line():
    before = signal.getsignal(signal.SIGHUP)
    with pytest.raises(rig.Aborted, match="^Aborted.$"), rig.end_on_signals():
        os.kill(os.getpid(), signal.SIGHUP)
    assert signal.getsignal(signal.SIGHUP) is before


def target_suite(tmp_path: Path) -> list[rig.Suite]:
    return [rig.Suite(load_step_file(write(tmp_path, "steps.yaml", fakes.WAITING_MIDWAY)), "shelly")]


async def test_a_target_coming_back_within_the_grace_lets_the_run_start(tmp_path, capsys):
    ha = fakes.FakeHA({"light.a": {"state": "unavailable"}})
    ha.later(0.02, "light.a", "off")
    profile = fakes.profile("shelly", light_ct="light.a")

    await rig.wait_for_targets(ha, target_suite(tmp_path), profile, fakes.OPTION_REGISTRY, None, timeout=5)

    assert "Waiting up to 5 s for light.a" in capsys.readouterr().out
    assert ha.calls == []


async def test_a_target_still_unavailable_stops_the_run_before_it_changes_anything(tmp_path):
    ha = fakes.FakeHA({"light.a": {"state": "unavailable"}})
    profile = fakes.profile("shelly", light_ct="light.a")

    with pytest.raises(SystemExit, match=r"Still unavailable after 0 s: light.a\. Check it is powered"):
        await rig.wait_for_targets(ha, target_suite(tmp_path), profile, fakes.OPTION_REGISTRY, None, timeout=0.05)
    assert ha.calls == []


async def test_targets_already_available_need_no_wait(tmp_path, capsys):
    ha = fakes.FakeHA({"light.a": {"state": "on"}})
    profile = fakes.profile("shelly", light_ct="light.a")

    await rig.wait_for_targets(ha, target_suite(tmp_path), profile, fakes.OPTION_REGISTRY, None, timeout=5)

    assert capsys.readouterr().out == ""


async def test_a_target_missing_from_ha_stops_the_run_at_once(tmp_path):
    ha = fakes.FakeHA({})
    profile = fakes.profile("shelly", light_ct="light.a")

    with pytest.raises(SystemExit, match=r"Not in HA: light.a\. Check profile house"):
        await rig.wait_for_targets(ha, target_suite(tmp_path), profile, fakes.OPTION_REGISTRY, None, timeout=5)


async def test_an_unavailable_group_member_stops_the_run(tmp_path):
    steps = write(tmp_path, "steps.yaml", fakes.WAITING_MIDWAY.replace("[light_ct]", "[group]"))
    ha = fakes.FakeHA({"light.g": {"state": "on"}, "light.a": {"state": "on"}, "light.b": {"state": "unavailable"}})
    profile = Profile({"shelly": [RigEntity("light.g", frozenset({"group"}), members=("light.a", "light.b"))]})

    with pytest.raises(SystemExit, match=r"Still unavailable after 0 s: light.b\."):
        await rig.wait_for_targets(
            ha, [rig.Suite(load_step_file(steps), "shelly")], profile, fakes.OPTION_REGISTRY, None, timeout=0.05
        )


def test_profile_paths_are_every_profile_by_name(tmp_path):
    directory = profiles(tmp_path, "lab", "house")
    assert profile_paths(directory) == [directory / "house.yaml", directory / "lab.yaml"]


def test_the_example_is_a_valid_profile():
    assert Profile.load(EXAMPLE).pause


def test_load_env_still_reads_rig_pause_so_a_run_can_refuse_it(tmp_path, monkeypatch):
    monkeypatch.setenv("RIG_PAUSE", "switch.al")
    assert load_env(tmp_path / "missing.env.rig")["RIG_PAUSE"] == "switch.al"


# --- the deviceside checkout under test ----------------------------------------------------------------------------


def test_every_mode_checks_for_moved_files_then_writes_devicesides_household_terms(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(rig.target, "refuse_moved_files", lambda repo, rig_checkout: calls.append(("moved", repo)))
    monkeypatch.setattr(rig.terms, "write", lambda repo: calls.append(("terms", repo)))
    monkeypatch.setattr(rig, "common_dir", lambda: tmp_path)
    monkeypatch.setattr(rig.locks, "wait_free", lambda directory, integrations: None)
    assert rig.main(["--wait-free"]) == 0
    assert calls == [("moved", rig.REPO), ("terms", rig.REPO)]


def test_a_bad_profile_leaves_the_terms_stale_but_stops_no_mode(tmp_path, monkeypatch, capsys):
    def unreadable(repo):
        raise ValueError("house.yaml: not valid YAML")

    monkeypatch.setattr(rig.terms, "write", unreadable)
    monkeypatch.setattr(rig, "common_dir", lambda: tmp_path)
    monkeypatch.setattr(rig.locks, "wait_free", lambda directory, integrations: None)
    assert rig.main(["--wait-free"]) == 0
    assert "household terms not refreshed: house.yaml: not valid YAML" in capsys.readouterr().out


def test_the_runner_tests_a_deviceside_checkout():
    assert (rig.REPO / rig.COMPONENT / "manifest.json").is_file()
    assert rig.TEST_RECORDS_DIR == rig.REPO / "docs" / "test-records"


# --- stopping and resuming -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds"), [("30m", 1800), ("90s", 90), ("1h15m", 4500), ("1h", 3600), ("2m5s", 125)]
)
def test_for_reads_hours_minutes_and_seconds(text, seconds):
    assert rig.duration(text) == seconds


@pytest.mark.parametrize("text", ["", "15", "m", "0m", "5m1h", "1.5h", "-5m"])
def test_for_refuses_anything_else(text):
    with pytest.raises(rig.argparse.ArgumentTypeError):
        rig.duration(text)


@pytest.mark.parametrize("extra", [["--check"], ["--roam-check"], ["--recover"], ["--wait-free"], ["--deploy-only"]])
@pytest.mark.parametrize("flag", [["--for", "5m"], ["--fresh"]])
def test_for_and_fresh_are_for_a_test_run(extra, flag, capsys):
    with pytest.raises(SystemExit):
        rig.main([*flag, *extra])
    err = capsys.readouterr().err
    assert "--for and --fresh are for a test run" in err or "--deploy-only takes only --dry-run" in err


TWO_SUITES = """
action: raise_brightness
rig_seconds: 60
tests:
  - name: t
    roles: [light_ct]
    steps:
      - set: {state: off}
  - name: u
    roles: [light_ct]
    steps:
      - set: {state: off}
"""


def resumable(tmp_path: Path) -> tuple[Profile, list[rig.Suite]]:
    profile = Profile(
        {
            "shelly": [RigEntity("light.a", frozenset({"light_ct"}))],
            "zha": [RigEntity("light.b", frozenset({"light_ct"}))],
        },
        {},
        name="house",
    )
    step_file = load_step_file(write(tmp_path, "steps.yaml", TWO_SUITES))
    return profile, [rig.Suite(step_file, "shelly"), rig.Suite(step_file, "zha")]


def run_args(**kwargs) -> types.SimpleNamespace:
    defaults = {"integration": None, "action": None, "keyword": None, "serial": False, "unattended": True}
    return types.SimpleNamespace(**(defaults | {"fresh": False, "no_deploy": False, "roam_check": False} | kwargs))


def resumed(tmp_path, monkeypatch, done_ids: dict[str, list[str]], **kwargs) -> rig.Setup:
    """resume() after a stopped run that finished `done_ids` by integration, on HA 2026.10.1."""
    profile, suites = resumable(tmp_path)
    deploy = rig.Deploy("c1", "tree1", "tree1", "tree1", "wt/x")
    key = rig.Key("house", "tree1", (), (), None, False, True)
    saved = rig.Progress.load(tmp_path, "house", "wt/x", key)
    for suite in suites:
        for case_id in done_ids.get(suite.integration, []):
            saved.record(suite, engine.CaseResult(case_id, "light_ct", "light", engine.Outcome.PASS))
        saved.facts(suite, "2026.10.1", [])
    monkeypatch.setattr(rig, "ha_version", lambda env: "2026.10.1")
    setup = rig.Setup({}, profile, fakes.OPTION_REGISTRY, suites, False, False, deploy, selected=suites)
    rig.resume(run_args(**kwargs), setup, tmp_path)
    return setup


def test_a_resume_plans_holds_and_estimates_only_what_is_left(tmp_path, monkeypatch, capsys):
    setup = resumed(tmp_path, monkeypatch, {"shelly": ["t[light_ct]", "u[light_ct]"], "zha": ["t[light_ct]"]})
    assert [suite.integration for suite in setup.suites] == ["zha"]
    assert rig.claim(types.SimpleNamespace(no_deploy=False, roam_check=False), setup) == rig.Claim(
        "shared", frozenset({"zha"})
    )
    assert rig.estimate(setup.suites, fakes.OPTION_REGISTRY, False, done=setup.done, resumed=setup.profile) == 30
    rig.print_resume(types.SimpleNamespace(budget=None, keyword=None), setup)
    assert "Resuming: 3 of 4 cases done" in capsys.readouterr().out


def test_a_resume_with_nothing_left_starts_fresh(tmp_path, monkeypatch, capsys):
    every = ["t[light_ct]", "u[light_ct]"]
    setup = resumed(tmp_path, monkeypatch, {"shelly": every, "zha": every})
    assert setup.suites == setup.selected and not setup.done
    assert "every case finished; starting fresh" in capsys.readouterr().out


def test_another_selection_starts_fresh(tmp_path, monkeypatch, capsys):
    setup = resumed(tmp_path, monkeypatch, {"shelly": ["t[light_ct]"]}, serial=True)
    assert not setup.done and "selection changed" in capsys.readouterr().out


def test_a_run_stopped_by_its_budget_resumes_and_writes_its_record_once(tmp_path, monkeypatch, capsys):
    ha = rig_ha(monkeypatch, light="on", opt=True, automation="on")
    ha.states["light.b"] = {"entity_id": "light.b", "state": "on", "attributes": {"brightness": 200}}
    monkeypatch.setattr(rig, "git", lambda *args, check=True: "c1short")
    written: list[list[str]] = []

    async def write_records(result, registry, records_dir, date, source, profile):
        written.append([part.suite.integration for part in result.suites if part.recordable])
        return []

    monkeypatch.setattr(rig, "write_records", write_records)
    profile, suites = resumable(tmp_path)
    key = rig.Key("house", "tree1", (), (), None, False, True)

    def session(budget=None) -> int:
        progress = rig.Progress.load(tmp_path, "house", "wt/x", key)
        done = progress.done()
        left = [suite for suite in suites if rig.left(suite, profile, fakes.OPTION_REGISTRY, done, None)] or suites
        return asyncio.run(
            rig.run_rig(
                {},
                left,
                profile,
                fakes.OPTION_REGISTRY,
                None,
                True,
                "c1",
                progress=progress,
                selected=suites,
                budget=budget,
            )
        )

    record = rig.Progress.record

    def spent_after(count: int) -> engine.Budget:
        """A budget spent once `count` cases have finished."""
        budget = engine.Budget(float("inf"))

        def counting(self, suite, result):
            record(self, suite, result)
            if len(self.cases) >= count:
                budget.deadline = 0.0

        monkeypatch.setattr(rig.Progress, "record", counting)
        return budget

    assert session(spent_after(3)) == rig.RESUMABLE
    assert "Stopped with 1 cases left (budget spent); rerun the same command to resume." in capsys.readouterr().out
    assert written == [["shelly"]]  # zha is one case short
    assert path_for(tmp_path, "house", "wt/x").exists()

    monkeypatch.setattr(rig.Progress, "record", record)
    assert session() == 0
    assert "Resuming" not in capsys.readouterr().out
    assert written == [["shelly"], ["zha"]]  # shelly's record isn't written again
    assert not path_for(tmp_path, "house", "wt/x").exists()


def test_a_signal_stops_a_run_with_progress_resumable(capsys):
    async def cancelled():
        raise asyncio.CancelledError

    assert rig.run_tests(cancelled(), aborted=lambda: rig.RESUMABLE) == rig.RESUMABLE
    assert capsys.readouterr().out == "Aborted.\n"
