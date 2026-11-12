from ha_rig.observed import FAIL, HEADER, PASS, Observation, ObservedFile

LAMP = "Hue ambiance lamp (`LTA009`), firmware `1.163.1`"


def test_the_file_round_trips_its_dated_rows_sorted_by_test(tmp_path):
    path = tmp_path / "_observed.md"
    observed = ObservedFile("Hue")
    observed.add(
        [
            Observation("2026-11-12", "raise[light_ct] observed", LAMP, PASS),
            Observation("2026-11-10", "lower[group] observed", f"Zone, firmware `unknown`; {LAMP}", FAIL),
        ]
    )
    observed.write(path)
    text = path.read_text()
    assert text.startswith("# Hue · observed\n") and HEADER in text
    assert text.index("lower[group]") < text.index("raise[light_ct]")
    assert ObservedFile.read(path, "ignored").rows == sorted(observed.rows, key=lambda row: row.test)


def test_a_pass_and_a_fail_both_stand_and_only_an_identical_row_is_left_out(tmp_path):
    path = tmp_path / "_observed.md"
    seen = Observation("2026-11-12", "raise[light_ct] observed", LAMP, PASS)
    observed = ObservedFile("Hue")
    observed.add([seen, seen])
    observed.write(path)
    again = ObservedFile.read(path, "Hue")
    again.add([seen, Observation("2026-11-12", "raise[light_ct] observed", LAMP, FAIL)])
    assert [row.result for row in again.rows] == [PASS, FAIL]


def test_a_missing_file_reads_empty_and_a_pipe_in_a_cell_survives(tmp_path):
    assert ObservedFile.read(tmp_path / "none.md", "Hue").rows == []
    path = tmp_path / "_observed.md"
    observed = ObservedFile("Hue", [Observation("2026-11-12", "t[x] observed", "a | b", PASS)])
    observed.write(path)
    assert ObservedFile.read(path, "Hue").rows == observed.rows
