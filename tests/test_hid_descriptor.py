from pathlib import Path

import pytest

from custom_components.bt_remote_voice.hid_descriptor import (
    Report,
    find_voice_reports,
    parse_reports,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fire_0413_report_descriptor.hex"


def fire_0413():
    return bytes.fromhex(FIXTURE.read_text().strip())


def table(reports):
    return {(r.kind, r.id): r for r in reports}


def test_the_verified_0413_descriptor_parses_to_the_documented_reports():
    reports = table(parse_reports(fire_0413()))
    sizes = {key: report.size_bytes for key, report in reports.items()}
    assert sizes == {
        ("input", 0x01): 3,
        ("input", 0x02): 4,
        ("input", 0x03): 1,
        ("input", 0xF0): 80,
        ("input", 0xF1): 3,
        ("output", 0xF2): 1,
        ("output", 0xF3): 10,
    }
    assert reports[("input", 0x01)].usage_page == 0x07 and reports[("input", 0x01)].is_array
    assert reports[("input", 0x02)].usage_page == 0x0C
    assert reports[("input", 0x03)].usage_page == 0x06 and not reports[("input", 0x03)].is_array
    assert reports[("input", 0xF0)].usage_page == 0xFF00 and reports[("input", 0xF0)].is_array
    assert reports[("output", 0xF2)].usage_page == 0xFF00


def test_find_voice_reports_on_0413_returns_f0_and_f2():
    audio, mic = find_voice_reports(parse_reports(fire_0413()))
    assert (audio.id, audio.kind, audio.size_bytes) == (0xF0, "input", 80)
    assert (mic.id, mic.kind, mic.size_bytes) == (0xF2, "output", 1)


def report(id, kind, size, page=0xFF00):
    return Report(id, kind, size, page, True)


@pytest.mark.parametrize(
    "reports",
    [
        [report(0xF0, "input", 80, page=0x0C), report(0xF2, "output", 1)],  # wrong page
        [report(0xF0, "input", 64), report(0xF2, "output", 1)],  # wrong audio size
        [report(0xF0, "input", 80), report(0xF3, "output", 10)],  # only F3, no F2
        [report(0xF0, "input", 80), report(0xF2, "output", 10)],  # F2 with another length
        [report(0xF0, "feature", 80), report(0xF2, "output", 1)],  # wrong direction
        [report(0xF5, "input", 80), report(0xF2, "output", 1)],  # other audio id
        [],
    ],
)
def test_voice_pair_is_refused_unless_it_is_exactly_the_verified_shape(reports):
    assert find_voice_reports(reports) is None


def test_truncated_and_long_items_and_push_pop_do_not_raise():
    data = fire_0413()
    for cut in range(len(data) + 1):  # every truncation point
        parse_reports(data[:cut])
    # long item (0xFE, length 2, tag, 2 data bytes) is skipped; push/pop restore globals
    descriptor = bytes.fromhex(
        "06 00ff 85 f0 75 08 95 50 a4 95 01 b4 81 00 fe 02 10 aa bb".replace(" ", "")
    )
    (only,) = parse_reports(descriptor)
    assert (only.id, only.size_bytes, only.usage_page) == (0xF0, 80, 0xFF00)
    assert parse_reports(b"") == ()
    assert parse_reports(b"\xff") == ()


def test_report_id_zero_when_the_descriptor_declares_none():
    # one 16-byte input report, no Report ID item
    (only,) = parse_reports(bytes.fromhex("0600ff 7508 9510 8100".replace(" ", "")))
    assert (only.id, only.kind, only.size_bytes) == (0, "input", 16)


def test_sizes_of_the_same_id_and_direction_are_summed():
    descriptor = bytes.fromhex("850a 7508 9502 8100 7508 9503 8100".replace(" ", ""))
    (only,) = parse_reports(descriptor)
    assert (only.id, only.size_bytes) == (0x0A, 5)


def test_usage_page_change_inside_a_collection_is_tracked():
    # report 3 in the real descriptor switches from the consumer to the device-controls page
    reports = table(parse_reports(fire_0413()))
    assert reports[("input", 0x03)].usage_page == 0x06


@pytest.mark.parametrize("junk", [None, "abc", 5, object()])
def test_parse_reports_never_raises_on_non_bytes_input(junk):
    assert parse_reports(junk) == ()


# ---- key decoding (used by the Test my remote step) ----
from custom_components.bt_remote_voice.hid_descriptor import KeyUsage, decode_keys  # noqa: E402


def test_the_0413_descriptor_exposes_key_array_geometry():
    reports = {(r.kind, r.id): r for r in parse_reports(fire_0413())}
    keyboard, consumer = reports[("input", 0x01)], reports[("input", 0x02)]
    assert (keyboard.element_bits, keyboard.count) == (8, 3)
    assert (consumer.element_bits, consumer.count) == (16, 2)


def test_decode_keys_reads_consumer_and_keyboard_arrays():
    reports = parse_reports(fire_0413())
    assert decode_keys(reports, bytes.fromhex("0221020000")) == {KeyUsage(0x0C, 0x0221)}  # voice
    assert decode_keys(reports, bytes.fromhex("02e900b300")) == {
        KeyUsage(0x0C, 0x00E9),
        KeyUsage(0x0C, 0x00B3),
    }
    assert decode_keys(reports, bytes.fromhex("01000052")) == {KeyUsage(0x07, 0x52)}  # up
    assert decode_keys(reports, bytes.fromhex("0200000000")) == frozenset()  # release
    assert decode_keys(reports, bytes.fromhex("01010100")) == frozenset()  # rollover only


@pytest.mark.parametrize(
    "data",
    [b"", b"\xf0" + bytes(80), bytes.fromhex("03 50".replace(" ", "")), b"\x09\x01", None, "x", 5],
)
def test_decode_keys_ignores_audio_battery_unknown_ids_and_garbage(data):
    assert decode_keys(parse_reports(fire_0413()), data) == frozenset()


def test_decode_keys_handles_reports_without_an_id_prefix():
    # one 2-byte consumer array, no Report ID item: the whole packet is the payload
    descriptor = bytes.fromhex("05 0c 75 10 95 01 81 00".replace(" ", ""))
    assert decode_keys(parse_reports(descriptor), bytes.fromhex("2102")) == {KeyUsage(0x0C, 0x0221)}


# ---- review fixes: boot keyboard layout, constants are not arrays ----
BOOT_KEYBOARD = bytes.fromhex(
    "05010906a101 0507 19e029e7 15002501 7501 9508 8102 "  # modifier bitmap (variable)
    "9501 7508 8101 "  # reserved byte (constant, NOT an array)
    "9506 7508 15002565 19002965 8100 c0".replace(" ", "")  # six-key array
)


def test_a_standard_boot_keyboard_report_decodes_its_key_array():
    reports = parse_reports(BOOT_KEYBOARD)
    (report,) = reports
    assert report.size_bytes == 8 and len(report.items) == 3
    assert [item[4] for item in report.items] == [False, False, True]  # bitmap, padding, array
    assert decode_keys(reports, bytes.fromhex("00 00 52 00 00 00 00 00".replace(" ", ""))) == {
        KeyUsage(0x07, 0x52)
    }
    assert (
        decode_keys(reports, bytes.fromhex("02 00 00 00 00 00 00 00".replace(" ", "")))
        == frozenset()
    )


def test_a_constant_item_is_never_treated_as_a_key_array():
    descriptor = bytes.fromhex("0507 7508 9501 8101 9501 8100".replace(" ", ""))
    (report,) = parse_reports(descriptor)
    assert [item[4] for item in report.items] == [False, True]


# ---- issue #1: a 3rd-gen remote (0x0414) declares its vendor page as 0x00FF -----------------
from custom_components.bt_remote_voice.hid_descriptor import voice_checks  # noqa: E402

FIXTURE_0414 = Path(__file__).parent / "fixtures" / "fire_0414_report_descriptor.hex"


def fire_0414():
    return bytes.fromhex(FIXTURE_0414.read_text().strip())


def test_the_reported_0414_descriptor_has_the_voice_pair_on_page_00ff():
    reports = table(parse_reports(fire_0414()))
    assert (
        reports[("input", 0xF0)].usage_page == 0x00FF and reports[("input", 0xF0)].size_bytes == 80
    )
    assert reports[("output", 0xF2)].size_bytes == 1
    assert reports[("input", 0xEF)].size_bytes == 3  # the 3rd gen's app-button report
    audio, mic = find_voice_reports(parse_reports(fire_0414()))
    assert (audio.id, mic.id) == (0xF0, 0xF2)


@pytest.mark.parametrize("page", [0xFF00, 0xFF01, 0xFFFF, 0x00FF])
def test_vendor_style_pages_are_accepted(page):
    assert find_voice_reports(
        [report(0xF0, "input", 80, page=page), report(0xF2, "output", 1, page=page)]
    )


@pytest.mark.parametrize("page", [0x0001, 0x0007, 0x000C, 0x00FE, 0x0100, 0xFE00, 0xFEFF, None])
def test_ordinary_pages_are_still_refused(page):
    assert (
        find_voice_reports([report(0xF0, "input", 80, page=page), report(0xF2, "output", 1)])
        is None
    )


def test_voice_checks_explain_each_requirement_for_a_passing_descriptor():
    checks = voice_checks(parse_reports(fire_0413()))
    assert [c["check"] for c in checks] == [
        "audio_in_present",
        "audio_in_size_80",
        "audio_in_vendor_page",
        "mic_out_present",
        "mic_out_size_1",
    ]
    assert all(c["ok"] for c in checks)


def test_voice_checks_name_the_requirement_that_failed_and_what_was_found():
    # audio on an ordinary page, microphone report 10 bytes long
    reports = [report(0xF0, "input", 80, page=0x000C), report(0xF2, "output", 10)]
    failed = {c["check"]: c["found"] for c in voice_checks(reports) if not c["ok"]}
    assert failed == {"audio_in_vendor_page": "0x000c", "mic_out_size_1": 10}


def test_voice_checks_on_an_empty_descriptor_fail_everything_without_raising():
    checks = voice_checks(())
    assert [c["ok"] for c in checks] == [False] * 5 and checks[0]["found"] is None
