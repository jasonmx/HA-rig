from ha_rig.facts import FAIL, FIRMWARE_FACT, HEADER, OBSERVED, PASS, Fact, FactsFile

LAMP = "Hue ambiance lamp (`LTA009`), firmware `1.163.1`"


def test_the_file_round_trips_its_rows_in_the_order_they_were_added(tmp_path):
    path = tmp_path / "_facts.md"
    facts = FactsFile("Hue")
    facts.add(
        [
            Fact("2026-11-12", "raise[light_ct] observed", OBSERVED, LAMP, PASS),
            Fact("2026-11-10", "wins[light_ct]", FIRMWARE_FACT, f"Zone, firmware `unknown`; {LAMP}", FAIL),
        ]
    )
    facts.write(path)
    text = path.read_text()
    assert text.startswith("# Hue · facts\n") and HEADER in text
    assert text.index("raise[light_ct]") < text.index("wins[light_ct]")  # appended, never sorted
    assert FactsFile.read(path, "ignored").rows == facts.rows


def test_rows_are_only_appended_and_only_an_identical_one_is_left_out(tmp_path):
    path = tmp_path / "_facts.md"
    seen = Fact("2026-11-12", "raise[light_ct] observed", OBSERVED, LAMP, PASS)
    FactsFile("Hue", [seen]).write(path)
    again = FactsFile.read(path, "Hue")
    again.add([seen, Fact("2026-11-12", "raise[light_ct] observed", OBSERVED, LAMP, FAIL)])
    assert [row.result for row in again.rows] == [PASS, FAIL]


def test_latest_is_the_newest_date_and_of_one_date_the_last_added():
    old = Fact("2026-11-01", "t[x]", FIRMWARE_FACT, LAMP, FAIL)
    facts = FactsFile("Hue", [Fact("2026-11-12", "t[x]", FIRMWARE_FACT, LAMP, PASS), old])
    assert str(facts.latest("t[x]")) == "last on file 2026-11-12: pass"
    facts.add([Fact("2026-11-12", "t[x]", FIRMWARE_FACT, "another", FAIL)])
    assert str(facts.latest("t[x]")) == "last on file 2026-11-12: fail"
    assert facts.latest("u[x]") is None


def test_a_missing_file_reads_empty_and_a_pipe_in_a_cell_survives(tmp_path):
    assert FactsFile.read(tmp_path / "none.md", "Hue").rows == []
    path = tmp_path / "_facts.md"
    facts = FactsFile("Hue", [Fact("2026-11-12", "t[x] observed", OBSERVED, "a | b", PASS)])
    facts.write(path)
    assert FactsFile.read(path, "Hue").rows == facts.rows
