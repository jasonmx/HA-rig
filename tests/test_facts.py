from ha_rig.facts import AUTOMATED, FAIL, HEADER, OBSERVED, PASS, Fact, FactsFile, append

LAMP = "Hue ambiance lamp (`LTA009`), firmware `1.163.1`"


def test_a_new_file_starts_with_its_heading_and_each_row_is_appended_in_turn(tmp_path):
    path = tmp_path / "hue" / "_facts.md"
    first = Fact("2026-11-12", "raise", "raise[light_ct] observed", OBSERVED, LAMP, PASS)
    second = Fact("2026-11-10", "stop", "wins[light_ct]", AUTOMATED, f"Zone, firmware `unknown`; {LAMP}", FAIL)
    append(path, "Hue", first)
    append(path, "Hue", second)
    append(path, "ignored", first)  # the same row again: appended, as each case ends once
    text = path.read_text()
    assert text.startswith("# Hue · facts\n") and text.count(HEADER) == 1 and "ignored" not in text
    assert FactsFile.read(path).rows == (first, second, first)


def test_an_empty_file_gets_its_heading(tmp_path):
    (path := tmp_path / "_facts.md").write_text("")
    append(path, "Hue", Fact("2026-11-12", "s", "t[x]", AUTOMATED, LAMP, PASS))
    assert path.read_text().startswith("# Hue · facts\n")


def test_a_line_the_rig_cant_read_survives_a_write(tmp_path):
    path = tmp_path / "_facts.md"
    append(path, "Hue", Fact("2026-11-12", "s", "t[x]", AUTOMATED, LAMP, PASS))
    path.write_text(path.read_text() + "A note a maintainer added by hand | with a pipe\n")
    append(path, "Hue", Fact("2026-11-13", "s", "t[x]", AUTOMATED, LAMP, FAIL))
    assert "A note a maintainer added by hand | with a pipe\n" in path.read_text()
    assert [row.result for row in FactsFile.read(path).rows] == [PASS, FAIL]


def test_latest_is_the_last_row_for_the_case(tmp_path):
    path = tmp_path / "_facts.md"
    for result in (PASS, FAIL, PASS):  # one day
        append(path, "Hue", Fact("2026-11-12", "s", "t[x]", AUTOMATED, LAMP, result))
    facts = FactsFile.read(path)
    assert facts.latest("s", "t[x]").result == PASS
    assert str(facts.latest("s", "t[x]")) == f"last on file 2026-11-12, on {LAMP}: pass"
    assert facts.latest("s", "u[x]") is None


def test_a_missing_file_reads_empty_and_a_pipe_in_a_cell_survives(tmp_path):
    assert FactsFile.read(tmp_path / "none.md").rows == ()
    path = tmp_path / "_facts.md"
    fact = Fact("2026-11-12", "s", "t[x] observed", OBSERVED, "a | b", PASS)
    append(path, "Hue", fact)
    assert FactsFile.read(path).rows == (fact,)


def test_latest_keeps_apart_two_suites_sharing_a_case_id(tmp_path):
    path = tmp_path / "_facts.md"
    append(path, "Shelly", Fact("2026-11-12", "set_auto_off", "takes_the_bounds[switch_plug]", AUTOMATED, LAMP, FAIL))
    append(path, "Shelly", Fact("2026-11-12", "set_auto_on", "takes_the_bounds[switch_plug]", AUTOMATED, LAMP, PASS))
    facts = FactsFile.read(path)
    assert facts.latest("set_auto_off", "takes_the_bounds[switch_plug]").result == FAIL
    assert facts.latest("set_auto_on", "takes_the_bounds[switch_plug]").result == PASS
    assert facts.latest("stop", "takes_the_bounds[switch_plug]") is None
