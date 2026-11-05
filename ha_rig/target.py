"""The deviceside checkout a run tests, and this rig's own checkout.

The rig tests whichever deviceside checkout it is run from, so a run from a linked worktree deploys and records that
worktree's code: DEVICE_SIDE_REPO when set, else the git checkout holding the current directory, else a checkout beside
this one named deviceside (the repo's name) or device-side (so the rig's own tests run from here). The locks, journals
and baseline live in the target's git common dir, shared by every worktree of it; the profiles and .env.rig live in
this rig's main checkout.

Pure file and git reading: the household check and the runner's preflight load it before anything mutates.
"""

import os
import subprocess
from pathlib import Path

RIG_HOME = Path(__file__).resolve().parents[1]
COMPONENT = "custom_components/deviceside"
SIBLINGS = ("deviceside", "device-side")


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
