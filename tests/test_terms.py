"""ha_rig/terms.py: the household's terms, for deviceside's check and this repo's."""

import subprocess
from pathlib import Path

import pytest

from ha_rig import terms
from tests.rig_fakes import write

REAL_WRITE = terms.write  # conftest swaps the module's for a no-op


def found(tmp_path: Path, profile: str = "", env: str = "") -> dict[str, str]:
    return terms.private_terms(
        [write(tmp_path, "house.yaml", profile) if profile else tmp_path / "missing.yaml"],
        write(tmp_path, ".env.rig", env) if env else tmp_path / "missing.env",
    )


def test_no_private_files_means_no_terms(tmp_path):
    assert found(tmp_path) == {}


def test_terms_come_from_the_profile_and_env(tmp_path):
    assert found(
        tmp_path,
        profile="shelly:\n  - entity_id: light.Kitchen_Relay\n    roles: [light_onoff]\n",
        env="HA_URL=http://ha.example.lan:8123\nHA_TOKEN=abc.def\n",
    ) == {
        "light.kitchen_relay": "light.Kitchen_Relay",
        "ha.example.lan": "ha.example.lan",
        "abc.def": "HA_TOKEN from .env.rig",
    }


def test_terms_come_from_every_entity_a_profile_names(tmp_path):
    house = write(tmp_path, "house.yaml", "pause: [automation.Hall_Motion]\n")
    lab = write(
        tmp_path,
        "lab.yaml",
        "zigbee2mqtt:\n  - entity_id: light.desk_group\n    roles: [group]\n    members: [light.desk_a]\n"
        "  - entity_id: light.desk_b\n    roles: [light_ct]\n    powered_by: switch.desk_plug\n"
        "  - entity_id: switch.desk_plug\n    roles: [switch_plug]\n",
    )
    assert terms.private_terms([house, lab], tmp_path / "missing.env") == {
        "automation.hall_motion": "automation.Hall_Motion",
        **{name: name for name in ("light.desk_group", "light.desk_a", "light.desk_b", "switch.desk_plug")},
    }


def test_terms_come_from_every_private_key_in_the_env(tmp_path):
    assert found(
        tmp_path,
        env="HA_ADMIN_TOKEN=Admin.Tok\nHUE_BRIDGE_HOST=Bridge.example.lan\nHUE_APP_KEY=HueKey123\n"
        "RIG_RESTRICTED_ENTITY=binary_sensor.Hall_Watch\nRIG_BLOCKER_ENTITY=binary_sensor.old_blocker\n",
    ) == {
        "admin.tok": "HA_ADMIN_TOKEN from .env.rig",
        "bridge.example.lan": "bridge.example.lan",
        "huekey123": "HUE_APP_KEY from .env.rig",
        "binary_sensor.hall_watch": "binary_sensor.Hall_Watch",
        "binary_sensor.old_blocker": "binary_sensor.old_blocker",
    }


@pytest.mark.parametrize(
    ("value", "host"),
    [
        ("bridge.example.lan:443", "bridge.example.lan"),
        ("https://bridge.example.lan", "bridge.example.lan"),
        ("192.0.2.7", "192.0.2.7"),
        ("2001:db8::7", "2001:db8::7"),
        ("[2001:db8::7]", "2001:db8::7"),
    ],
)
def test_a_bridge_host_is_its_host_however_written(tmp_path, value, host):
    assert found(tmp_path, env=f"HUE_BRIDGE_HOST={value}\n") == {host: host}


def test_generic_names_are_not_terms(tmp_path):
    assert found(tmp_path, env="HA_URL=http://homeassistant.local:8123\n") == {}
    assert found(tmp_path, env="HUE_BRIDGE_HOST=localhost\nHUE_APP_KEY=\n") == {}
    assert found(tmp_path, env="RIG_RESTRICTED_ENTITY=binary_sensor.rig_restricted\n") == {}


def test_terms_default_to_every_profile(tmp_path, monkeypatch):
    lab = write(tmp_path, "lab.yaml", "pause:\n  all: [switch.lab_al]\n  zha: [automation.lab_zha]\n")
    monkeypatch.setattr(terms, "profile_paths", lambda: [lab])
    assert terms.private_terms(env_file=tmp_path / "missing.env") == {
        "switch.lab_al": "switch.lab_al",
        "automation.lab_zha": "automation.lab_zha",
    }


def git_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_write_puts_the_terms_in_the_main_checkout_for_devicesides_check(tmp_path):
    repo = git_repo(tmp_path / "deviceside")
    path = REAL_WRITE(repo, {"light.kitchen_relay": "light.Kitchen_Relay", "abc.def": "HA_TOKEN from .env.rig"})
    assert path == repo / terms.TERMS_FILE
    assert (
        path.read_text() == terms.HEADER + "abc.def\tHA_TOKEN from .env.rig\nlight.kitchen_relay\tlight.Kitchen_Relay\n"
    )
    assert path.stat().st_mode & 0o777 == 0o600


def test_write_leaves_unchanged_terms_alone(tmp_path):
    repo = git_repo(tmp_path / "deviceside")
    path = REAL_WRITE(repo, {"a": "a"})
    path.chmod(0o640)  # a rewrite would set it back
    REAL_WRITE(repo, {"a": "a"})
    assert path.stat().st_mode & 0o777 == 0o640


def test_main_writes_into_the_target(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "deviceside"
    monkeypatch.setattr(terms, "deviceside_repo", lambda: repo)
    monkeypatch.setattr(terms, "write", lambda target: target / terms.TERMS_FILE)
    assert terms.main() == 0
    assert str(repo / terms.TERMS_FILE) in capsys.readouterr().out


def test_main_fails_when_a_profile_cannot_be_read(tmp_path, monkeypatch, capsys):
    def unreadable(target):
        raise ValueError("house.yaml: not valid YAML")

    monkeypatch.setattr(terms, "deviceside_repo", lambda: tmp_path)
    monkeypatch.setattr(terms, "write", unreadable)
    assert terms.main() == 1
    assert "not valid YAML" in capsys.readouterr().out


def test_main_here_writes_into_this_repo(monkeypatch, capsys):
    monkeypatch.setattr(terms, "deviceside_repo", lambda: pytest.fail("--here never needs deviceside"))
    monkeypatch.setattr(terms, "write", lambda target: target / terms.TERMS_FILE)
    assert terms.main(["--here"]) == 0
    assert str(terms.RIG_HOME / terms.TERMS_FILE) in capsys.readouterr().out


def test_main_here_fails_when_a_profile_cannot_be_read(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(terms, "RIG_HOME", git_repo(tmp_path / "rig"))
    monkeypatch.setattr(terms, "profile_paths", lambda: [write(tmp_path, "house.yaml", "pause: [a]1\n")])
    monkeypatch.setattr(terms, "write", REAL_WRITE)
    assert terms.main(["--here"]) == 1
    assert "house.yaml: not valid YAML" in capsys.readouterr().out
    assert not (tmp_path / "rig" / terms.TERMS_FILE).exists()


def test_write_makes_an_older_file_owner_only_on_a_change(tmp_path):
    repo = git_repo(tmp_path / "deviceside")
    path = repo / terms.TERMS_FILE
    path.write_text("old\n")
    path.chmod(0o644)
    REAL_WRITE(repo, {"a": "a"})
    assert path.stat().st_mode & 0o777 == 0o600
