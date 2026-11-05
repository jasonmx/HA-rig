"""ha_rig/target.py: which device-side checkout the rig tests."""

import subprocess
from pathlib import Path

import pytest

from ha_rig import target


def checkout(path: Path) -> Path:
    (path / target.COMPONENT).mkdir(parents=True)
    (path / target.COMPONENT / "manifest.json").write_text("{}")
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_device_side_repo_prefers_the_env(tmp_path):
    repo = checkout(tmp_path / "anywhere")
    assert target.device_side_repo(tmp_path, {"DEVICE_SIDE_REPO": str(repo)}) == repo.resolve()


def test_device_side_repo_refuses_an_env_that_isnt_one(tmp_path):
    with pytest.raises(SystemExit, match="not a device-side checkout"):
        target.device_side_repo(tmp_path, {"DEVICE_SIDE_REPO": str(tmp_path)})


def test_device_side_repo_is_the_checkout_run_from(tmp_path):
    repo = checkout(tmp_path / "wt")
    (repo / "docs").mkdir()
    assert target.device_side_repo(repo / "docs", {}) == repo.resolve()


def test_device_side_repo_falls_back_to_a_sibling(tmp_path, monkeypatch):
    rig = tmp_path / "ha-rig"
    rig.mkdir()
    monkeypatch.setattr(target, "RIG_HOME", rig)
    sibling = checkout(tmp_path / "device-side")
    assert target.device_side_repo(tmp_path, {}) == sibling
    renamed = checkout(tmp_path / "deviceside")
    assert target.device_side_repo(tmp_path, {}) == renamed


def test_device_side_repo_says_how_to_name_one(tmp_path, monkeypatch):
    monkeypatch.setattr(target, "RIG_HOME", tmp_path / "ha-rig")
    with pytest.raises(SystemExit, match="DEVICE_SIDE_REPO"):
        target.device_side_repo(tmp_path, {})


def test_main_checkout_falls_back_outside_a_repo(tmp_path):
    assert target.main_checkout(tmp_path) == tmp_path


@pytest.mark.parametrize("stale", [".env.rig", "tests/rig/profiles"])
def test_a_household_file_left_in_device_side_is_refused(tmp_path, stale):
    from tests.conftest import REAL_REFUSE_MOVED_FILES

    repo = checkout(tmp_path / "deviceside")
    (repo / stale).mkdir(parents=True)
    with pytest.raises(SystemExit, match=f"Move {repo.resolve() / stale} into {tmp_path / 'ha-rig'}"):
        REAL_REFUSE_MOVED_FILES(repo, tmp_path / "ha-rig")
    (repo / stale).rmdir()
    REAL_REFUSE_MOVED_FILES(repo, tmp_path / "ha-rig")  # nothing left: passes
