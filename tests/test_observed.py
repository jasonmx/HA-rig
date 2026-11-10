from ha_rig.observed import (
    FAIL,
    PASS,
    Device,
    Observation,
    ObservedFile,
    Trace,
    begins,
    call_hash,
    lend,
    normalize,
    render_call,
    sequence,
    short_hash,
)

RAISE = ("zcl", "0x0008/0x01", {"move_mode": 0, "rate": 50}, {})
STOP = ("zcl", "0x0008/0x03", {}, {})
LOWER = ("zcl", "0x0008/0x01", {"move_mode": 1, "rate": 50}, {})

SIGNIFY = Device("Signify", "LCT015", "1.0")
SIGNIFY_NEWER = Device("Signify", "LCT015", "2.0")
SIGNIFY_OTHER = Device("Signify", "LTA001", "1.0")
IKEA = Device("IKEA", "LED2003G10", "1.0")


def seq(*calls, slot="light_ct"):
    return sequence((slot, call) for call in calls)[0]


def row(device, sequence_, result=PASS, date="2026-11-01", state="s0"):
    return Observation(device, state, sequence_, "t[light_ct]", date, result)


def test_sequence_collapses_consecutive_repeats_and_keeps_slots_apart():
    got, legend = sequence([("a", RAISE), ("a", RAISE), ("b", STOP), ("a", STOP), ("a", RAISE)])
    assert got == (("a", (call_hash(RAISE), call_hash(STOP), call_hash(RAISE))), ("b", (call_hash(STOP),)))
    assert set(legend) == {call_hash(RAISE), call_hash(STOP)}


def test_begins_equal_prefix_longer_and_slots():
    long = seq(RAISE, STOP, RAISE)
    assert begins(long, long)
    assert begins(seq(RAISE, STOP), long)
    assert not begins(long, seq(RAISE, STOP))
    assert not begins(seq(RAISE, STOP, slot="group"), long)


def test_a_pass_lends_its_prefixes_across_firmware_and_models_of_its_maker():
    rows = [row(SIGNIFY, seq(RAISE, STOP, RAISE))]
    assert lend(rows, "s0", seq(RAISE, STOP), [SIGNIFY_NEWER, SIGNIFY_OTHER]) == rows[0]
    assert lend(rows, "s0", seq(RAISE, STOP, RAISE), [SIGNIFY]) == rows[0]


def test_no_lend_for_a_longer_sequence_another_state_another_maker_or_no_calls():
    rows = [row(SIGNIFY, seq(RAISE, STOP))]
    assert lend(rows, "s0", seq(RAISE, STOP, RAISE), [SIGNIFY]) is None
    assert lend(rows, "s1", seq(RAISE, STOP), [SIGNIFY]) is None
    assert lend(rows, "s0", seq(RAISE, STOP), [IKEA]) is None
    assert lend(rows, "s0", seq(RAISE, STOP), [SIGNIFY, IKEA]) is None
    assert lend(rows, "s0", (), [SIGNIFY]) is None
    assert lend(rows, "s0", seq(RAISE, STOP), []) is None


def test_a_fail_blocks_sequences_beginning_with_it_on_its_model_only():
    rows = [row(SIGNIFY, seq(RAISE, STOP, RAISE)), row(SIGNIFY, seq(RAISE, STOP), FAIL, "2026-11-05")]
    assert lend(rows, "s0", seq(RAISE, STOP), [SIGNIFY_NEWER]) is None
    assert lend(rows, "s0", seq(RAISE, STOP, RAISE), [SIGNIFY]) is None
    assert lend(rows, "s0", seq(RAISE, STOP), [SIGNIFY_OTHER]) == rows[0]


def test_a_fail_of_the_longer_sequence_never_blocks_a_shorter_one():
    rows = [row(SIGNIFY, seq(RAISE, STOP)), row(SIGNIFY_NEWER, seq(RAISE, STOP, RAISE), FAIL, "2026-11-05")]
    assert lend(rows, "s0", seq(RAISE, STOP), [SIGNIFY]) == rows[0]


def test_a_newer_pass_on_the_model_lifts_a_fail():
    fail = row(SIGNIFY, seq(RAISE, STOP), FAIL, "2026-11-05")
    later = row(SIGNIFY_NEWER, seq(RAISE, STOP), PASS, "2026-11-08")
    assert lend([fail, later], "s0", seq(RAISE, STOP), [SIGNIFY]) == later
    same_day = row(SIGNIFY_NEWER, seq(RAISE, STOP), PASS, "2026-11-05")
    assert lend([fail, same_day], "s0", seq(RAISE, STOP), [SIGNIFY]) is None


def test_the_file_round_trips_and_a_row_with_the_same_key_is_replaced(tmp_path):
    path = tmp_path / "zha" / "_observed.md"
    long, legend = sequence([("light_ct", RAISE), ("light_ct", STOP), ("member_1", LOWER)])
    observed = ObservedFile.read(path, "ZHA")
    observed.add([row(SIGNIFY, long), row(IKEA, seq(STOP), FAIL)], legend | sequence([("light_ct", STOP)])[1])
    observed.write(path)

    again = ObservedFile.read(path, "ignored")
    assert again.title == "ignored"  # the title comes from the caller, not the file
    assert set(again.rows) == set(observed.rows)
    assert again.legend == {call_hash(c): c for c in (RAISE, STOP, LOWER)}

    again.add([row(SIGNIFY, long, date="2026-11-09")], {})
    assert {(r.device, r.date) for r in again.rows} == {(IKEA, "2026-11-01"), (SIGNIFY, "2026-11-09")}
    again.add([row(SIGNIFY_NEWER, long)], {})
    assert len(again.rows) == 3  # another firmware is another row


def test_the_legend_lists_only_calls_some_row_uses(tmp_path):
    observed = ObservedFile("ZHA")
    observed.add([row(SIGNIFY, seq(RAISE))], dict([sequence([("x", RAISE)])[1].popitem(), (call_hash(STOP), STOP)]))
    assert call_hash(STOP) not in observed.render()
    assert call_hash(RAISE) in observed.render()


def hue(brightness, **extra):
    return ("hue", "put", {"dimming": {"brightness": brightness}, **extra}, {})


def test_numbers_hash_by_their_sign_and_nothing_else_jitters_into_the_hash():
    assert call_hash(hue(68.27)) == call_hash(hue(69.14))  # timing jitter
    assert call_hash(hue(0)) != call_hash(hue(6875))  # zero is its own sign
    assert call_hash(hue(-13)) != call_hash(hue(13))
    assert call_hash(hue(1, on=True)) != call_hash(hue(1, on=False))  # a bool is not a number
    assert call_hash(hue(1, mode="up")) != call_hash(hue(1, mode="down"))  # strings stay exact
    assert call_hash(hue(1)) != call_hash(("hue", "put", {"dim": {"brightness": 1}}, {}))  # keys stay exact
    assert normalize(("z", "c", {"a": [1, -2, 0, True, "x"]}, {"o": 1})) == (
        "z",
        "c",
        {"a": ["+", "-", "0", True, "x"]},
        {"o": 1},
    )


def test_the_legend_keeps_the_exact_call_and_the_latest_of_one_hash():
    _, legend = sequence([("a", hue(68.27)), ("a", STOP), ("a", hue(69.14))])
    assert legend[call_hash(hue(68.27))] == hue(69.14)


def z2m(native, brightness=1):
    return ("z2m", "set", {"brightness": brightness}, {"hue_native_control": native})


def test_options_are_hashed_only_when_present():
    assert call_hash(RAISE) == short_hash(["zcl", "0x0008/0x01", {"move_mode": "0", "rate": "+"}])  # as before the key
    assert call_hash(z2m(True)) != call_hash(z2m(False))
    assert call_hash(z2m(None)) not in {call_hash(z2m(True)), call_hash(z2m(False))}
    assert call_hash(z2m(True)) != call_hash(("z2m", "set", {"brightness": 1}, {}))
    assert normalize(z2m(None))[3] == {"hue_native_control": None}
    assert render_call(z2m(True)) == 'z2m set {"brightness":1} {"hue_native_control":true}'
    assert render_call(STOP) == "zcl 0x0008/0x03 {}"


def test_the_options_column_round_trips_and_an_old_four_cell_file_reads(tmp_path):
    observed = ObservedFile("Z2M")
    calls = [z2m(True), RAISE]
    seq_, legend = sequence(("target", call) for call in calls)
    observed.add([row(SIGNIFY, seq_)], legend)
    path = tmp_path / "_observed.md"
    observed.write(path)
    assert "| Call | Transport | Command | Params | Options |" in path.read_text()
    assert ObservedFile.read(path, "Z2M").legend == legend
    digest = call_hash(RAISE)
    path.write_text(
        f"## Calls\n\n| Call | Transport | Command | Params |\n|---|---|---|---|\n"
        f'| `{digest}` | zcl | `0x0008/0x01` | `{{"move_mode":0,"rate":50}}` |\n'
    )
    read = ObservedFile.read(path, "Z2M")
    assert read.legend[call_hash(RAISE)] == RAISE


def test_a_trace_round_trips_and_reads_an_old_ledger():
    seq_, legend = sequence([("target", z2m(True))])
    trace = Trace("s0", seq_, legend, (SIGNIFY,), True)
    data = trace.to_json()
    assert data["legend"][call_hash(z2m(True))] == list(z2m(True))
    assert Trace.from_json(data) == trace and Trace.from_json(data).legend == legend and Trace.from_json(data).unknown
    old = {**data, "legend": {call_hash(RAISE): list(RAISE[:3])}}
    del old["unknown"]
    assert Trace.from_json(old).legend == {call_hash(RAISE): RAISE} and not Trace.from_json(old).unknown


def test_a_pass_from_before_options_is_not_lent_and_an_unknown_trace_neither_lends_nor_records():
    old_hash = short_hash(["z2m", "set", {"brightness": "+"}])
    old = Observation(SIGNIFY, "s0", (("light_ct", (old_hash,)),), "t", "2026-11-01", PASS)
    seq_, legend = sequence([("light_ct", z2m(True))])
    assert Trace("s0", seq_, legend, (SIGNIFY,)).lent_by([old]) is None
    known = row(SIGNIFY, seq_)
    assert Trace("s0", seq_, legend, (SIGNIFY,)).lent_by([known]) == known
    unknown_seq, unknown_legend = sequence([("light_ct", z2m(None))])
    unknown = Trace("s0", unknown_seq, unknown_legend, (SIGNIFY,), True)
    assert unknown.lent_by([row(SIGNIFY, unknown_seq)]) is None
    assert unknown.rows("t", "2026-11-02", True) == []
    assert Trace("s0", seq_, legend, (SIGNIFY,)).rows("t", "2026-11-02", True)
