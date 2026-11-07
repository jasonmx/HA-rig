#!/usr/bin/env python3
"""Refuse a commit whose files or message name the household's private details.

This repo is private, but its code is kept as free of household details as deviceside's: the profiles and .env.rig
are gitignored, and this check keeps their terms out of what is committed. Terms come from every profile and
.env.rig (ha_rig/terms.py) and .private-terms (one term per line, for names, hostnames and anything else), all read
from the main checkout so a linked worktree is checked too. Where none of those exist (CI) the check passes.

Usage: scripts/check_private.py FILE ...   (pre-commit passes staged files, or the message file at commit-msg)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ha_rig.profile import MAIN_CHECKOUT  # noqa: E402
from ha_rig.terms import private_terms  # noqa: E402

PRIVATE_TERMS = MAIN_CHECKOUT / ".private-terms"


def all_terms(terms_file: Path = PRIVATE_TERMS) -> dict[str, str]:
    """Lowercased term -> label to report: the rig's, and .private-terms's."""
    terms = private_terms()
    if terms_file.exists():
        for line in terms_file.read_text().splitlines():
            if (line := line.strip()) and not line.startswith("#"):
                terms[line.lower()] = line
    return terms


def find(paths: list[Path], terms: dict[str, str]) -> list[str]:
    hits = []
    for path in paths:
        try:
            lines = path.read_text().splitlines()
        except UnicodeDecodeError, FileNotFoundError:
            continue
        for number, line in enumerate(lines, 1):
            lowered = line.lower()
            hits.extend(f"{path}:{number}: {label}" for term, label in terms.items() if term in lowered)
    return hits


def main(argv: list[str]) -> int:
    try:
        terms = all_terms()
    except ValueError as err:
        print(f"Can't read the household's terms, so the commit is refused: {err}")
        return 1
    if not terms:
        return 0
    if hits := find([Path(arg) for arg in argv], terms):
        print("Private details in a commit:", *hits, sep="\n  ")
        print("Remove them, or move them to a gitignored file (CLAUDE.local.md, profiles/, .env.rig).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
