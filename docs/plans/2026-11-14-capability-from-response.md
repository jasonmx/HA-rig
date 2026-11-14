# The Capability column reads the class DeviceSide reports

Follows #84, which filled a record's Capability column by classing a stand-in Target the rig built from HA's
registries. DeviceSide's device-classes step 4 (`claude/device-classes-4-core`, and step 5 on
`claude/device-classes-5-z2m`) removes `Adapter.capability()`, which that stand-in fell back to, and reports instead
the class each call reached in its response (`EntityResult.class_keys()`, `core/models.py`). The rig reads that.

## Coordination

Intent: a record's Capability cell is the class DeviceSide's response reports for the test's target, never one the
rig works out.
Touches: ha_rig/engine.py, tests/test_rig_engine.py, tests/rig_fakes.py
Pairs with DeviceSide's device-classes branches (step 4 onward); no Tests-with, as CI must pass against DeviceSide
`main`, which has no `IntegrationInfo.capability` yet.

## Parts

1. **Which integrations name classes:** those whose `IntegrationInfo` has a `capability` that is not None (read with
   `getattr`, so an older checkout doesn't crash), as DeviceSide's `docs/evidence.md` (Sign-off format) now says.
   The adapter fallback goes. Every other integration keeps three columns; against DeviceSide `main` that is Shelly
   too.
2. **Calls:** every DeviceSide call a test step sends (`call`) already asks for the response; nothing changes there.
3. **The cell**, from each call of the tested action on the case's own target (the role's entity), outside
   setup, that returned a response (another action's call can reach other members of a group): the entry
   under that entity, else, for a group, its members' entries present in the response (an HA group DeviceSide
   expands, or a native group that fell back to its members, the refused member absent). `capability` (a string) is
   one class, `capabilities` (a list) a native group's members' classes. The cell is the sorted union over every
   entry and call; a dash when any entry has neither key, no entry is found, no call returned a response, or the case
   didn't pass (only a pass credits a class, as before). A test that expects the group's own entry fails on its
   absence through `expect_response`, as it does today.
4. **The stand-in Target, its registry and state lookups and its tests go.** DeviceSide's next step can then drop the
   `hass=None` requirement from `IntegrationInfo.capability` (its comment, `docs/design.md`, the pinning test in
   `tests/core/test_registry.py`).
5. **Tests**, on canned responses: a command entry with `capability`, a native group's `capabilities`, a
   `get_attributes` entry with `capability` in its data, an entry with neither key, an HA group answering per member,
   two calls that disagree, a skipped test, a group that fell back to its members.
