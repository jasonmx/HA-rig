"""ha_rig/terms.py and scripts/check_private.py: the household's terms, for device-side's check and this repo's."""

import importlib.util
import subprocess
from pathlib import Path

from ha_rig import terms

RIG_HOME = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("check_private", RIG_HOME / "scripts" / "check_private.py")
check_private = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_private)
REAL_WRITE = terms.write  # conftest swaps the module's for a no-op


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


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


def test_generic_host_is_not_a_term(tmp_path):
    assert found(tmp_path, env="HA_URL=http://homeassistant.local:8123\n") == {}


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


def test_write_puts_the_terms_in_the_main_checkout_for_device_sides_check(tmp_path):
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
    monkeypatch.setattr(terms, "device_side_repo", lambda: repo)
    monkeypatch.setattr(terms, "write", lambda target: target / terms.TERMS_FILE)
    assert terms.main() == 0
    assert str(repo / terms.TERMS_FILE) in capsys.readouterr().out


def test_main_fails_when_a_profile_cannot_be_read(tmp_path, monkeypatch, capsys):
    def unreadable(target):
        raise ValueError("house.yaml: not valid YAML")

    monkeypatch.setattr(terms, "device_side_repo", lambda: tmp_path)
    monkeypatch.setattr(terms, "write", unreadable)
    assert terms.main() == 1
    assert "not valid YAML" in capsys.readouterr().out


def test_check_adds_private_terms(tmp_path, monkeypatch):
    monkeypatch.setattr(check_private, "private_terms", lambda: {"light.a": "light.a"})
    extra = write(tmp_path, ".private-terms", "# names\nAcme Street\n\n")
    assert check_private.all_terms(extra) == {"light.a": "light.a", "acme street": "Acme Street"}
    assert check_private.all_terms(tmp_path / "missing") == {"light.a": "light.a"}


def test_check_reports_matches_case_insensitively_by_label(tmp_path):
    staged = write(tmp_path, "doc.md", "fine\nToken ABC.DEF here\n")
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\xff\xfe")
    hits = check_private.find([staged, binary], {"abc.def": "HA_TOKEN from .env.rig"})
    assert hits == [f"{staged}:2: HA_TOKEN from .env.rig"]


def test_check_passes_without_terms(tmp_path, monkeypatch):
    monkeypatch.setattr(check_private, "all_terms", dict)
    assert check_private.main([str(write(tmp_path, "doc.md", "anything"))]) == 0


def test_check_fails_when_a_profile_cannot_be_read(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(terms, "profile_paths", lambda: [write(tmp_path, "house.yaml", "pause: [a]1\n")])
    assert check_private.main([str(write(tmp_path, "msg", "anything"))]) == 1
    assert "house.yaml: not valid YAML" in capsys.readouterr().out


def test_check_fails_on_a_hit(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check_private, "all_terms", lambda: {"acme street": "Acme Street"})
    assert check_private.main([str(write(tmp_path, "msg", "Fix lamp on acme street"))]) == 1
    assert "Acme Street" in capsys.readouterr().out
