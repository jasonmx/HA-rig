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


def test_in_a_restricted_profile_every_entity_is_restricted(tmp_path):
    text = MIXED.replace("pre_authorized: true\n", "pre_authorized: false\n", 1)
    profile = Profile.load(write(tmp_path, "house.yaml", text))
    assert profile.restricts(["switch.plug"]) == ["switch.plug"]


def test_a_supply_that_isnt_pre_authorized_restricts_what_it_powers(tmp_path):
    text = MIXED + "  - entity_id: light.lamp\n    roles: [light_onoff]\n    powered_by: switch.plug_b\n"
    profile = Profile.load(write(tmp_path, "lab.yaml", text))
    assert profile.restricts(["light.lamp"]) == ["switch.plug_b"]


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
    assert seen and all(("restricted" in sections) is restricted for sections in seen)
    assert all(sections.get("restricted", True) is True for sections in seen)


async def _nothing():
    return [], []
