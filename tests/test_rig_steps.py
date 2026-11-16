"""deviceside's step files parse, and a broken one fails at load naming the file, the test and the step."""

from pathlib import Path

import pytest
from custom_components.deviceside.core.registry import AdapterRegistry

from ha_rig.engine import Expr, Observe, SchemaError, check_owned, load_step_file, load_step_files
from ha_rig.target import deviceside_repo, integration_steps_dirs, steps_dir
from tests.rig_fakes import STEPS

# deviceside's step files, the only ones a rig test reads, and only to check they parse: what they test is deviceside's.
SHIPPED = steps_dir(deviceside_repo())
SHIPPED_OWN = integration_steps_dirs(deviceside_repo())  # each integration's own


@pytest.mark.parametrize(
    "path",
    sorted(SHIPPED.glob("*.yaml")) + sorted(path for d in SHIPPED_OWN.values() for path in d.glob("*.yaml")),
    ids=lambda path: path.relative_to(deviceside_repo()).as_posix(),
)
def test_every_shipped_step_file_parses(path):
    assert load_step_file(path).tests


def test_every_step_file_has_an_action():
    """An integration's own files merge (no collision, no `only:` naming another) and have its adapter."""
    step_files = load_step_files(SHIPPED, SHIPPED_OWN)
    registry = AdapterRegistry.discover()
    assert step_files
    assert {step_file.action for step_file in step_files} <= set(registry.actions)
    check_owned(step_files, registry)


def test_every_only_names_an_integration():
    """A misspelled key would silently drop the test, or its observed run, from every suite."""
    known = set(AdapterRegistry.discover().integrations)
    for step_file in load_step_files(SHIPPED, SHIPPED_OWN):
        for test in step_file.tests:
            named = (test.only or frozenset()) | ((test.observe and test.observe.only) or frozenset())
            named |= {key for step in (*test.setup, *test.steps) for key in step.only or ()}
            assert named <= known, f"{step_file.action} {test.name}: {sorted(named - known)}"


OWN = """
action: turn_on_for
rig_seconds: 7
tests:
  - name: own_case
    roles: [switch_plug]
    steps:
      - call: {duration: 1}
"""


def own_dirs(tmp_path: Path, text: str = OWN, integration: str = "shelly", name: str = "turn_on_for") -> dict:
    """An integration's own step file `name`.yaml holding `text`; the integration_dirs that find it."""
    (directory := tmp_path / integration / "rig").mkdir(parents=True)
    (directory / f"{name}.yaml").write_text(text)
    return {integration: directory}


def test_an_integrations_own_tests_join_the_actions_suite_on_it_alone(tmp_path):
    files = load_step_files(STEPS, own_dirs(tmp_path))
    merged = next(f for f in files if f.action == "turn_on_for" and not f.options)
    shared = next(f for f in load_step_files(STEPS) if f.action == "turn_on_for")
    assert [t.name for t in merged.tests] == [t.name for t in shared.tests] + ["own_case"]
    assert merged.tests[-1].only == frozenset({"shelly"}) and merged.owned_by == frozenset({"shelly"})
    assert merged.rig_seconds == shared.rig_seconds + 7 and merged.path == shared.path  # one suite, one record
    assert merged.tests[-1].runs_on("shelly") and not merged.tests[-1].runs_on("hue")
    assert len(files) == len(load_step_files(STEPS))


def test_an_integrations_own_file_with_no_shared_one_stands_alone(tmp_path):
    files = load_step_files(STEPS, own_dirs(tmp_path, OWN.replace("turn_on_for", "clear_auto_off"), name="x"))
    assert [(f.action, f.owned_by) for f in files if f.action == "clear_auto_off"] == [
        ("clear_auto_off", frozenset({"shelly"}))
    ]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (OWN.replace("own_case", "on_then_off"), "shelly/rig/turn_on_for.yaml: test on_then_off is already in"),
        (OWN.replace("    roles:", "    only: [hue]\n    roles:"), "own_case names hue, but the file is shelly's own"),
        (OWN.replace("      - call: {duration: 1}", "      - {wait: 1, only: [zha]}"), "own_case names zha"),
        (
            OWN + "      - {wait: 1, except_classes: {zha: [x]}}\n",
            "own_case names zha",
        ),
        ("options: [x]\n" + OWN, "options and name are for shared step files"),
    ],
    ids=["collision", "only_another", "step_only_another", "classes_another", "options"],
)
def test_an_integrations_own_file_refuses(tmp_path, text, message):
    with pytest.raises(SchemaError, match=message):
        load_step_files(STEPS, own_dirs(tmp_path, text))


def test_an_integrations_own_file_needs_its_adapter(tmp_path):
    registry = AdapterRegistry.discover()
    assert registry.adapter("set_scene", "shelly") is None
    files = load_step_files(STEPS, own_dirs(tmp_path, OWN.replace("turn_on_for", "set_scene").replace("own_case", "x")))
    with pytest.raises(SchemaError, match="shelly has no set_scene adapter"):
        check_owned(files, registry)
    check_owned(load_step_files(STEPS, own_dirs(tmp_path / "ok")), registry)


HEADER = "action: turn_on_for\nrig_seconds: 5\ntests:\n  - name: broken\n    roles: [switch_plug]\n    steps:\n"


def observed(observe: str = "{see: s, watch: w, ask: a}", setup: str = "[{wait: 0}]") -> str:
    """HEADER for an observed test, its steps to follow."""
    return HEADER.replace("    steps:\n", f"    setup: {setup}\n    observe: {observe}\n    steps:\n")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("- 1\n", "expected a mapping"),
        ("action: turn_on_for\nrig_seconds: 5\ntests: []\n", "tests must be a non-empty list"),
        ("action: turn_on_for\nrig_seconds: 0\ntests: [x]\n", "rig_seconds must be a positive whole number"),
        ("action: turn_on_for\nrig_seconds: 5\ntests: [x]\nextra: 1\n", "unknown key"),
        ("rig_seconds: 5\ntests: [x]\n", "action must name"),
        ("action: x\nrig_seconds: 5\ntests: [{roles: [switch_plug]}]\n", "a test needs a name"),
        (HEADER + "      - bogus: 1\n", "test broken, step 1 (bogus): unknown step"),
        (HEADER + "      - set: {state: on}\n        wait: 1\n", "step 1: expected one `kind: args` mapping"),
        (HEADER + "      - set: {state: dim}\n", "step 1 (set): state must be on or off"),
        (HEADER + "      - set: {}\n", "step 1 (set): missing state"),
        (HEADER + "      - expect_no_reload: {}\n", "step 1 (expect_no_reload): missing for"),
        (HEADER + "      - call: {target: lamp}\n", "target 'lamp' is not a profile role"),
        (HEADER + "      - call: {duration: forever}\n", "unknown name 'forever'"),
        (HEADER + "      - wait: duration / 2\n", "something other than numbers, names and + - *"),
        (HEADER + "      - wait: '1 +'\n", "cannot parse"),
        (HEADER + "      - wait: \"'a' + 1\"\n", "is not a number"),
        (HEADER + "      - wait: true\n", "expected a number or an expression"),
        (HEADER + "      - expect_error: {}\n", "name a key, a summary or both"),
        (HEADER + "      - expect_state: {}\n", "name a state or an attribute"),
        (HEADER + "      - expect_state: {brightness: {low: 1}}\n", "a range takes min and/or max"),
        (HEADER + "      - expect_state: {brightness: {min: a}}\n", "range bounds must be numbers"),
        (HEADER + "      - expect_response: {attributes: 3}\n", "attributes must be a mapping"),
        (HEADER + "      - sample: {attribute: brightness, for: 1, expect: wobbly}\n", "rising, falling or steady"),
        (HEADER + "      - sample: {attribute: brightness, for: 1, source: hue}\n", "source must be ha or deviceside"),
        (HEADER + "      - observe: ok?\n", "step 1 (observe): the observe step is gone; give the test observe:"),
        (
            HEADER.replace("    steps:\n", "    brief: {see: s, watch: w}\n    steps:\n") + "      - wait: 0\n",
            "test broken, brief is gone; give the test observe:",
        ),
        (observed("{see: s, watch: w}") + "      - wait: 0\n", "observe: missing ask"),
        (
            observed("{see: s, watch: w, ask: ''}") + "      - wait: 0\n",
            "observe: see, watch and ask must be non-empty",
        ),
        (observed("[a]") + "      - wait: 0\n", "observe: expected a mapping"),
        (observed(setup="[]") + "      - wait: 0\n", "setup must be a non-empty list"),
        (observed("{see: s, watch: w, ask: a, roles: []}") + "      - wait: 0\n", "observe: roles must be a non-empty"),
        (
            observed("{see: s, watch: w, ask: a, roles: [[a]]}") + "      - wait: 0\n",
            "observe: roles must be a non-empty",
        ),
        (
            observed("{see: s, watch: w, ask: a, roles: [group]}") + "      - expect_state: {state: on}\n",
            "observe roles group are not the test's roles",
        ),
        (
            observed("{see: s, watch: w, ask: a, roles: [switch_plug]}") + "      - wait: 0\n",
            "observe roles or only needs an automated run",
        ),
        (observed("{see: s, watch: w, ask: a, only: []}") + "      - wait: 0\n", "observe: only must be a non-empty"),
        (
            observed("{see: s, watch: w, ask: a, only: [hue]}") + "      - wait: 0\n",
            "observe roles or only needs an automated run",
        ),
        (
            observed("{see: s, watch: w, ask: a, only: [hue]}").replace("    steps:\n", "    only: [zha]\n    steps:\n")
            + "      - expect_state: {state: on}\n",
            "observe only hue are not in the test's only",
        ),
        (
            HEADER.replace("    steps:\n", "    observe: {see: s, watch: w, ask: a}\n    steps:\n")
            + "      - wait: 0\n",
            "a test with observe needs setup",
        ),
        (observed() + "      - do: Flip it.\n", "step 1 (do): do belongs in setup"),
        (
            HEADER.replace("    steps:\n", "    setup: [{do: Flip it.}]\n    steps:\n") + "      - wait: 0\n",
            "setup step 1 (do): do is only for a test with observe",
        ),
        (observed("{see: s, watch: w, ask: a}", "[{do: ''}]") + "      - wait: 0\n", "do takes the instruction"),
        (observed(setup="[{observe: x}]") + "      - wait: 0\n", "setup step 1 (observe): the observe step is gone"),
        (HEADER + "      - restore: 1\n", "restore takes no arguments"),
        (HEADER + "      - expect_response: 1\n", "expected a mapping"),
        (HEADER + "      - expect_response: {status: fine, colour: red}\n", "unknown key(s) colour"),
        (HEADER.replace("[switch_plug]", "[plug]") + "      - restore: null\n", "unknown role(s) plug"),
        (HEADER.replace("[switch_plug]", "[]") + "      - restore: null\n", "roles must be a non-empty list"),
        (HEADER.replace("    steps:\n", "    only: []\n    steps:\n") + "      - restore: null\n", "only must be"),
        (HEADER.replace("    steps:\n", "    steps: []\n"), "steps must be a non-empty list"),
        (HEADER.replace("    steps:\n", "    when: now\n    steps:\n") + "      - restore: null\n", "unknown key"),
        (HEADER + "      - restore: null\n" + HEADER.split("tests:\n")[1] + "      - restore: null\n", "duplicate"),
        (HEADER + "      - {wait: 1, only: []}\n", "step 1: only must be a non-empty list"),
        (HEADER + "      - {wait: 1, only: zha}\n", "step 1: only must be a non-empty list"),
        (HEADER + "      - {wait: 1, set: {state: off}, only: [zha]}\n", "expected one `kind: args` mapping"),
        (
            HEADER.replace("    steps:\n", "    only: [hue]\n    steps:\n") + "      - {wait: 1, only: [zha]}\n",
            "step 1 (wait): only zha are not in the test's only",
        ),
        (
            HEADER.replace("    steps:\n", "    firmware_fact: yes please\n    steps:\n") + "      - wait: 1\n",
            "firmware_fact",
        ),
        ("action: [\n", "while parsing"),
        *[
            (HEADER + f"      - call: {{duration: 1}}\n      - {{wait: 1, {step}}}\n", message)
            for step, message in [
                ("classes: [x]", "step 2: classes must map integration keys to non-empty lists"),
                ("classes: {}", "classes must map integration keys"),
                ("except_classes: {zha: []}", "except_classes must map integration keys"),
                ("classes: {zha: x}", "classes must map integration keys"),
                ("classes: {zha: [1]}", "classes must map integration keys"),
                ("classes: {zha: ['']}", "classes must map integration keys"),
                ("classes: {'': [x]}", "classes must map integration keys"),
                ("classes: {zha: [x]}, except_classes: {zha: [y]}", "classes or except_classes, not both"),
            ]
        ],
        (HEADER + "      - {wait: 1, classes: {zha: [x]}}\n", "step 1 (wait): a class selector needs a call step"),
        (
            HEADER.replace("    steps:\n", "    setup: [{wait: 0}]\n    steps:\n")
            + "      - {wait: 1, except_classes: {zha: [x]}}\n      - call: {duration: 1}\n",
            "step 1 (wait): a class selector needs a call step before it",
        ),
        (
            HEADER.replace("    steps:\n", "    only: [hue]\n    steps:\n")
            + "      - call: {duration: 1}\n      - {wait: 1, classes: {hue: [x], zha: [y]}}\n",
            "step 2 (wait): classes of zha are not in the test's only",
        ),
    ],
)
def test_a_schema_error_names_where_it_is(tmp_path: Path, text, message):
    path = tmp_path / "broken.yaml"
    path.write_text(text)
    with pytest.raises(SchemaError, match=r"^broken\.yaml: ") as err:
        load_step_file(path)
    assert message in str(err.value)


@pytest.mark.parametrize(
    ("text", "value"), [("6", 6), ("duration - tol", 9.5), ("2 * tol", 1.0), ("-tol + duration", 9.5), (1.5, 1.5)]
)
def test_expressions_evaluate_against_the_run(text, value):
    assert Expr.parse(text).eval({"duration": 10, "tol": 0.5}) == value


def test_an_observed_test_is_parsed_with_its_setup(tmp_path: Path):
    path = tmp_path / "observed.yaml"
    text = observed("{see: ' It clicks. ', watch: Twice., ask: 'Two?'}", "[{do: Flip it.}, {set: {state: off}}]")
    path.write_text(text + "      - call: {}\n      - expect_state: {state: on}\n")
    (test,) = load_step_file(path).tests
    assert test.observe == Observe("It clicks.", "Twice.", "Two?")
    assert [step.label for step in test.setup] == ["setup step 1 (do)", "setup step 2 (set)"]
    assert not test.automated  # its setup needs the person


def test_observe_roles_limit_the_observed_cases_to_those_roles(tmp_path: Path):
    path = tmp_path / "observed.yaml"
    text = observed("{see: s, watch: w, ask: a, roles: [light_color]}").replace(
        "[switch_plug]", "[light_color, group_color]"
    )
    path.write_text(text + "      - expect_state: {state: on}\n")
    (test,) = load_step_file(path).tests
    assert test.observe.roles == {"light_color"}
    assert test.observed_on("light_color", "zha")
    assert not test.observed_on("group_color", "zha")


def test_observe_only_limits_the_observed_cases_to_those_integrations(tmp_path: Path):
    path = tmp_path / "observed.yaml"
    path.write_text(observed("{see: s, watch: w, ask: a, only: [zigbee2mqtt]}") + "      - expect_state: {state: on}\n")
    (test,) = load_step_file(path).tests
    assert test.observe.only == {"zigbee2mqtt"}
    assert test.observed_on("switch_plug", "zigbee2mqtt")
    assert not test.observed_on("switch_plug", "hue")
    assert test.automated


@pytest.mark.parametrize(
    ("steps", "automated"),
    [("      - call: {}\n", False), ("      - call: {}\n      - sample: {attribute: b, for: 1}\n", True)],
)
def test_an_observed_test_is_automated_too_when_its_steps_check_something(tmp_path: Path, steps, automated):
    path = tmp_path / "observed.yaml"
    path.write_text(observed() + steps)
    assert load_step_file(path).tests[0].automated is automated


def test_a_step_takes_only_beside_its_kind(tmp_path: Path):
    path = tmp_path / "only.yaml"
    path.write_text(
        HEADER + "      - {wait: 2, only: [zha]}\n      - wait: 1\n        only: [hue, zha]\n      - wait: 3\n"
    )
    steps = load_step_file(path).tests[0].steps
    assert [(step.kind, step.only) for step in steps] == [
        ("wait", frozenset({"zha"})),
        ("wait", frozenset({"hue", "zha"})),
        ("wait", None),
    ]


def test_a_step_takes_a_class_selector_beside_its_kind(tmp_path):
    path = tmp_path / "classes.yaml"
    path.write_text(
        HEADER.replace("    steps:\n", "    setup: [{call: {duration: 1}}]\n    steps:\n")
        + "      - {wait: 2, classes: {zigbee2mqtt: [tuya_countdown, other], zha: [x]}, only: [zigbee2mqtt]}\n"
        + "      - wait: 1\n        except_classes: {zigbee2mqtt: [tuya_countdown]}\n"
        + "      - wait: 3\n"
    )
    test = load_step_file(path).tests[0]
    with_classes, without, plain = test.steps
    assert with_classes.classes == {"zigbee2mqtt": ("tuya_countdown", "other"), "zha": ("x",)}
    assert with_classes.except_classes is None and with_classes.only == frozenset({"zigbee2mqtt"})
    assert (without.classes, without.except_classes) == (None, {"zigbee2mqtt": ("tuya_countdown",)})
    assert (plain.classes, plain.except_classes, plain.class_selector) == (None, None, None)
    assert without.kind == "wait"  # the selector key is not part of the step's arguments


def test_firmware_fact_marks_the_test(tmp_path: Path):
    path = tmp_path / "fact.yaml"
    path.write_text(HEADER.replace("    steps:\n", "    firmware_fact: true\n    steps:\n") + "      - wait: 1\n")
    (fact,) = load_step_file(path).tests
    path.write_text(HEADER + "      - wait: 1\n")
    (plain,) = load_step_file(path).tests
    assert (fact.firmware_fact, plain.firmware_fact) == (True, False)
