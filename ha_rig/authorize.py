"""Whether the rig may do a piece of work now: one decision, from what the work disturbs, not who asks.

Unrestricted work (a pre-authorized profile's run or recovery, a deploy, a probe declared not visual) goes at any
time. Restricted work, everything else, goes when the restricted sensor is off, or on an explicit approval:
DEVICE_SIDE_RIG_RESTRICTED=1, or a y typed at a terminal. The y/N prompt is otherwise a readiness check, asked only at
a terminal; an agent's call, with none, is never asked.
"""

from dataclasses import dataclass

APPROVAL = "DEVICE_SIDE_RIG_RESTRICTED"
SENSOR_KEY = "RIG_RESTRICTED_ENTITY"


@dataclass(frozen=True, slots=True)
class Decision:
    go: bool  # may go, once `prompt`, when there is one, is answered yes
    prompt: str | None  # the question to ask at a terminal; None: don't ask
    watch: bool  # arm the watcher on the restricted sensor
    reason: str  # printed in the plan, or as the refusal


def authorize(*, restricted: bool, sensor: tuple[bool, str] | None, approved: bool, tty: bool) -> Decision:
    """The decision for work that is `restricted` or not, given the restricted sensor's reading (rig_clear's answer:
    clear, and a reason naming its state; None when no sensor is configured, which restricted work reads as not
    clear), an explicit approval (DEVICE_SIDE_RIG_RESTRICTED=1) and whether a terminal can be asked."""
    readiness = "Proceed?" if tty else None
    if not restricted:
        return Decision(True, readiness, False, "Unrestricted.")
    watch = sensor is not None
    clear, why = sensor if sensor is not None else (False, f"no {SENSOR_KEY} in .env.rig")
    if clear:
        return Decision(True, readiness, watch, f"Restricted; {why}, so it may go.")
    if approved:
        return Decision(True, None, watch, f"Restricted, and {why}; approved by {APPROVAL}=1.")
    if tty:
        return Decision(True, f"Restricted: {why}. Proceed anyway?", watch, f"Restricted, and {why}.")
    return Decision(False, None, watch, f"Restricted, and {why}. Needs {APPROVAL}=1 (Jason's yes) or the sensor off.")
