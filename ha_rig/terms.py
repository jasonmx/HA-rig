"""The household's terms, for the household check (check-private, from jasonmx/pre-commit-hooks), which reads only
term files.

Both repos' check refuses a commit naming a household detail. Its own blocklist is .private-terms; the rig adds every
profile's entity IDs and roaming bulbs' IEEEs, and from .env.rig the HA and Hue bridge hosts, both tokens and the Hue
app key, and the restricted sensor's entity ID, by writing them to TERMS_FILE as `term<TAB>label` lines. A secret is
reported by its label, never echoed. The rig writes deviceside's on each invocation, and `python -m ha_rig.terms` by
hand after editing a profile; `python -m ha_rig.terms --here` writes this repo's, which its write-terms pre-commit
hook runs before the check, so an unreadable profile refuses the commit.
"""

import ipaddress
import os
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from urllib.parse import urlparse

from .authorize import OLD_SENSOR_KEY, SENSOR_KEY
from .profile import ENV_FILE, Profile, colon_ieee, load_env, profile_paths
from .target import RIG_HOME, deviceside_repo, main_checkout

TERMS_FILE = ".private-terms.rig"  # gitignored in deviceside and here
HEADER = "# Written by ha-rig (python -m ha_rig.terms); edits are overwritten.\n"
# Hosts and the restricted sensor's name that the docs, examples and tests name on purpose.
GENERIC_TERMS = frozenset({"homeassistant.local", "localhost", "127.0.0.1", "binary_sensor.rig_restricted"})
SECRET_KEYS = ("HA_TOKEN", "HA_ADMIN_TOKEN", "HUE_APP_KEY")  # reported as "<KEY> from .env.rig"
# The restricted sensor, under its name and its old one: reported as itself, like a profile's entities.
ENTITY_KEYS = (SENSOR_KEY, OLD_SENSOR_KEY)


def private_terms(profiles: Iterable[Path] | None = None, env_file: Path = ENV_FILE) -> dict[str, str]:
    """Lowercased term -> label to report. `profiles` defaults to every profile."""
    terms: dict[str, str] = {}
    for path in profile_paths() if profiles is None else profiles:
        if path.exists():
            terms.update((entity_id.lower(), entity_id) for entity_id in profile_entities(Profile.load(path)))
    if env_file.exists():
        env = load_env(env_file)
        named = [urlparse(env.get("HA_URL", "")).hostname, hostname(env.get("HUE_BRIDGE_HOST", ""))]
        named += [env.get(key) for key in ENTITY_KEYS]
        terms.update((term.lower(), term) for term in filter(None, named) if term.lower() not in GENERIC_TERMS)
        terms.update((secret.lower(), f"{key} from .env.rig") for key in SECRET_KEYS if (secret := env.get(key)))
    return terms


def hostname(host: str) -> str | None:
    """The host in HUE_BRIDGE_HOST: a bare address (IPv6 too), `host:port`, or a URL."""
    if not host:
        return None
    try:
        return str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        return urlparse(host if "://" in host else f"//{host}").hostname


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
        # It holds the HA token: owner-only from the first byte, and set again on a file made before.
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
            file.write(text)
        path.chmod(0o600)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    here = "--here" in (sys.argv[1:] if argv is None else argv)
    try:
        path = write(RIG_HOME if here else deviceside_repo())
    except ValueError as err:
        print(f"Can't read the household's terms: {err}")
        return 1
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
