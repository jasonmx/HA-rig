"""Staged test records: a run's records wait next to its report until the run is known to be publishable.

A run writes each record it earns into `<report>.records/`, laid out as the checkout's `docs/test-records`
(`<integration>/<suite>.md`), with a manifest (`staged.json`) naming the checkout, the ledger and, per record, its
suite's ledger entry and the hash of the checkout's file when it was staged. Only a run that ends cleanly promotes its
records into the checkout itself; any other end leaves the checkout untouched and prints the one command that
promotes them (`command`).

Promoting copies each record into the checkout, then drops its suite's ledger entry (the record carries its coverage
from then on), then removes the staged dir. A record whose file in the checkout changed since it was staged (a later
run wrote it, or this dir was promoted already) is left staged and named, never overwritten.

The ledger keeps a staged suite's cases until promotion, so a staged dir lost with the temp folder loses no result: the
next run that ends cleanly, running any case, stages and promotes the record from the ledger. When every selected
suite is covered there is no such run, and `--fresh` reruns them.
"""

import hashlib
import json
import shlex
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .progress import EntryKey, Progress

MANIFEST = "staged.json"
RIG = Path(__file__).resolve().parents[1] / "scripts" / "rig.py"


def digest(path: Path) -> str | None:
    """The file's content hash, or None when there is none."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


@dataclass
class Staging:
    """The records one run staged, in `directory`, for `records_dir` (the checkout's docs/test-records), with the
    ledger (`ledger_dir`, `profile`) that holds their suites' entries until promotion."""

    directory: Path
    repo: Path
    records_dir: Path
    ledger_dir: Path | None = None
    profile: str | None = None
    records: dict[str, dict] = field(default_factory=dict)  # by path relative to records_dir

    def add(self, staged: Path, entry: EntryKey | None) -> None:
        """Note a record written at `staged` (under `directory`) and its suite's ledger entry, and save the manifest:
        the hash it notes is of the checkout's file as it is now, before any promotion."""
        relative = staged.relative_to(self.directory).as_posix()
        self.records[relative] = {"entry": list(entry) if entry else None, "base": digest(self.records_dir / relative)}
        self.save()

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        manifest = {
            "repo": str(self.repo),
            "records_dir": str(self.records_dir),
            "ledger_dir": str(self.ledger_dir) if self.ledger_dir else None,
            "profile": self.profile,
            "records": self.records,
        }
        (self.directory / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")

    @classmethod
    def load(cls, directory: Path) -> Staging:
        """The staging in `directory`. Raises SystemExit, naming the problem, when it holds none."""
        try:
            manifest = json.loads((directory / MANIFEST).read_text())
            return cls(
                directory,
                Path(manifest["repo"]),
                Path(manifest["records_dir"]),
                Path(manifest["ledger_dir"]) if manifest.get("ledger_dir") else None,
                manifest.get("profile"),
                dict(manifest["records"]),
            )
        except (OSError, ValueError, KeyError, TypeError) as err:
            raise SystemExit(f"{directory} holds no staged records ({MANIFEST}): {err}") from None

    def command(self) -> str:
        """The one command that promotes these records, from any directory."""
        repo, rig, directory = (shlex.quote(str(path)) for path in (self.repo, RIG, self.directory))
        return f"DEVICE_SIDE_REPO={repo} {rig} --promote {directory}"

    def promote(self, progress: Progress | None = None, out: Callable[[str], None] = print) -> list[Path]:
        """Copy each staged record into the checkout, drop its suite's ledger entry (from `progress`, else the
        ledger on disk) and, once every record is promoted, remove the staged dir. Returns the checkout's paths
        written. A record whose file changed since staging stays staged, and is named."""
        promoted, refused, dropped = [], {}, []
        for relative, staged in sorted(self.records.items()):
            if why := self._copy(relative, staged.get("base")):
                refused[relative] = why
                continue
            promoted.append(self.records_dir / relative)
            if staged.get("entry"):
                dropped.append(tuple(staged["entry"]))
        self._drop(dropped, progress)
        for target in promoted:
            out(f"Wrote {display(target, self.repo)}; review the diff before committing it.")
        for relative, why in refused.items():
            out(f"Not promoted {relative}: {why}.")
        if refused:
            self.records = {relative: self.records[relative] for relative in refused}
            self.save()
        else:
            shutil.rmtree(self.directory, ignore_errors=True)
        return promoted

    def _copy(self, relative: str, base: str | None) -> str | None:
        """Copy one staged record into the checkout; None once it is there (one already there, from an attempt that
        stopped part way, counts), else why not: the staged file is gone, or the checkout's changed since staging.
        Either keeps the suite in the ledger."""
        source, target = self.directory / relative, self.records_dir / relative
        if (staged := digest(source)) is None:
            return f"{source} is gone; its suite stays saved, and the next run that ends cleanly writes its record"
        if (now := digest(target)) == staged:
            return None
        if now != base:
            return (
                f"{display(target, self.repo)} changed since it was staged in {self.directory}; compare them and "
                "copy it by hand if it should win"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return None

    def _drop(self, entries: list[EntryKey], progress: Progress | None) -> None:
        """Take the promoted records' entries out of the ledger: `progress`, else the one on disk."""
        if not entries:
            return
        if progress is None and self.ledger_dir is not None and self.profile is not None:
            progress = Progress.load(self.ledger_dir, self.profile)
        if progress is not None:
            progress.drop_entries(entries)
            progress.save()


def display(path: Path, repo: Path) -> str:
    """`path` relative to `repo` when inside it."""
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)
