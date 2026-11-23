"""Minimal HID report descriptor reader (reports, sizes, usage pages); never raises.

Only what Fire TV remote support needs: which report IDs exist, in which direction, how many
payload bytes each carries and under which usage page. Report IDs are what the remote's firmware
defines, so a remote is judged by its descriptor, not by a model table.
"""

from dataclasses import dataclass

AUDIO_ID = 0xF0
MIC_ID = 0xF2
VENDOR_PAGE = 0xFF00
AUDIO_BYTES = 80
MIC_BYTES = 1

_INPUT, _OUTPUT, _FEATURE = 0x8, 0x9, 0xB
_KINDS = {_INPUT: "input", _OUTPUT: "output", _FEATURE: "feature"}


@dataclass(frozen=True)
class Report:
    id: int
    kind: str  # "input" | "output" | "feature"
    size_bytes: int  # payload bytes, report ID excluded
    usage_page: int | None  # page in force at the first main item of this report
    is_array: bool  # array (key list) as opposed to variable (bitmap/values)
    element_bits: int = 0  # Report Size of the first main item (8 or 16 for key arrays)
    count: int = 0  # Report Count of the first main item
    # Every main item of the report: (bit offset, element bits, count, usage page, is_array).
    items: tuple = ()


def parse_reports(descriptor):
    """Reports declared by a HID report descriptor; on malformed input, what parsed so far."""
    found = {}
    try:
        _walk(bytes(descriptor), found)
    except IndexError, ValueError, TypeError:
        pass
    reports = []
    for (rid, kind), items in found.items():
        first = items[0]
        bits = sum(size * count for _o, size, count, _p, _a in items)
        reports.append(
            Report(rid, kind, bits // 8, first[3], first[4], first[1], first[2], tuple(items))
        )
    return tuple(reports)


def _walk(data, found):
    state = {"page": None, "size": 0, "count": 0, "id": 0}
    stack = []
    index = 0
    while index < len(data):
        prefix = data[index]
        if prefix == 0xFE:  # long item: skip (length byte follows)
            index += 3 + data[index + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 3]
        kind, tag = (prefix >> 2) & 3, prefix >> 4
        value = int.from_bytes(data[index + 1 : index + 1 + size], "little")
        if len(data) < index + 1 + size:
            return  # truncated item
        index += 1 + size
        if kind == 1:  # global
            if tag == 0:
                state["page"] = value
            elif tag == 7:
                state["size"] = value
            elif tag == 8:
                state["id"] = value
            elif tag == 9:
                state["count"] = value
            elif tag == 10:
                stack.append(dict(state))
            elif tag == 11 and stack:
                state = stack.pop()
        elif kind == 0 and tag in _KINDS:  # main item that defines report data
            items = found.setdefault((state["id"], _KINDS[tag]), [])
            offset = sum(size * count for _o, size, count, _p, _a in items)
            # An array item has neither the Constant (bit 0) nor the Variable (bit 1) flag.
            items.append((offset, state["size"], state["count"], state["page"], not value & 3))


def is_vendor_page(page):
    """Vendor-defined usage pages are 0xFF00-0xFFFF. The 3rd-gen remote (product 0x0414, issue #1)
    declares 0x00FF instead, so that value is accepted as well; ordinary pages never are."""
    return page is not None and (page >= 0xFF00 or page == 0x00FF)


def _find(reports, kind, rid):
    return next((r for r in reports if r.kind == kind and r.id == rid), None)


def voice_checks(reports):
    """Each requirement of the verified Fire voice shape, with what was found (for support)."""
    audio, mic = _find(reports, "input", AUDIO_ID), _find(reports, "output", MIC_ID)
    page = None if audio is None or audio.usage_page is None else f"0x{audio.usage_page:04x}"
    return [
        {
            "check": "audio_in_present",
            "ok": audio is not None,
            "found": audio and f"0x{AUDIO_ID:02x}",
        },
        {
            "check": "audio_in_size_80",
            "ok": audio is not None and audio.size_bytes == AUDIO_BYTES,
            "found": audio and audio.size_bytes,
        },
        {
            "check": "audio_in_vendor_page",
            "ok": audio is not None and is_vendor_page(audio.usage_page),
            "found": page,
        },
        {"check": "mic_out_present", "ok": mic is not None, "found": mic and f"0x{MIC_ID:02x}"},
        {
            "check": "mic_out_size_1",
            "ok": mic is not None and mic.size_bytes == MIC_BYTES,
            "found": mic and mic.size_bytes,
        },
    ]


def find_voice_reports(reports):
    """``(audio_in, mic_out)`` only for the verified Fire shape, else None.

    Audio is input report 0xF0 of 80 bytes on a vendor page; the microphone enable is output
    report 0xF2 of exactly 1 byte. Anything else is refused rather than written to blindly;
    ``voice_checks`` says which requirement a remote missed.
    """
    reports = tuple(reports)
    if not all(check["ok"] for check in voice_checks(reports)):
        return None
    return _find(reports, "input", AUDIO_ID), _find(reports, "output", MIC_ID)


KEYBOARD_PAGE = 0x07
CONSUMER_PAGE = 0x0C


@dataclass(frozen=True)
class KeyUsage:
    page: int
    usage: int


def decode_keys(reports, data):
    """Keys held in one input report, as ``KeyUsage`` values; unknown shapes give ``frozenset()``.

    Every byte-aligned array item on the keyboard or consumer page is decoded (that covers
    remotes' key lists and the standard boot keyboard's six-key array): each non-zero element is a
    usage. Bitmaps (modifiers) and padding are skipped. Never raises.
    """
    try:
        data = bytes(data)
        inputs = {r.id: r for r in reports if r.kind == "input"}
        if data and data[0] in inputs and data[0] != 0:
            report, payload = inputs[data[0]], data[1:]
        elif 0 in inputs:
            report, payload = inputs[0], data
        else:
            return frozenset()
        items = report.items or (
            (0, report.element_bits, report.count, report.usage_page, report.is_array),
        )
        keys = set()
        for offset, element, count, page, array in items:
            if not array or page not in (KEYBOARD_PAGE, CONSUMER_PAGE):
                continue
            if element not in (8, 16) or offset % 8:
                continue
            width, start = element // 8, offset // 8
            chunk = payload[start : start + count * width]
            for index in range(0, len(chunk) - width + 1, width):
                value = int.from_bytes(chunk[index : index + width], "little")
                if value and not (page == KEYBOARD_PAGE and value <= 3):  # 1-3: rollover/errors
                    keys.add(KeyUsage(page, value))
        return frozenset(keys)
    except TypeError, ValueError:
        return frozenset()
