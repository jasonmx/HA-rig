"""The deviceside checkout a run tests, and this rig's own checkout.

The rig tests whichever deviceside checkout it is run from, so a run from a linked worktree deploys and records that
worktree's code: DEVICE_SIDE_REPO when set, else the git checkout holding the current directory, else a checkout beside
this one named deviceside (the repo's name) or device-side (so the rig's own tests run from here). The locks, journals
and baseline live in the target's git common dir, shared by every worktree of it; the profiles and .env.rig live in
this rig's main checkout.

The target's own `scripts/check_release.py` says where its test records live (`record_dir`) and which files are an
adapter's code (`adapter_code`), so the rig follows the gate's layout and staleness rule rather than restating them.

Pure file and git reading: the household check and the runner's preflight load it before anything mutates.
"""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

RIG_HOME = Path(__file__).resolve().parents[1]
COMPONENT = "custom_components/deviceside"
SIBLINGS = ("deviceside", "device-side")
UNOWNED_OK = "HA_RIG_UNOWNED_CHECKOUT"  # set to 1 to run from a checkout with no worktree-and-PR owner


def git_path(directory: Path, flag: str) -> Path | None:
    """`git rev-parse <flag>` in `directory`, absolute; None outside a checkout or without git."""
    try:
        out = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--path-format=absolute", flag],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except OSError, subprocess.CalledProcessError:
        return None
    return Path(out)


def main_checkout(repo: Path) -> Path:
    """The main checkout of `repo`, which holds the gitignored files every linked worktree shares; else `repo`."""
    common = git_path(repo, "--git-common-dir")
    return common.parent if common else repo


def check_release(repo: Path) -> ModuleType:
    """deviceside's `scripts/check_release.py` in `repo`, loaded once per checkout. Loading it puts its checkout first
    on sys.path, which is put back, so the rig's own `tests` package still wins."""
    script = (repo / "scripts" / "check_release.py").resolve()
    name = f"check_release_{hashlib.sha256(str(script).encode()).hexdigest()[:8]}"
    if (loaded := sys.modules.get(name)) is None:
        spec = importlib.util.spec_from_file_location(name, script)
        loaded = importlib.util.module_from_spec(spec)
        saved = list(sys.path)
        try:
            spec.loader.exec_module(loaded)
        finally:
            sys.path[:] = saved
        sys.modules[name] = loaded
    return loaded


def record_dir(repo: Path, integration: str, root: Path | None = None) -> Path:
    """The integration's test records directory in `repo`, as its `check_release.record_dir` says, laid out under
    `root` instead when given (a run's staging mirrors the checkout)."""
    loaded = check_release(repo)
    return (root or repo) / loaded.record_dir(integration).relative_to(loaded.REPO)


def is_deviceside(path: Path) -> bool:
    return (path / COMPONENT / "manifest.json").is_file()


def deviceside_repo(cwd: Path | None = None, env: dict[str, str] | None = None) -> Path:
    """The deviceside checkout to test (see the module docstring). Raises SystemExit, naming the fix, if none is."""
    env = os.environ if env is None else env
    if explicit := env.get("DEVICE_SIDE_REPO"):
        path = Path(explicit).expanduser().resolve()
        if not is_deviceside(path):
            raise SystemExit(f"DEVICE_SIDE_REPO={explicit} is not a deviceside checkout: no {COMPONENT}")
        return path
    if (top := git_path(cwd or Path.cwd(), "--show-toplevel")) and is_deviceside(top):
        return top
    for name in SIBLINGS:
        if is_deviceside(sibling := main_checkout(RIG_HOME).parent / name):
            return sibling
    raise SystemExit("Run the rig from a deviceside checkout, or set DEVICE_SIDE_REPO to one")


def refuse_moved_files(repo: Path, rig_checkout: Path) -> None:
    """Refuse while a household file is still where the rig kept it inside deviceside, so no run reads half of a
    move and the old copy doesn't linger outside the household check's reach."""
    old = main_checkout(repo)
    if stale := [path for path in (old / ".env.rig", old / "tests" / "rig" / "profiles") if path.exists()]:
        listed = ", ".join(str(path) for path in stale)
        raise SystemExit(f"Move {listed} into {rig_checkout} (profiles/ and .env.rig there), then run again")


def refuse_unowned_checkout(repo: Path, env: dict[str, str] | None = None) -> None:
    """Refuse a run from a deviceside checkout that is not a worktree on a `wt/*` branch with an open PR: a run
    deploys and records that code, and a PR's owner is whoever works in its worktree. UNOWNED_OK=1 opts out, for
    the main checkout and for a detached CI clone. The read-only modes (--check, --dry-run, --recover) deploy and
    record nothing in the checkout, so the runner does not call this for them."""
    env = os.environ if env is None else env
    if env.get(UNOWNED_OK) == "1":
        return
    fix = (
        f"Open a draft PR from a wt/ worktree (pr-draft <plan>), or set {UNOWNED_OK}=1 to run from this checkout anyway"
    )
    git_dir, common = git_path(repo, "--git-dir"), git_path(repo, "--git-common-dir")
    if git_dir is None or git_dir == common:
        raise SystemExit(f"{repo} is not a linked worktree of deviceside. {fix}")
    try:
        branch = subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
    except OSError, subprocess.CalledProcessError:
        branch = ""
    if not branch.startswith("wt/"):
        raise SystemExit(f"{repo} is on {branch or 'a detached HEAD'}, not a wt/* branch. {fix}")
    try:
        listed = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        open_prs = json.loads(listed)
    except (OSError, subprocess.CalledProcessError, ValueError) as err:
        raise SystemExit(f"Could not look up {branch}'s open PR with gh ({err}). {fix}") from err
    if not open_prs:
        raise SystemExit(f"{branch} has no open PR. {fix}")
