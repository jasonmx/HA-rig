"""Which suites are covered: a fresh test record, or a saved result for every case, at the code they prove.

A suite's code is that of each adapter its tests call on its integration: its own action's and those its `call`
steps name (engine.adapters_for). Each adapter's files are deviceside's own `check_release.adapter_code`, read from
the target checkout's `scripts/`, so a suite goes stale exactly when the release gate says its record does: `core/`
and `actions/` are left out, and HA's version doesn't count.

A suite's **fingerprint** at a commit hashes the git blob of each of those files there with the rig's step file, so
editing a test changes it too. The saved progress (ha_rig/progress.py) keys its entries by it.

A **fresh record** (`docs/test-records/<integration>/<suite name>.md`) cites a commit at which the suite's code is
the same as at the one tested now, as the gate compares them (`git diff --name-only`), and lists exactly the case
IDs the suite plans now.

Pure file and git reading: --coverage and the plan run it before anything changes.
"""

import datetime
import hashlib
import inspect
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .engine import Case, CaseResult, Outcome, Suite, adapters_for, cases
from .profile import Profile
from .progress import Progress

CITED_COMMIT = re.compile(r"^\| Source \|.*\bcommit `([0-9a-f]{7,40})`", re.MULTILINE)  # check_release's
DATE = re.compile(r"^\| Date \| (\d{4}-\d{2}-\d{2}) \|", re.MULTILINE)
CASE_ROW = re.compile(r"^\| `([^`]+)` \|", re.MULTILINE)
RECORDS = Path("docs") / "test-records"
INTEGRATIONS = "custom_components/deviceside/integrations/"  # left off the changed files a stale record names


def adapter_code(repo: Path) -> Callable[[Path], list[Path]]:
    """deviceside's `check_release.adapter_code`, from `repo`'s scripts/. Its import puts `repo` first on sys.path,
    which is put back, so the rig's own `tests` package still wins."""
    if (loaded := sys.modules.get("check_release")) is None:
        saved = list(sys.path)
        sys.path.append(str(repo / "scripts"))
        try:
            import check_release as loaded  # noqa: PLC0415 - from the target checkout, found only at run time
        finally:
            sys.path[:] = saved
    return loaded.adapter_code


def code(suite: Suite, registry: Any, repo: Path) -> tuple[str, ...]:
    """The files, relative to `repo`, of every adapter the suite's tests call on its integration (see the module
    docstring); none for a suite with no adapter."""
    actions = [suite.action, *sorted(adapters_for(suite, registry))]
    adapters = [adapter for action in actions if (adapter := registry.adapter(action, suite.integration))]
    files_of = adapter_code(repo)
    files = {
        path.resolve().relative_to(repo.resolve()).as_posix()
        for adapter in adapters
        for path in files_of(Path(inspect.getsourcefile(type(adapter))))
    }
    return tuple(sorted(files))


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True)


def fingerprint(repo: Path, commit: str, files: Iterable[str], step_file: Path) -> str:
    """The suite's fingerprint at `commit`: each of its code `files`' blob there, with the step file's hash. A file
    missing at the commit counts as itself missing, not an error."""
    files = sorted(files)
    blobs = [
        git(repo, "rev-parse", "--verify", "--quiet", f"{commit}:{path}", check=False).stdout.strip() for path in files
    ]
    digest = hashlib.sha256()
    for path, blob in zip(files, blobs, strict=True):
        digest.update(f"{path}\0{blob or '-'}\n".encode())
    digest.update(hashlib.sha256(step_file.read_bytes()).hexdigest().encode())
    return digest.hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class Record:
    """What coverage reads from a test record: the commit it cites, its date and the case IDs in its Tests table."""

    path: Path
    commit: str | None
    date: str | None
    case_ids: frozenset[str]

    @classmethod
    def read(cls, path: Path) -> Record | None:
        if not path.is_file():
            return None
        text = path.read_text()
        cited, date = CITED_COMMIT.search(text), DATE.search(text)
        _, _, tests = text.partition("## Tests")
        return cls(path, cited and cited[1], date and date[1], frozenset(CASE_ROW.findall(tests)))

    @property
    def age_days(self) -> int | None:
        try:
            return (datetime.date.today() - datetime.date.fromisoformat(self.date)).days if self.date else None
        except ValueError:
            return None


def record_path(repo: Path, suite: Suite) -> Path:
    return repo / RECORDS / suite.integration / f"{suite.name}.md"


@dataclass(frozen=True, slots=True)
class Freshness:
    """A suite's record against the code tested now: `record` None when it has none; `stale` says why it is stale,
    or is None when it is fresh."""

    record: Record | None
    stale: str | None

    @property
    def fresh(self) -> bool:
        return self.record is not None and self.stale is None

    def __str__(self) -> str:
        if self.record is None:
            return "no record"
        if self.stale is not None:
            return f"stale: {self.stale}"
        return f"record {self.record.date or 'undated'}, commit {self.record.commit}"


def freshness(repo: Path, suite: Suite, commit: str, files: Iterable[str], planned: Iterable[str]) -> Freshness:
    """Whether the suite's record is fresh at `commit` (see the module docstring), given its code `files` and the
    case IDs it `planned`."""
    if (record := Record.read(record_path(repo, suite))) is None:
        return Freshness(None, None)
    if record.commit is None:
        return Freshness(record, "cites no commit")
    if git(repo, "rev-parse", "--verify", "--quiet", f"{record.commit}^{{commit}}", check=False).returncode:
        return Freshness(record, f"cites {record.commit}, which git can't find")
    if (diff := git(repo, "diff", "--name-only", record.commit, commit, "--", *files, check=False)).returncode:
        return Freshness(record, f"git can't compare {record.commit} with {commit}: {diff.stderr.strip()}")
    if changed := diff.stdout.split():
        return Freshness(record, ", ".join(path.removeprefix(INTEGRATIONS) for path in changed))
    planned = frozenset(planned)
    if record.case_ids != planned:
        new, gone = sorted(planned - record.case_ids), sorted(record.case_ids - planned)
        why = [f"new {', '.join(new)}" if new else "", f"gone {', '.join(gone)}" if gone else ""]
        return Freshness(record, "cases changed: " + "; ".join(filter(None, why)))
    return Freshness(record, None)


@dataclass(slots=True)
class SuiteCoverage:
    """One suite's coverage now: its fingerprint, its record's freshness, the cases it plans and their saved results
    that still hold."""

    suite: Suite
    fingerprint: str
    freshness: Freshness
    planned: list[Case]
    saved: dict[str, CaseResult]

    @property
    def covered(self) -> bool:
        return self.freshness.fresh or self.complete

    @property
    def complete(self) -> bool:
        """Whether every planned case has a saved result."""
        return bool(self.planned) and all(case.id in self.saved for case in self.planned)

    @property
    def missing(self) -> list[Case]:
        return [case for case in self.planned if case.id not in self.saved]

    def __str__(self) -> str:
        if self.freshness.fresh:
            return f"covered ({self.freshness})"
        if self.complete:
            return "covered (saved; no record yet)"
        if self.saved:
            return f"partial {len(self.saved)}/{len(self.planned)}"
        return f"uncovered ({self.freshness})"


def holds(case: Case, result: CaseResult, unattended: bool) -> bool:
    """Whether a saved result still stands for `case`: the profile skips it for the same reason, or it moved the
    entities the case plans now. A `not observed` stands only for an unattended run."""
    if case.skip:
        return result.outcome is Outcome.SKIPPED and result.detail == case.skip
    if result.outcome is Outcome.NOT_OBSERVED:
        return unattended
    return result.outcome is not Outcome.SKIPPED and result.entity_ids == tuple(case.entities.values())


def of(
    suites: Iterable[Suite],
    profile: Profile,
    registry: Any,
    repo: Path,
    commit: str,
    progress: Progress | None,
    *,
    fresh: bool = False,
    unattended: bool = False,
) -> list[SuiteCoverage]:
    """Each suite's coverage at `commit`, for those with an adapter; tracking each in `progress` at its fingerprint.
    Saved results that no longer hold are forgotten, and a suite with a saved failure is dropped, to rerun whole.
    `fresh` (--fresh) ignores the records and drops the saved cases."""
    found = []
    for suite in suites:
        if registry.adapter(suite.action, suite.integration) is None:
            continue
        files = code(suite, registry, repo)
        suite_fingerprint = fingerprint(repo, commit, files, suite.file.path)
        planned = cases(suite, profile, None, adapters_for(suite, registry))
        saved: dict[str, CaseResult] = {}
        if progress is not None:
            progress.track(suite, suite_fingerprint)
            entry = progress.saved(suite)
            if fresh or any(result.outcome is Outcome.FAIL for result in entry.values()):
                progress.drop(suite)
            else:
                saved = {c.id: entry[c.id] for c in planned if c.id in entry and holds(c, entry[c.id], unattended)}
                progress.forget(suite, set(entry) - set(saved))
        record = Freshness(None, None) if fresh else freshness(repo, suite, commit, files, [c.id for c in planned])
        found.append(SuiteCoverage(suite, suite_fingerprint, record, planned, saved))
    return found
