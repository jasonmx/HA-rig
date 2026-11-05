"""The household's terms, for device-side's household check, which can't read the rig's files itself.

device-side is public, so its check (scripts/check_private.py there) refuses a commit naming a household detail. Its
own blocklist is .private-terms; the rig adds every profile's entity IDs and roaming bulbs' IEEEs, and .env.rig's HA
host and token, by writing them to TERMS_FILE in the device-side main checkout on each rig invocation, as
`term<TAB>label` lines. A secret is reported by its label, never echoed. `python -m ha_rig.terms` writes it by hand,
after editing a profile.
"""

import sys
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlparse

from .profile import ENV_FILE, Profile, colon_ieee, load_env, profile_paths
from .target import device_side_repo, main_checkout

TERMS_FILE = ".private-terms.rig"  # gitignored in device-side
HEADER = "# Written by ha-rig on each run (python -m ha_rig.terms); edits are overwritten.\n"
# Hosts the docs and examples name on purpose.
GENERIC_HOSTS = frozenset({"homeassistant.local", "localhost", "127.0.0.1"})


def private_terms(profiles: Iterable[Path] | None = None, env_file: Path = ENV_FILE) -> dict[str, str]:
    """Lowercased term -> label to report. `profiles` defaults to every profile."""
    terms: dict[str, str] = {}
    for path in profile_paths() if profiles is None else profiles:
        if path.exists():
            terms.update((entity_id.lower(), entity_id) for entity_id in profile_entities(Profile.load(path)))
    if env_file.exists():
        env = load_env(env_file)
        if (host := urlparse(env.get("HA_URL", "")).hostname) and host not in GENERIC_HOSTS:
            terms[host.lower()] = host
        if token := env.get("HA_TOKEN"):
            terms[token.lower()] = "HA_TOKEN from .env.rig"
    return terms


def profile_entities(profile: Profile) -> set[str]:
    """Every entity ID a profile names (its entities, their groups' members and supplies, its pause list), and each
    roaming bulb's IEEE: as bare hex, which `0x`-prefixed spellings contain, and with colons."""
    named = set(profile.paused_by())
    for entities in profile.integrations.values():
        for entity in entities:
            named |= {entity.entity_id, *entity.members, *filter(None, [entity.powered_by])}
    for bulb in profile.roam:
        named |= {bulb.entity_id, bulb.ieee, colon_ieee(bulb.ieee)}
    return named


def render(terms: dict[str, str]) -> str:
    return HEADER + "".join(f"{term}\t{label}\n" for term, label in sorted(terms.items()))


def write(repo: Path, terms: dict[str, str] | None = None) -> Path:
    """Write the terms into `repo`'s main checkout, shared by its worktrees; untouched when they haven't changed."""
    path = main_checkout(repo) / TERMS_FILE
    text = render(private_terms() if terms is None else terms)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
        path.chmod(0o600)  # it holds the HA token
    return path


def main() -> int:
    try:
        path = write(device_side_repo())
    except ValueError as err:
        print(f"Can't read the household's terms: {err}")
        return 1
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
