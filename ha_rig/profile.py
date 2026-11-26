"""The rig's profiles and connection settings. Shared by scripts/rig.py, the rig engine and the household check.

A profile (`profiles/<name>.yaml`, gitignored) is one test setup on the HA instance .env.rig connects to: the entities
the rig may move, by integration and role, the automations and switches it pauses while it runs, the bulbs it roams
between Zigbee networks (ha_rig/roam.py), and whether its unattended runs skip .env.rig's blocker sensor.

Pure file reading, no HA or pytest imports, so the runner's preflight can load it before anything mutates.
"""

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .target import RIG_HOME, main_checkout

RIG_DIR = Path(__file__).parent
MAIN_CHECKOUT = main_checkout(RIG_HOME)  # holds the gitignored profiles and .env.rig for every linked worktree
PROFILES_DIR = MAIN_CHECKOUT / "profiles"
EXAMPLE = RIG_DIR / "profile.example.yaml"  # the tracked template a profile is copied from
ENV_FILE = MAIN_CHECKOUT / ".env.rig"

PAUSABLE = ("automation", "switch")

# A native group's roles: `group_color` is one whose members all take color.
GROUP_ROLES = frozenset({"group", "group_color"})
ZHA_GROUP_MIN = 2  # ZHA makes no light entity for a group with fewer members
# The keys a listed entity takes; any other (a typo of pre_authorized, say) fails the load rather than be ignored.
ENTITY_KEYS = frozenset({"entity_id", "roles", "powered_by", "members", "pre_authorized", "class"})
# The roles whose cases run once per entity the profile lists with them, so each device gets its own test (several
# plugs, one per maker string). Every other role tests its first entity: a roaming bulb is listed beside a network's
# fixed lights (Profile.add_roaming), so light roles often hold several, and only the first is meant.
EACH_ENTITY = frozenset({"switch_plug"})
# A Home Assistant scene a case's ha_scene setup step makes from its own lights (scene.create), under a fixed ID per
# integration (ha_scene_entity): no profile lists it.
HA_SCENE = "ha_scene"
ROLES = frozenset(
    {"light_onoff", "light_wrapped", "light_dimmable", "light_ct", "light_color", "switch_plug", "scene", HA_SCENE}
    | GROUP_ROLES
)

# Seconds either side of a duration within which a device may flip. Zigbee and Matter hop through a mesh.
TOLERANCE_S = {"shelly": 2.0, "hue": 2.0, "zigbee2mqtt": 3.0, "zha": 3.0, "matter": 3.0}

# Seconds after a move ends, or a stop, that an integration's adapters still predict a light's brightness rather
# than read it (Hue: API_SETTLE_SECONDS; Zigbee2MQTT: fade.SETTLE_SECONDS). A case waits them out on its lights, so
# it starts from what HA shows.
SETTLE_S = {"hue": 15.0, "zigbee2mqtt": 2.0}


@dataclass(frozen=True, slots=True)
class EntryReload:
    """Actions that make core reload the device's own config entry some time after they write to it: its entities
    go unavailable and come back, and a call landing meanwhile fails."""

    actions: frozenset[str]
    deadline_s: float  # from the write, by when the entities must have gone unavailable and come back


# Shelly: a SetConfig makes the device report config_changed, and core reloads the entry ENTRY_RELOAD_COOLDOWN (60 s)
# after the first one (a Debouncer with immediate=False: later writes in the window coalesce, and don't push it back).
ENTRY_RELOADS = {
    "shelly": EntryReload(
        frozenset({"set_auto_off", "set_auto_on", "clear_auto_off", "clear_auto_on"}), deadline_s=100.0
    ),
}

# The order in which roaming bulbs visit the networks a run needs, moving directly from one to the next, after their
# home, where a run starts and ends.
CYCLE = ("hue", "zha", "zigbee2mqtt")
# The homes a roaming bulb may have: roam.py saves and puts back a bulb's rooms and scenes on Hue only.
HOMES = ("hue",)
# The key of a profile's `pause:` list that every run pauses, whatever integrations it holds.
PAUSE_ALL = "all"


def normalize_ieee(ieee: str) -> str:
    """An IEEE address as bare lowercase hex: Hue writes `00:17:88:...`, Z2M `0x0017...`, ZHA `00:17:...`."""
    return ieee.lower().removeprefix("0x").replace(":", "")


def colon_ieee(ieee: str) -> str:
    """ZHA's and the Hue bridge's spelling: `00:17:88:...`."""
    bare = normalize_ieee(ieee)
    return ":".join(bare[i : i + 2] for i in range(0, 16, 2))


def hex_ieee(ieee: str) -> str:
    """Zigbee2MQTT's spelling: `0x0017...`."""
    return f"0x{normalize_ieee(ieee)}"


@dataclass(frozen=True, slots=True)
class RigEntity:
    entity_id: str
    roles: frozenset[str]
    powered_by: str | None = None  # the profile's switch this entity draws power through
    members: tuple[str, ...] = ()  # a group's member lights, for a group light without an entity_id attribute
    # False: work that moves this entity is restricted, though its profile is pre-authorized (`pre_authorized: false`
    # on the entity; Profile.restricts).
    pre_authorized: bool = True
    # The DeviceSide class its device is in (`class:`), for a single device: its cases that call an action the
    # checkout doesn't build on that class skip as not applying (engine.unbuilt), and a response naming another class
    # fails the case.
    declared_class: str | None = None


@dataclass(frozen=True, slots=True)
class RoamBulb:
    """A bulb the rig moves round CYCLE, renamed to `entity_id` on every network it joins."""

    ieee: str  # as bare lowercase hex (normalize_ieee); finds the bulb on every network
    entity_id: str
    roles: frozenset[str]
    z2m_options: dict = field(default_factory=dict)  # set again after each Z2M join: Z2M's remove drops them
    home: str = "hue"  # the network a run starts it from and ends it on


# What a rig-made group, zone or scene is called on its network, before HA's entity is renamed to its fixed ID.
RIG_PREFIX = "ds-rig-"
HUE_NAME_MAX = 32  # the longest name the Hue bridge takes for a zone or scene


@dataclass(frozen=True, slots=True)
class RoamGroup:
    """A native group of roam bulbs the rig makes on every network it visits: a Z2M group, a ZHA group or a Hue
    zone, renamed to `entity_id`; on Hue, with a scene in the zone renamed to `scene`, which takes the scene role."""

    entity_id: str
    roles: frozenset[str]
    members: tuple[str, ...]  # bulb entity IDs
    scene: str | None = None

    def made_on(self, network: str) -> bool:
        """Whether the rig makes this group on `network`: not a one-member group on ZHA, which gives a group a light
        entity only from two members, so the role has no entity there and its cases skip."""
        return network != "zha" or len(self.members) >= ZHA_GROUP_MIN


def rig_name(entity_id: str) -> str:
    """The native name of a rig-made group or scene with this fixed entity ID: `ds-rig-` and its object ID."""
    return RIG_PREFIX + entity_id.partition(".")[2].replace("_", "-")


def ha_scene_id(integration: str) -> str:
    """The `scene_id` scene.create makes the integration's ha_scene under: one per integration, so lanes never share
    one."""
    return f"ds_rig_ha_{integration}"


def ha_scene_entity(integration: str) -> str:
    """The entity ID of the integration's ha_scene (ha_scene_id)."""
    return f"scene.{ha_scene_id(integration)}"


def is_ha_scene(entity_id: str) -> bool:
    """Whether the entity ID is an integration's ha_scene (ha_scene_entity), which HA has only once a case made it."""
    return entity_id.startswith(ha_scene_entity(""))


@dataclass(slots=True)
class Profile:
    integrations: dict[str, list[RigEntity]] = field(default_factory=dict)
    # Automations and switches turned off while the rig runs, whatever would fight the tests on these entities: by
    # integration key, the run holding it pausing its list, and under PAUSE_ALL, those every run pauses.
    pause: dict[str, tuple[str, ...]] = field(default_factory=dict)
    name: str = ""
    roam: tuple[RoamBulb, ...] = ()
    roam_groups: tuple[RoamGroup, ...] = ()
    roam_networks: tuple[str, ...] = ()  # where the roaming bulbs may go, in CYCLE order, their home included
    # Whether this profile's work is unrestricted (ha_rig/authorize.py): its whole run, pauses, DeviceSide reloads and
    # disables, roaming and HA restarts included, its recovery and its --visual profile probes go whatever
    # RIG_RESTRICTED_ENTITY reads, for a setup whose lights nobody minds seeing move. Off, they are restricted.
    pre_authorized: bool = False
    # The fixed entities --ignore-unreachables found unavailable before the run: their cases skip as unreachable, and
    # while any is, so do those whose devices the profile can't name (unlisted group members, fixed scenes).
    offline: frozenset[str] = frozenset()

    @classmethod
    def load(cls, path: Path) -> Profile:
        raw = read_mapping(path)
        profile = cls(pause=pause_section(path, raw.pop("pause", None) or []), name=path.stem)
        profile.pre_authorized = pre_authorized_key(path, raw.pop("pre_authorized", False))
        profile.roam, profile.roam_groups, profile.roam_networks = roam_section(path, raw.pop("roam", None) or {})
        for integration, entities in raw.items():
            if integration not in TOLERANCE_S:
                raise ValueError(f"{path.name}: unknown integration {integration!r}; use {', '.join(TOLERANCE_S)}")
            profile.integrations[integration] = []
            for item in entities or []:
                entity = fixed_entity(path, integration, item)
                if any(e.entity_id == entity.entity_id for e in profile.integrations[integration]):
                    raise ValueError(f"{path.name}: {entity.entity_id} is listed twice under {integration}")
                profile.integrations[integration].append(entity)
        listed = {entity.entity_id for entities in profile.integrations.values() for entity in entities}
        profile.add_roaming(path, listed)
        for entity_id, supply in profile.power().items():
            if supply not in listed:
                raise ValueError(f"{path.name}: {entity_id} is powered_by {supply}, which the profile doesn't list")
        paused = profile.restricts(dict.fromkeys(e for key in profile.pause for e in profile.pause[key]))
        if paused and profile.pre_authorized:
            raise ValueError(
                f"{path.name}: pause: reaches {', '.join(paused)}, which is pre_authorized: false (listed there, or "
                "drawing power through what is): every run of its key turns pause: off and on, restricted or not"
            )
        return profile

    def add_roaming(self, path: Path, listed: set[str]) -> None:
        """List each roaming bulb and rig-made group under every network the profile lets it visit, and each rig-made
        scene under Hue: while the bulbs are there, they are that network's entities. On any other network their
        roles find no entity, so those cases skip."""
        if both := sorted(listed & self.made_entities):
            raise ValueError(f"{path.name}: {', '.join(both)} roams, so it can't also be listed under an integration")
        made = {role for group in self.roam_groups for role in group.roles}
        made |= {"scene"} if any(group.scene for group in self.roam_groups) else set()
        for network in self.roam_networks:
            # The fixed entity would win (entity() takes the first), leaving the rig-made one unused.
            if taken := sorted(made & {role for e in self.integrations.get(network, ()) for role in e.roles}):
                raise ValueError(f"{path.name}: {network} lists a fixed {', '.join(taken)}, which roam.groups makes")
        for network in self.roam_networks if self.roam else ():
            entities = self.integrations.setdefault(network, [])
            entities.extend(RigEntity(b.entity_id, b.roles) for b in self.roam)
            entities.extend(
                RigEntity(g.entity_id, g.roles, members=g.members) for g in self.roam_groups if g.made_on(network)
            )
            if network == "hue":
                entities.extend(RigEntity(g.scene, frozenset({"scene"})) for g in self.roam_groups if g.scene)

    @property
    def roam_home(self) -> str | None:
        """The roaming bulbs' home, which they share (roam_section), or None with no roaming bulbs."""
        return self.roam[0].home if self.roam else None

    def paused_by(self, integrations: Iterable[str] | None = None) -> tuple[str, ...]:
        """What a run holding `integrations` (None: all of them) pauses: the PAUSE_ALL list, then each one's."""
        keys = [PAUSE_ALL, *(sorted(self.pause) if integrations is None else sorted(integrations))]
        return tuple(dict.fromkeys(entity for key in keys for entity in self.pause.get(key, ())))

    @property
    def made_entities(self) -> frozenset[str]:
        """The roaming bulbs' lights and the rig-made groups and scenes: entities HA has under these IDs only once the
        run has moved or made them."""
        bulbs = {bulb.entity_id for bulb in self.roam}
        return frozenset(bulbs | {e for group in self.roam_groups for e in (group.entity_id, group.scene) if e})

    def made_by_run(self, entity_id: str) -> bool:
        """Whether HA has the entity only once the run has made or moved it: a made_entities one, or a case's ha_scene,
        so no check looks it up beforehand."""
        return entity_id in self.made_entities or is_ha_scene(entity_id)

    def entity(self, integration: str, role: str) -> str | None:
        """The first entity with the role: the one a case of any role but EACH_ENTITY's targets."""
        return next(iter(self.entities(integration, role)), None)

    def entities(self, integration: str, role: str) -> tuple[str, ...]:
        """Every entity with the role on the integration, in profile order."""
        return tuple(e.entity_id for e in self.integrations.get(integration, ()) if role in e.roles)

    def declared_class(self, integration: str, entity_id: str) -> str | None:
        """The DeviceSide class the profile declares for the entity (`class:`), or None."""
        return next(
            (e.declared_class for e in self.integrations.get(integration, ()) if e.entity_id == entity_id), None
        )

    def members(self, integration: str, entity_id: str) -> tuple[str, ...]:
        return next((e.members for e in self.integrations.get(integration, ()) if e.entity_id == entity_id), ())

    @property
    def restricted(self) -> frozenset[str]:
        """The entities listed with `pre_authorized: false`: moving one is restricted work, whatever the profile's."""
        return frozenset(e.entity_id for es in self.integrations.values() for e in es if not e.pre_authorized)

    def restricts(self, entity_ids: Iterable[str]) -> list[str]:
        """The entities listed `pre_authorized: false` that moving `entity_ids` reaches, in order: each one and
        everything joined to it by power, through any chain of `powered_by` either way. A case needs its supplies on
        and may switch them (the engine follows a chain of them, CaseSnapshot.take), and a supply switched on or off
        moves everything drawing power through it."""
        restricted, linked = self.restricted, self.power_links()
        touched: dict[str, None] = {}
        stack = list(entity_ids)
        while stack:
            if (entity_id := stack.pop(0)) not in touched:
                touched[entity_id] = None
                stack += sorted(linked.get(entity_id, ()))
        return [e for e in touched if e in restricted]

    def power_links(self) -> dict[str, set[str]]:
        """Each entity's supply and what draws power through it, both ways."""
        links: dict[str, set[str]] = {}
        for entity_id, supply in self.power().items():
            links.setdefault(entity_id, set()).add(supply)
            links.setdefault(supply, set()).add(entity_id)
        return links

    def restricted_on(self, integration: str) -> list[str]:
        """The integration's entities listed `pre_authorized: false`, in profile order."""
        return [e.entity_id for e in self.integrations.get(integration, ()) if not e.pre_authorized]

    def power(self) -> dict[str, str]:
        """Each entity that draws power through another, mapped to its supply, across every integration."""
        return {e.entity_id: e.powered_by for entities in self.integrations.values() for e in entities if e.powered_by}


def roam_section(path: Path, raw: object) -> tuple[tuple[RoamBulb, ...], tuple[RoamGroup, ...], tuple[str, ...]]:
    """A profile's `roam: {networks: [...], bulbs: [...], groups: [...]}`: the networks the bulbs may visit, each
    bulb's home among them and shared by all, as they move together; each IEEE and entity ID used once, and each
    group role on one group, since a run uses only the first."""
    if not raw:
        return (), (), ()
    if not isinstance(raw, dict) or set(raw) - {"networks", "bulbs", "groups"}:
        raise ValueError(
            f"{path.name}: roam must be a mapping with a networks list, a bulbs list and an optional groups list"
        )
    bulbs = tuple(roam_bulb(path, item) for item in raw.get("bulbs") or [])
    networks = roam_networks(path, raw.get("networks"), bulbs)
    if len({bulb.ieee for bulb in bulbs}) != len(bulbs):
        raise ValueError(f"{path.name}: two roam bulbs share an IEEE")
    groups = tuple(roam_group(path, item, bulbs) for item in raw.get("groups") or [])
    ids = [bulb.entity_id for bulb in bulbs]
    ids += [entity for group in groups for entity in (group.entity_id, group.scene) if entity]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path.name}: two roam bulbs, groups or scenes share an entity ID")
    for role in sorted(GROUP_ROLES):
        if len([group for group in groups if role in group.roles]) > 1:
            raise ValueError(f"{path.name}: two roam groups have the {role} role; a run uses only the first")
    return bulbs, groups, networks


def roam_networks(path: Path, raw: object, bulbs: tuple[RoamBulb, ...]) -> tuple[str, ...]:
    """`roam: networks:`, in CYCLE order: where the bulbs may go, their one home among them."""
    if not isinstance(raw, list) or not raw or set(map(str, raw)) - set(CYCLE):
        raise ValueError(f"{path.name}: roam needs networks, a list of where its bulbs may go: {', '.join(CYCLE)}")
    networks = tuple(network for network in CYCLE if network in raw)
    for bulb in bulbs:
        if bulb.home not in networks:
            raise ValueError(f"{path.name}: roam bulb {bulb.entity_id}'s home, {bulb.home}, isn't in roam networks")
    if len({bulb.home for bulb in bulbs}) > 1:
        raise ValueError(f"{path.name}: the roam bulbs move together, so they need one home")
    return networks


def roam_bulb(path: Path, item: object) -> RoamBulb:
    """One roam bulb: its IEEE, fixed light entity ID, home, roles and Z2M options."""
    if not isinstance(item, dict) or not {"ieee", "entity_id", "home"} <= set(item):
        raise ValueError(f"{path.name}: each roam bulb needs an ieee, an entity_id and a home")
    entity_id, ieee, home = str(item["entity_id"]), normalize_ieee(str(item["ieee"])), str(item["home"])
    if unknown := set(item) - {"ieee", "entity_id", "home", "roles", "z2m_options"}:
        raise ValueError(f"{path.name}: roam bulb {entity_id} has unknown keys {sorted(unknown)}")
    if not entity_id.startswith("light."):
        raise ValueError(f"{path.name}: roam bulb {entity_id} must be a light")
    if len(ieee) != 16 or set(ieee) - set("0123456789abcdef"):
        raise ValueError(f"{path.name}: roam bulb {entity_id} has an IEEE that isn't 16 hex digits")
    roles = frozenset(item.get("roles", ()))
    if unknown := roles - ROLES:
        raise ValueError(f"{path.name}: {entity_id} has unknown roles {sorted(unknown)}")
    if roles & GROUP_ROLES:
        raise ValueError(f"{path.name}: roam bulb {entity_id} is a bulb, not a group")
    if HA_SCENE in roles:
        raise ValueError(f"{path.name}: {entity_id} has the {HA_SCENE} role, which is made by the {HA_SCENE} step")
    if not isinstance(options := item.get("z2m_options") or {}, dict):
        raise ValueError(f"{path.name}: roam bulb {entity_id} z2m_options must be a mapping")
    if home not in HOMES:
        raise ValueError(
            f"{path.name}: roam bulb {entity_id} has home {home}; only {', '.join(HOMES)} homes are supported so far, "
            "as Hue is where the rig saves and puts back a bulb's rooms and scenes"
        )
    return RoamBulb(ieee, entity_id, roles, options, home)


def roam_group(path: Path, item: object, bulbs: tuple[RoamBulb, ...]) -> RoamGroup:
    """One rig-made group: its fixed light entity ID, group roles, members (`all` or bulb entity IDs) and an optional
    scene's fixed entity ID."""
    if not isinstance(item, dict) or not {"entity_id", "members"} <= set(item):
        raise ValueError(f"{path.name}: each roam group needs an entity_id and members")
    entity_id = str(item["entity_id"])
    if unknown := set(item) - {"entity_id", "roles", "members", "scene"}:
        raise ValueError(f"{path.name}: roam group {entity_id} has unknown keys {sorted(unknown)}")
    if not entity_id.startswith("light."):
        raise ValueError(f"{path.name}: roam group {entity_id} must be a light")
    roles = frozenset(item.get("roles", ()))
    if not roles or roles - GROUP_ROLES:
        only = ", ".join(sorted(GROUP_ROLES))
        raise ValueError(f"{path.name}: roam group {entity_id} takes group roles only: {only}")
    members = group_members(path, entity_id, roles, item["members"], bulbs)
    scene = item.get("scene")
    if scene is not None and (not isinstance(scene, str) or not scene.startswith("scene.")):
        raise ValueError(f"{path.name}: roam group {entity_id} scene must be a scene entity ID")
    if scene is not None and "group" not in roles:
        raise ValueError(f"{path.name}: roam group {entity_id} has a scene, so it needs the group role")
    for fixed in filter(None, (entity_id, scene)):
        if len(rig_name(fixed)) > HUE_NAME_MAX:
            raise ValueError(f"{path.name}: {fixed} is too long: Hue would name it {rig_name(fixed)}")
    return RoamGroup(entity_id, roles, members, scene)


def group_members(
    path: Path, entity_id: str, roles: frozenset[str], raw: object, bulbs: tuple[RoamBulb, ...]
) -> tuple[str, ...]:
    """A roam group's members: `all`, or a list of roam bulbs; for group_color, each one takes color."""
    by_id = {bulb.entity_id: bulb for bulb in bulbs}
    members = list(by_id) if raw == "all" else raw
    if not isinstance(members, list) or not members or not all(isinstance(member, str) for member in members):
        raise ValueError(f"{path.name}: roam group {entity_id} members must be all or a list of roam bulbs")
    if strangers := [member for member in members if member not in by_id]:
        raise ValueError(f"{path.name}: roam group {entity_id} lists {', '.join(strangers)}, not a roam bulb")
    if "group_color" in roles and (white := [m for m in members if "light_color" not in by_id[m].roles]):
        raise ValueError(f"{path.name}: {entity_id} is a group_color, but {', '.join(white)} has no light_color role")
    return tuple(dict.fromkeys(members))


def pause_section(path: Path, raw: object) -> dict[str, tuple[str, ...]]:
    """A profile's `pause:`: a list, which every run pauses, or a mapping of lists by PAUSE_ALL or integration key."""
    if isinstance(raw, list):
        raw = {PAUSE_ALL: raw}
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: pause must be a list of entity IDs, or a mapping of such lists")
    if unknown := sorted(map(str, set(raw) - {PAUSE_ALL, *TOLERANCE_S})):
        raise ValueError(f"{path.name}: pause has unknown keys {unknown}; use {PAUSE_ALL} or {', '.join(TOLERANCE_S)}")
    return {key: pause_list(path, entity_ids or []) for key, entity_ids in raw.items()}


def pause_list(path: Path, entity_ids: object) -> tuple[str, ...]:
    """One of `pause:`'s lists of automations and switches."""
    if not isinstance(entity_ids, list) or not all(isinstance(entity_id, str) for entity_id in entity_ids):
        raise ValueError(f"{path.name}: pause must be a list of entity IDs, or a mapping of such lists")
    if other := [entity_id for entity_id in entity_ids if entity_id.partition(".")[0] not in PAUSABLE]:
        raise ValueError(f"{path.name}: pause takes automations and switches only, got {', '.join(other)}")
    return tuple(entity_ids)


def fixed_entity(path: Path, integration: str, item: object) -> RigEntity:
    """One entity listed under an integration: its ID, roles, supply, members, `pre_authorized` and class, and no other
    key."""
    if not isinstance(item, dict) or "entity_id" not in item:
        raise ValueError(f"{path.name}: each {integration} entity needs an entity_id")
    entity_id = item["entity_id"]
    if unknown := set(item) - ENTITY_KEYS:
        raise ValueError(f"{path.name}: {entity_id} has unknown keys {sorted(unknown)}")
    roles = frozenset(item.get("roles", ()))
    if unknown := roles - ROLES:
        raise ValueError(f"{path.name}: {entity_id} has unknown roles {sorted(unknown)}")
    if HA_SCENE in roles:
        raise ValueError(f"{path.name}: {entity_id} has the {HA_SCENE} role, which is made by the {HA_SCENE} step")
    members = item.get("members", [])
    if not isinstance(members, list) or not all(isinstance(member, str) for member in members):
        raise ValueError(f"{path.name}: {entity_id} members must be a list of entity IDs")
    if members and not roles & GROUP_ROLES:
        raise ValueError(f"{path.name}: {entity_id} lists members but has no group role")
    declared = item.get("class")
    if declared is not None and (not isinstance(declared, str) or not declared):
        raise ValueError(f"{path.name}: {entity_id} class must be a DeviceSide class name")
    if declared is not None and roles & GROUP_ROLES:
        raise ValueError(f"{path.name}: {entity_id} is a group, whose members each have their own class")
    return RigEntity(
        entity_id, roles, item.get("powered_by"), tuple(members), entity_pre_authorized(path, item), declared
    )


def entity_pre_authorized(path: Path, item: dict) -> bool:
    """A listed entity's `pre_authorized:`, which only narrows: `false` makes moving it restricted work in a
    pre-authorized profile (and restates it in a restricted one). Left out, the entity is as its profile is; it can't
    be pre-authorized on its own, as that is the profile's to say."""
    if "pre_authorized" not in item:
        return True
    if item["pre_authorized"] is not False:
        raise ValueError(
            f"{path.name}: {item['entity_id']} pre_authorized must be false, got {item['pre_authorized']!r}; "
            "only the profile's own pre_authorized makes work unrestricted"
        )
    return False


def pre_authorized_key(path: Path, value: object) -> bool:
    """The profile's `pre_authorized:`, true or false."""
    if not isinstance(value, bool):
        raise ValueError(f"{path.name}: pre_authorized must be true or false, got {value!r}")
    return value


def read_mapping(path: Path) -> dict:
    """A profile file's top-level mapping; an error names the file, so a typo in one doesn't read as a traceback."""
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as err:
        raise ValueError(f"{path.name}: not valid YAML: {err}") from err
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: must be a mapping of pause and integration keys")
    return raw


def profile_paths(directory: Path = PROFILES_DIR) -> list[Path]:
    """Every profile in `directory`, by name."""
    return sorted(directory.glob("*.yaml"))


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    """KEY=value lines from .env.rig, overridden by the process environment."""
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if (line := line.strip()) and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip("\"'")
    for key in (
        "HA_URL",
        "HA_TOKEN",
        "HA_ADMIN_TOKEN",
        "RIG_SYNC_CMD",
        "RIG_STATUS_CMD",
        "RIG_RESTART_CMD",
        "RIG_RESTRICTED_ENTITY",
        "RIG_BLOCKER_ENTITY",  # its old name, still read
        "RIG_PAUSE",  # moved into the profile; still read, so a run set it in refuses rather than ignores it
        "HUE_BRIDGE_HOST",
        "HUE_APP_KEY",
    ):
        if key in os.environ:
            env[key] = os.environ[key]
    return env
