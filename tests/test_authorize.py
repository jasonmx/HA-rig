"""The authorization decision, row by row: unrestricted work, and restricted work by sensor, approval and terminal."""

import pytest

from ha_rig.authorize import Decision, authorize

OFF = (True, "binary_sensor.rig_restricted is off")
ON = (False, "binary_sensor.rig_restricted is on")
UNREAD = (False, "can't read binary_sensor.rig_restricted: refused")
REFUSED = "Needs DEVICE_SIDE_RIG_RESTRICTED=1 (Jason's yes) or the sensor off."


@pytest.mark.parametrize("sensor", [OFF, ON, UNREAD, None])
@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize(("tty", "prompt"), [(True, "Proceed?"), (False, None)])
def test_unrestricted_work_goes_whatever_the_sensor_asking_only_at_a_terminal(sensor, approved, tty, prompt):
    assert authorize(restricted=False, sensor=sensor, approved=approved, tty=tty) == Decision(
        True, prompt, False, "Unrestricted."
    )


@pytest.mark.parametrize(("tty", "prompt"), [(True, "Proceed?"), (False, None)])
@pytest.mark.parametrize("approved", [True, False])
def test_restricted_work_goes_while_the_sensor_is_off_and_is_watched(tty, prompt, approved):
    decision = authorize(restricted=True, sensor=OFF, approved=approved, tty=tty)
    assert decision == Decision(True, prompt, True, "Restricted; binary_sensor.rig_restricted is off, so it may go.")


@pytest.mark.parametrize("sensor", [ON, UNREAD])
@pytest.mark.parametrize("tty", [True, False])
def test_an_approval_lets_restricted_work_past_the_sensor_unasked_and_still_watched(sensor, tty):
    decision = authorize(restricted=True, sensor=sensor, approved=True, tty=tty)
    assert decision.go and decision.prompt is None and decision.watch
    assert decision.reason == f"Restricted, and {sensor[1]}; approved by DEVICE_SIDE_RIG_RESTRICTED=1."


@pytest.mark.parametrize("sensor", [ON, UNREAD])
def test_at_a_terminal_a_yes_is_the_approval(sensor):
    decision = authorize(restricted=True, sensor=sensor, approved=False, tty=True)
    question = f"Restricted: {sensor[1]}. Proceed anyway?"
    assert decision == Decision(True, question, True, f"Restricted, and {sensor[1]}.")


@pytest.mark.parametrize("sensor", [ON, UNREAD])
def test_an_agents_restricted_work_is_refused_past_the_sensor(sensor):
    decision = authorize(restricted=True, sensor=sensor, approved=False, tty=False)
    assert decision == Decision(False, None, True, f"Restricted, and {sensor[1]}. {REFUSED}")


@pytest.mark.parametrize(
    ("approved", "tty", "go", "prompt"),
    [
        (True, False, True, None),
        (True, True, True, None),
        (False, True, True, "Restricted: no RIG_RESTRICTED_ENTITY in .env.rig. Proceed anyway?"),
        (False, False, False, None),
    ],
)
def test_without_a_sensor_restricted_work_needs_the_approval_and_nothing_is_watched(approved, tty, go, prompt):
    decision = authorize(restricted=True, sensor=None, approved=approved, tty=tty)
    assert (decision.go, decision.prompt, decision.watch) == (go, prompt, False)
    assert "no RIG_RESTRICTED_ENTITY in .env.rig" in decision.reason
