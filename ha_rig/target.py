"""The deviceside checkout a run tests, and this rig's own checkout.

The rig tests whichever deviceside checkout it is run from, so a run from a linked worktree deploys and records that
worktree's code: DEVICE_SIDE_REPO when set, else the git checkout holding the current directory, else a checkout beside
this one named deviceside (the repo's name) or device-side (so the rig's own tests run from here). The locks, journals
and baseline live in the target's git common dir, shared by every worktree of it; the profiles and .env.rig live in
this rig's main checkout.

Its step files, the test plan, live in `tests/rig/steps/` (STEPS, shared) and `tests/integrations/<x>/rig/` (an
integration's own, integration_steps_dirs), and its test records in
`docs/integrations/<integration>/test-records/` (RECORDS), as deviceside lays them out.

What needs testing is deviceside's to say: `signoffs_needed` asks its release check (SIGNOFFS) for the integrations
still needing a sign-off, the run selection of --signoffs.

Pure reading, of files, git and that check: the household check and the runner's preflight load it before anything
mutates.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

RIG_HOME = Path(__file__).resolve().parents[1]
COMPONENT = "custom_components/deviceside"
STEPS = "tests/rig/steps"  # deviceside's shared step files, one per action
INTEGRATIONS = "tests/integrations"  # deviceside's integration test packages: each one's own step files are in its rig/
RECORDS = "docs/integrations/{integration}/test-records"  # deviceside's test records directory, per integration
SIBLINGS = ("deviceside", "device-side")
SIGNOFFS = ("scripts/check_release.py", "--signoffs", "--json")  # deviceside's list of integrations needing a sign-off
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


def integration_steps_dirs(repo: Path) -> dict[str, Path]:
    """Each integration's own step files' directory in `repo`, by integration, for those that have one."""
    return {path.parent.name: path for path in sorted((repo / INTEGRATIONS).glob("*/rig")) if path.is_dir()}


def is_step_path(path: str) -> bool:
    """Whether `path`, relative to the checkout, is a step file: `*.yaml` directly in STEPS or in an integration's
    rig/."""
    parts = Path(path).parts
    shared = Path(path).parent.as_posix() == STEPS
    own = len(parts) == 5 and Path(*parts[:2]).as_posix() == INTEGRATIONS and parts[3] == "rig"
    return path.endswith(".yaml") and (shared or own)


def head_commit(repo: Path) -> str | None:
    """`repo`'s HEAD commit; None outside a checkout or without git."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
    except OSError, subprocess.CalledProcessError:
        return None


def committed_step_sources(repo: Path, commit: str) -> dict[str, bytes]:
    """The step files at `commit` (is_step_path), by path relative to the checkout, as committed; none where git can't
    say."""

    def git(*args: str) -> bytes:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout

    sources = {}
    try:
        for entry in filter(None, git("ls-tree", "-r", "-z", commit, "--", STEPS, INTEGRATIONS).split(b"\0")):
            meta, _, raw = entry.partition(b"\t")
            if meta.split()[1] == b"blob" and is_step_path(path := raw.decode()):
                sources[path] = git("cat-file", "blob", f"{commit}:{path}")
    except OSError, subprocess.CalledProcessError:
        return {}
    return sources


def signoffs_needed(repo: Path) -> list[str]:
    """The integrations `repo`'s release check lists as still needing a sign-off (SIGNOFFS), run with the interpreter
    the rig already imports the checkout's code with. Exit 0 and 1 both carry the list: the exit code says whether the
    release gate fails, not whether the list is empty, so 0 can list some (an Untested fix caveat passes the gate) and
    1 with nothing listed means an error that is no missing sign-off, which raises SystemExit quoting the check's
    stderr. Any other exit, or output that is no JSON list of names, raises SystemExit with what the check said."""
    command = " ".join(SIGNOFFS)
    try:
        done = subprocess.run([sys.executable, *SIGNOFFS], cwd=repo, capture_output=True, text=True)
    except OSError as err:
        raise SystemExit(f"Could not run deviceside's {command}: {err}") from err
    said = (done.stderr.strip() or done.stdout.strip() or "no output").splitlines()[-1]
    if done.returncode not in (0, 1):
        raise SystemExit(f"deviceside's {command} failed (exit {done.returncode}): {said}")
    try:
        listed = json.loads(done.stdout)
    except ValueError:
        listed = None
    if not isinstance(listed, list) or not all(isinstance(name, str) for name in listed):
        raise SystemExit(f"deviceside's {command} printed no JSON list of integrations: {said}")
    if done.returncode == 1 and not listed:
        raise SystemExit(f"deviceside's check failed with nothing listed: {done.stderr.strip() or 'no output'}")
    return listed


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
