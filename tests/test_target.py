"""ha_rig/target.py: which deviceside checkout the rig tests."""

import subprocess
from pathlib import Path

import pytest

from ha_rig import target


def checkout(path: Path) -> Path:
    (path / target.COMPONENT).mkdir(parents=True)
    (path / target.COMPONENT / "manifest.json").write_text("{}")
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_deviceside_repo_prefers_the_env(tmp_path):
    repo = checkout(tmp_path / "anywhere")
    assert target.deviceside_repo(tmp_path, {"DEVICE_SIDE_REPO": str(repo)}) == repo.resolve()


def test_deviceside_repo_refuses_an_env_that_isnt_one(tmp_path):
    with pytest.raises(SystemExit, match="not a deviceside checkout"):
        target.deviceside_repo(tmp_path, {"DEVICE_SIDE_REPO": str(tmp_path)})


def test_deviceside_repo_is_the_checkout_run_from(tmp_path):
    repo = checkout(tmp_path / "wt")
    (repo / "docs").mkdir()
    assert target.deviceside_repo(repo / "docs", {}) == repo.resolve()


def test_deviceside_repo_falls_back_to_a_sibling(tmp_path, monkeypatch):
    rig = tmp_path / "ha-rig"
    rig.mkdir()
    monkeypatch.setattr(target, "RIG_HOME", rig)
    sibling = checkout(tmp_path / "device-side")
    assert target.deviceside_repo(tmp_path, {}) == sibling
    renamed = checkout(tmp_path / "deviceside")
    assert target.deviceside_repo(tmp_path, {}) == renamed


def test_deviceside_repo_says_how_to_name_one(tmp_path, monkeypatch):
    monkeypatch.setattr(target, "RIG_HOME", tmp_path / "ha-rig")
    with pytest.raises(SystemExit, match="DEVICE_SIDE_REPO"):
        target.deviceside_repo(tmp_path, {})


def test_main_checkout_falls_back_outside_a_repo(tmp_path):
    assert target.main_checkout(tmp_path) == tmp_path


@pytest.mark.parametrize("stale", [".env.rig", "tests/rig/profiles"])
def test_a_household_file_left_in_deviceside_is_refused(tmp_path, stale):
    from tests.conftest import REAL_REFUSE_MOVED_FILES

    repo = checkout(tmp_path / "deviceside")
    (repo / stale).mkdir(parents=True)
    with pytest.raises(SystemExit, match=f"Move {repo.resolve() / stale} into {tmp_path / 'ha-rig'}"):
        REAL_REFUSE_MOVED_FILES(repo, tmp_path / "ha-rig")
    (repo / stale).rmdir()
    REAL_REFUSE_MOVED_FILES(repo, tmp_path / "ha-rig")  # nothing left: passes


def git(*args, cwd):
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True
    )


def worktree(tmp_path, branch):
    main = tmp_path / "main"
    main.mkdir()
    git("init", "-q", "-b", "main", cwd=main)
    git("commit", "-q", "--allow-empty", "-m", "x", cwd=main)
    wt = tmp_path / "wt"
    git("worktree", "add", "-q", "-b", branch, str(wt), cwd=main)
    return main, wt


def fake_gh(monkeypatch, out):
    real = subprocess.run

    def run(cmd, **kwargs):
        if cmd[0] == "gh":
            assert cmd[:4] == ["gh", "pr", "list", "--head"]
            if isinstance(out, Exception):
                raise out
            return subprocess.CompletedProcess(cmd, 0, stdout=out)
        return real(cmd, **kwargs)

    monkeypatch.setattr(target.subprocess, "run", run)


def test_a_wt_worktree_with_an_open_pr_is_owned(tmp_path, monkeypatch):
    _, wt = worktree(tmp_path, "wt/topic")
    fake_gh(monkeypatch, '[{"number": 7}]')
    from tests.conftest import REAL_REFUSE_UNOWNED

    REAL_REFUSE_UNOWNED(wt, {})


def test_a_wt_branch_without_an_open_pr_is_refused(tmp_path, monkeypatch):
    _, wt = worktree(tmp_path, "wt/topic")
    fake_gh(monkeypatch, "[]")
    from tests.conftest import REAL_REFUSE_UNOWNED

    with pytest.raises(SystemExit, match=r"wt/topic has no open PR.*pr-draft"):
        REAL_REFUSE_UNOWNED(wt, {})


def test_a_failing_gh_refuses_rather_than_passes(tmp_path, monkeypatch):
    _, wt = worktree(tmp_path, "wt/topic")
    fake_gh(monkeypatch, FileNotFoundError("gh"))
    from tests.conftest import REAL_REFUSE_UNOWNED

    with pytest.raises(SystemExit, match="Could not look up"):
        REAL_REFUSE_UNOWNED(wt, {})


def test_a_worktree_on_another_branch_is_refused(tmp_path):
    _, wt = worktree(tmp_path, "feature/x")
    from tests.conftest import REAL_REFUSE_UNOWNED

    with pytest.raises(SystemExit, match=r"not a wt/\* branch.*pr-draft"):
        REAL_REFUSE_UNOWNED(wt, {})


def test_a_detached_ci_style_clone_and_the_main_checkout_are_refused_without_the_opt_out(tmp_path):
    main, wt = worktree(tmp_path, "wt/topic")
    from tests.conftest import REAL_REFUSE_UNOWNED

    with pytest.raises(SystemExit, match="not a linked worktree"):
        REAL_REFUSE_UNOWNED(main, {})
    git("checkout", "-q", "--detach", cwd=wt)
    with pytest.raises(SystemExit, match="detached HEAD"):
        REAL_REFUSE_UNOWNED(wt, {})


def test_the_opt_out_passes_anywhere(tmp_path):
    from tests.conftest import REAL_REFUSE_UNOWNED

    REAL_REFUSE_UNOWNED(tmp_path, {target.UNOWNED_OK: "1"})


def test_record_dir_is_the_targets_own_answer_laid_out_under_a_root(tmp_path):
    repo = target.deviceside_repo()
    gate = target.check_release(repo)
    assert target.test_records_dir(repo, "shelly") == gate.test_records_dir("shelly")
    expected = tmp_path / gate.test_records_dir("shelly").relative_to(repo)
    assert target.test_records_dir(repo, "shelly", tmp_path) == expected
