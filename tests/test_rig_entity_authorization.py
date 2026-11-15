"""`pre_authorized: false` on one entity of a pre-authorized profile: the work that moves it is restricted, the rest of
the profile's stays unrestricted. A run, a recovery of what it left and a `--visual profile` probe each follow it."""

import pytest

from ha_rig import engine
from ha_rig.journal import Journal
from ha_rig.profile import Profile
from tests import rig_fakes as fakes
from tests.rig_fakes import write

from .test_rig import rig, rig_ha, target_suite

MIXED = (
    "pre_authorized: true\n"
    "shelly:\n"
    "  - entity_id: switch.plug\n    roles: [switch_plug]\n"
    "  - entity_id: switch.plug_b\n    roles: [switch_plug]\n    pre_authorized: false\n"
)


# --- The profile ---------------------------------------------------------------------------------------------


def test_an_entity_can_be_left_out_of_its_profiles_pre_authorization(tmp_path):
    profile = Profile.load(write(tmp_path, "lab.yaml", MIXED))
    assert profile.pre_authorized
    assert profile.restricted == {"switch.plug_b"}
    assert profile.restricts(["switch.plug"]) == []
    assert profile.restricts(["switch.plug", "switch.plug_b"]) == ["switch.plug_b"]


@pytest.mark.parametrize("value", ["true", "yes", "0"])
def test_an_entity_cant_be_pre_authorized_on_its_own(tmp_path, value):
    text = "pre_authorized: false\nshelly:\n  - entity_id: switch.plug\n    roles: [switch_plug]\n"
    with pytest.raises(ValueError, match="switch.plug pre_authorized must be false"):
        Profile.load(write(tmp_path, "house.yaml", f"{text}    pre_authorized: {value}\n"))


def test_false_on_an_entity_of_a_restricted_profile_only_restates_it(tmp_path):
    """Its every run is restricted anyway (the gate reads the profile's own key first); the entity's key is allowed, so
    a profile can be made restricted for a while without editing its entities."""
    text = MIXED.replace("pre_authorized: true\n", "pre_authorized: false\n", 1)
    profile = Profile.load(write(tmp_path, "house.yaml", text))
    assert not profile.pre_authorized
    assert profile.restricted == {"switch.plug_b"}


def test_a_supply_that_isnt_pre_authorized_restricts_what_it_powers(tmp_path):
    text = MIXED + "  - entity_id: light.lamp\n    roles: [light_onoff]\n    powered_by: switch.plug_b\n"
    profile = Profile.load(write(tmp_path, "lab.yaml", text))
    assert profile.restricts(["light.lamp"]) == ["switch.plug_b"]


def test_switching_a_supply_restricts_what_draws_power_through_it_and_isnt_pre_authorized(tmp_path):
    text = MIXED + "  - entity_id: light.lamp\n    roles: [light_onoff]\n    powered_by: switch.plug\n"
    profile = Profile.load(
        write(
            tmp_path,
            "lab.yaml",
            text.replace("    roles: [light_onoff]\n", "    roles: [light_onoff]\n    pre_authorized: false\n"),
        )
    )
    assert profile.restricts(["switch.plug"]) == ["light.lamp"]


@pytest.mark.parametrize("key", ["pre_authorised", "preauthorized", "restricted"])
def test_a_misspelt_key_on_an_entity_fails_the_load_rather_than_leave_it_pre_authorized(tmp_path, key):
    text = MIXED.replace("    pre_authorized: false\n", f"    {key}: false\n")
    with pytest.raises(ValueError, match=rf"switch.plug_b has unknown keys \['{key}'\]"):
        Profile.load(write(tmp_path, "lab.yaml", text))


def test_power_is_followed_through_a_chain_of_supplies_both_ways(tmp_path):
    """light.a draws power through switch.b, which draws it through switch.c: switching switch.c cuts light.a."""
    text = (
        "pre_authorized: true\nshelly:\n"
        "  - entity_id: switch.c\n    roles: [switch_plug]\n"
        "  - entity_id: switch.b\n    roles: [switch_plug]\n    powered_by: switch.c\n"
        "  - entity_id: light.a\n    roles: [light_onoff]\n    powered_by: switch.b\n    pre_authorized: false\n"
        "  - entity_id: light.d\n    roles: [light_wrapped]\n"
    )
    profile = Profile.load(write(tmp_path, "lab.yaml", text))
    assert profile.restricts(["switch.c"]) == ["light.a"]
    assert profile.restricts(["switch.b"]) == ["light.a"]
    assert profile.restricts(["light.d"]) == []


def test_a_left_out_entity_cant_be_paused_by_a_pre_authorized_profile(tmp_path):
    text = MIXED.replace("pre_authorized: true\n", "pre_authorized: true\npause: [switch.plug_b]\n", 1)
    with pytest.raises(ValueError, match="switch.plug_b is pre_authorized: false, so it can't be under pause:"):
        Profile.load(write(tmp_path, "lab.yaml", text))


# --- What the run leaves -------------------------------------------------------------------------------------


@pytest.mark.parametrize("restricted", [True, False])
async def test_a_restricted_runs_journal_says_so(tmp_path, monkeypatch, restricted):
    rig_ha(monkeypatch, light="on", opt=False, automation="on")
    journal = Journal(tmp_path / "deviceside-rig.journal.test.json")
    seen = []

    async def run(*args, **kwargs):
        seen.append(dict(Journal(journal.path).sections))
        return engine.RunResult([])

    monkeypatch.setattr(rig, "git", lambda *args, check=True: "c1short")
    monkeypatch.setattr(rig, "run", run)
    monkeypatch.setattr(rig, "write_report", lambda result, directory, title: tmp_path / "report.md")
    monkeypatch.setattr(rig, "write_records", lambda *args, **kwargs: _nothing())
    suites = target_suite(tmp_path)
    profile = fakes.profile("shelly")
    await rig.run_rig({}, suites, profile, fakes.OPTION_REGISTRY, None, True, "c1", journal, restricted=restricted)
    assert seen and all(sections["restricted"] is restricted for sections in seen)
    assert not journal.path.exists()  # cleared with the profile once nothing is left


async def _nothing():
    return [], []
