"""Staged test records (ha_rig/staging.py): what a promote copies, drops from the ledger and refuses."""

import datetime
from pathlib import Path

from ha_rig.progress import Progress
from ha_rig.staging import Staging

KEY = ("shelly", "raise_brightness", "f1")


def now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def ledger(tmp_path: Path) -> Progress:
    """The `lab` ledger in tmp_path, holding one entry at KEY."""
    progress = Progress.load(tmp_path, "lab")
    entry = {"integration": KEY[0], "suite": KEY[1], "fingerprint": KEY[2], "cases": [], "touched": now()}
    progress.entries[KEY] = entry
    progress.changed.add(KEY)
    progress.save()
    assert Progress.load(tmp_path, "lab").entries
    return progress


def staging(tmp_path: Path) -> Staging:
    """A run's staging in tmp_path/reports with one record, for the checkout at tmp_path/repo."""
    repo = tmp_path / "repo"
    found = Staging(tmp_path / "reports" / "run.records", repo, repo / "docs" / "test-records", tmp_path, "lab")
    path = found.directory / "shelly" / "raise_brightness.md"
    path.parent.mkdir(parents=True)
    path.write_text("new\n")
    found.add(path, KEY)
    return found


def test_a_promote_copies_the_record_drops_its_ledger_entry_and_clears_the_staging(tmp_path, capsys):
    ledger(tmp_path)
    staged = staging(tmp_path)
    loaded = Staging.load(staged.directory)  # as --promote finds it, from the manifest alone
    assert loaded.promote() == [tmp_path / "repo" / "docs" / "test-records" / "shelly" / "raise_brightness.md"]
    assert (tmp_path / "repo" / "docs" / "test-records" / "shelly" / "raise_brightness.md").read_text() == "new\n"
    assert not Progress.load(tmp_path, "lab").entries
    assert not staged.directory.exists()
    assert "Wrote docs/test-records/shelly/raise_brightness.md; review the diff" in capsys.readouterr().out


def test_a_record_changed_since_staging_is_never_overwritten(tmp_path, capsys):
    ledger(tmp_path)
    staged = staging(tmp_path)
    target = tmp_path / "repo" / "docs" / "test-records" / "shelly" / "raise_brightness.md"
    target.parent.mkdir(parents=True)
    target.write_text("a later run's\n")
    assert staged.promote() == []
    assert target.read_text() == "a later run's\n"
    assert Progress.load(tmp_path, "lab").entries  # the ledger keeps the suite
    assert Staging.load(staged.directory).records  # and the staging, to compare by hand
    assert "Not promoted shelly/raise_brightness.md" in capsys.readouterr().out


def test_a_promote_that_stopped_part_way_finishes(tmp_path):
    ledger(tmp_path)
    staged = staging(tmp_path)
    target = tmp_path / "repo" / "docs" / "test-records" / "shelly" / "raise_brightness.md"
    target.parent.mkdir(parents=True)
    target.write_text("new\n")  # copied, but the ledger wasn't saved
    assert staged.promote() == [target]
    assert not Progress.load(tmp_path, "lab").entries


def test_the_command_names_the_checkout_and_the_staging(tmp_path):
    staged = staging(tmp_path)
    command = staged.command()
    assert command.startswith(f"DEVICE_SIDE_REPO={tmp_path / 'repo'} ")
    assert command.endswith(f"scripts/rig.py --promote {staged.directory}")
