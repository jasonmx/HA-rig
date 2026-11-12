"""The deviceside checkout a run tests, and this rig's own checkout.

The rig tests whichever deviceside checkout it is run from, so a run from a linked worktree deploys and records that
worktree's code: DEVICE_SIDE_REPO when set, else the git checkout holding the current directory, else a checkout beside
this one named deviceside (the repo's name) or device-side (so the rig's own tests run from here). The locks, journals
and baseline live in the target's git common dir, shared by every worktree of it; the profiles and .env.rig live in
this rig's main checkout.

Its step files, the test plan, live in `tests/rig/steps/` (STEPS), and its test records in
`docs/integrations/<integration>/test-records/` (RECORDS), as deviceside lays them out.

Pure file and git reading: the household check and the runner's preflight load it before anything mutates.
"""

import json
import os
import subprocess
from pathlib import Path

RIG_HOME = Path(__file__).resolve().parents[1]
COMPONENT = "custom_components/deviceside"
STEPS = "tests/rig/steps"  # deviceside's step files, one per action
RECORDS = "docs/integrations/{integration}/test-records"  # deviceside's test records directory, per integration
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


def steps_dir(repo: Path) -> Path:
    """The step files' directory in `repo`."""
    return repo / STEPS


def head_commit(repo: Path) -> str | None:
    """`repo`'s HEAD commit; None outside a checkout or without git."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
    except OSError, subprocess.CalledProcessError:
        return None


def committed_step_sources(repo: Path, commit: str) -> dict[str, bytes]:
    """The `*.yaml` files directly in STEPS at `commit`, by name, as committed; none where git can't say."""

    def git(*args: str) -> bytes:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout

    sources = {}
    try:
        for entry in filter(None, git("ls-tree", "-z", commit, "--", f"{STEPS}/").split(b"\0")):
            meta, _, path = entry.partition(b"\t")
            name = Path(path.decode()).name
            if meta.split()[1] == b"blob" and name.endswith(".yaml"):
                sources[name] = git("cat-file", "blob", f"{commit}:{STEPS}/{name}")
    except OSError, subprocess.CalledProcessError:
        return {}
    return sources


def test_records_dir(repo: Path, integration: str, root: Path | None = None) -> Path:
    """The integration's test records directory in `repo`, laid out under `root` instead when given (a run's staging
    mirrors the checkout)."""
    return (root or repo) / RECORDS.format(integration=integration)


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
