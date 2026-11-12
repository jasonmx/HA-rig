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


def test_test_records_dir_is_where_the_target_keeps_its_records_laid_out_under_a_root(tmp_path):
    repo = target.deviceside_repo()
    assert target.test_records_dir(repo, "shelly").is_dir()  # the layout the target really has
    expected = tmp_path / "docs" / "integrations" / "shelly" / "test-records"
    assert target.test_records_dir(repo, "shelly", tmp_path) == expected


def release_check(repo: Path, body: str) -> Path:
    """A stand-in for deviceside's release check in `repo`, running `body` as its script."""
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "check_release.py").write_text(f"import sys\n{body}\n")
    return repo


@pytest.mark.parametrize(
    ("body", "listed"),
    [
        ("print('[\"hue\", \"zha\"]'); print('error: hue: no sign-off', file=sys.stderr); sys.exit(1)", ["hue", "zha"]),
        ("print('[]')", []),
    ],
)
def test_the_release_checks_list_is_the_selection_whether_it_exits_1_or_0(tmp_path, body, listed):
    assert target.signoffs_needed(release_check(tmp_path, body)) == listed


def test_the_release_check_is_asked_for_its_json_list(tmp_path):
    repo = release_check(tmp_path, "import json; print(json.dumps(sys.argv[1:]))")
    assert target.signoffs_needed(repo) == ["--signoffs", "--json"]


def test_a_release_check_that_crashes_stops_the_run_with_what_it_said(tmp_path):
    repo = release_check(tmp_path, "print('Traceback: boom', file=sys.stderr); sys.exit(2)")
    with pytest.raises(SystemExit, match=r"failed \(exit 2\): Traceback: boom"):
        target.signoffs_needed(repo)


@pytest.mark.parametrize("output", ["Since v0.0.1:", "{}", "[1, 2]", ""])
def test_a_release_check_that_prints_no_list_of_names_stops_the_run(tmp_path, output):
    repo = release_check(tmp_path, f"print({output!r}); sys.exit(1)")
    with pytest.raises(SystemExit, match="printed no JSON list of integrations"):
        target.signoffs_needed(repo)


def test_a_checkout_without_the_release_check_stops_the_run(tmp_path):
    with pytest.raises(SystemExit, match=r"failed \(exit 2\)"):
        target.signoffs_needed(tmp_path)
