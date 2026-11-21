"""The shareable Remote Report: complete enough to classify a remote, never identifying."""

import json
import re
from pathlib import Path

import pytest

from custom_components.bt_remote_voice.bluez import Remote
from custom_components.bt_remote_voice.hid_descriptor import parse_reports
from custom_components.bt_remote_voice.report import (
    ReportInput,
    build_report,
    config_summary,
    hid_summary,
    mask_address,
)

LOOSE_MAC = re.compile(r"(?i)[0-9a-f]{2}(?:[:_-][0-9a-f]{2}){5}")  # any six octets, any mix
DESCRIPTOR = bytes.fromhex(
    (Path(__file__).parent / "fixtures" / "fire_0413_report_descriptor.hex").read_text().strip()
)
HID = "00001812-0000-1000-8000-00805f9b34fb"


def issue_1_remote():
    """The facts reported in issue #1 (3rd-gen Alexa Voice Remote)."""
    return Remote(
        "A8:42:A7:11:22:33",
        "AR",
        "/org/bluez/hci0/dev_A8_42_A7_11_22_33",
        True,
        True,
        "fire",
        uuids=(HID, "0000180f-0000-1000-8000-00805f9b34fb"),
        manufacturer=((0x0171, "0414027d00000008"),),
        appearance=0x0180,
        bonded=True,
        reasons=("manufacturer_0171",),
    )


def json_block(text):
    match = re.search(r"```json\n(.*?)\n```", text, re.S)
    assert match, text
    return json.loads(match.group(1))


@pytest.mark.parametrize(
    "text",
    [
        "A8:42:A7:11:22:33",
        "a8:42:a7:11:22:33",
        "A8-42-A7-11-22-33",
        "a8_42_a7_11_22_33",
        "/org/bluez/hci0/dev_A8_42_A7_11_22_33/service0010",
        "HID_UNIQ=a8:42:a7:11:22:33",
        "name is A8:42:A7:11:22:33, really",
    ],
)
def test_mask_address_hides_every_form_but_keeps_the_public_manufacturer_prefix(text):
    masked = mask_address(text)
    assert not LOOSE_MAC.search(masked)
    assert "11:22:33" not in masked and "11_22_33" not in masked and "11-22-33" not in masked
    assert "A8:42:A7:xx:xx:xx" in masked


def test_mask_address_leaves_uuids_hex_dumps_and_other_text_alone():
    text = "5de20000-5e8d-11e6-8b77-86f30ca893d3 0171 05010906a101 usb:v0171p0413"
    assert mask_address(text) == text


def test_report_never_contains_a_full_mac_in_any_form():
    remote = issue_1_remote()
    data = ReportInput(
        remote={
            **remote.signals(),
            "name": "A8:42:A7:11:22:33",  # BlueZ defaults the name to the address
            "address": "A8:42:A7:11:22:33",
        },
        hid={"uevent": "HID_UNIQ=a8:42:a7:11:22:33\nHID_NAME=AR", "nodes": 1},
        adapter={"path": "/org/bluez/hci0/dev_A8_42_A7_11_22_33"},
    )
    text = build_report(data)
    assert not LOOSE_MAC.search(text)
    for form in ("11:22:33", "11_22_33", "11-22-33", "A8:42:A7:11", "a8:42:a7:11"):
        assert form not in text
    assert "A8:42:A7:xx:xx:xx" in text


def test_report_has_no_names_urls_or_tokens():
    options = {
        "pipeline_id": "0123456789abcdef-secret-pipeline",
        "speaker": "media_player.living_room_secret_speaker",
        "reply_condition": [{"condition": "template", "value_template": "{{ is_state('x','y') }}"}],
        "min_press_seconds": 0.1,
        "input_mode": "single",
        "mode": "commands",
        "remote_type": "auto",
    }
    summary = config_summary(options, {"pipeline_id": "0123456789abcdef-secret-pipeline"})
    assert summary["pipeline"] == "explicit" and summary["speaker"] == "set"
    text = build_report(ReportInput(config=summary))
    for secret in ("secret", "living_room", "template", "http", "token", "0123456789abcdef"):
        assert secret not in text.lower()
    assert "preferred" in build_report(
        ReportInput(config=config_summary({"pipeline_id": "preferred"}, {}))
    )


def test_report_for_issue_1_facts_classifies_the_remote():
    remote = issue_1_remote()
    text = build_report(ReportInput(remote=remote.signals()))
    block = json_block(text)
    assert block["remote"]["family"] == "fire"
    assert block["remote"]["reasons"] == ["manufacturer_0171"]
    assert block["remote"]["manufacturer"] == [{"company": "0x0171", "value": "0414027d00000008"}]
    assert "name" not in block["remote"] and "AR" not in text.replace("AR2", "")
    assert "A8:42:A7:xx:xx:xx" in text and "manufacturer_0171" in text


def test_report_json_block_parses_with_schema_1():
    block = json_block(build_report(ReportInput(remote=issue_1_remote().signals())))
    assert block["schema"] == 1
    assert {"integration", "core", "python", "remote", "hid", "config", "outcome"} <= set(block)


def test_report_with_nothing_known_is_valid_explicit_and_bounded():
    text = build_report(ReportInput())
    block = json_block(text)
    assert block["schema"] == 1 and "unknown" in text and len(text) < 20_000


def test_report_includes_descriptor_hex_and_parsed_reports_for_the_0413_fixture():
    hid = hid_summary(parse_reports(DESCRIPTOR), DESCRIPTOR.hex(), nodes=1)
    block = json_block(build_report(ReportInput(hid=hid)))
    reports = {(r["kind"], r["id"]): r for r in block["hid"]["reports"]}
    assert (
        reports[("input", 0xF0)]["size_bytes"] == 80
        and reports[("output", 0xF2)]["size_bytes"] == 1
    )
    assert block["hid"]["descriptor_hex"] == DESCRIPTOR.hex()
    assert block["hid"]["voice_reports"] is True


def test_report_is_bounded_even_for_hostile_sizes():
    remote = issue_1_remote()
    signals = {
        **remote.signals(),
        "uuids": [f"{i:08x}-0000-1000-8000-00805f9b34fb" for i in range(500)],
    }
    signals["manufacturer"] = [{"company": f"0x{i:04x}", "value": "ab" * 64} for i in range(200)]
    hid = {
        "descriptor_hex": "ab" * 5000,
        "reports": [{"id": i, "kind": "input", "size_bytes": 1} for i in range(400)],
    }
    text = build_report(ReportInput(remote=signals, hid=hid))
    assert len(text) < 20_000 and json_block(text)["schema"] == 1


def test_report_building_never_raises_on_odd_input():
    odd = ReportInput(
        remote={"uuids": None, "manufacturer": 5}, hid={"reports": "x"}, config={"a": object()}
    )
    assert json_block(build_report(odd))["schema"] == 1


# ---- flow and diagnostics integration ----
from types import SimpleNamespace  # noqa: E402
from unittest.mock import patch  # noqa: E402

from custom_components.bt_remote_voice.config_flow import ConfigFlow  # noqa: E402


def _flow(hass, remote=None):
    flow = ConfigFlow()
    flow.hass = hass
    flow.selected = remote or issue_1_remote()
    return flow


async def test_the_setup_form_offers_a_show_report_switch_that_is_off_by_default(hass):
    flow = _flow(hass)
    with patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "P"}):
        result = await flow.async_step_configure()
    key = next(k for k in result["data_schema"].schema if k == "show_report")
    assert key.default() is False


async def test_a_failed_setup_report_step_shows_the_report_and_returns_to_the_form(hass):
    flow = _flow(hass)

    class Refuses:
        descriptor_reports = parse_reports(DESCRIPTOR)[:2]
        descriptor_hex = DESCRIPTOR.hex()

        async def open(self, *args):
            raise ValueError("fire_voice_unsupported")

        async def close(self):
            pass

    with (
        patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "P"}),
        patch("custom_components.bt_remote_voice.config_flow.FireTransport", Refuses),
    ):
        failed = await flow.async_step_configure({"pipeline_id": "p"})
        assert failed["errors"] == {"base": "fire_voice_unsupported"}
        shown = await flow.async_step_configure({"pipeline_id": "p", "show_report": True})
    assert shown["type"] == "form" and shown["step_id"] == "report"
    suggested = next(iter(shown["data_schema"].schema)).description["suggested_value"]
    block = json_block(suggested)
    assert block["outcome"]["error"] == "fire_voice_unsupported"
    assert block["remote"]["family"] == "fire" and block["remote"]["reasons"] == [
        "manufacturer_0171"
    ]
    assert block["hid"]["descriptor_hex"] == DESCRIPTOR.hex()
    assert not LOOSE_MAC.search(suggested)
    with patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "P"}):
        back = await flow.async_step_report({"report": "edited by the user"})
    assert back["type"] == "form" and back["step_id"] == "configure"


async def test_the_report_is_available_before_any_attempt(hass):
    flow = _flow(hass)
    shown = await flow.async_step_report()
    suggested = next(iter(shown["data_schema"].schema)).description["suggested_value"]
    assert json_block(suggested)["outcome"]["stage"] == "not_tried"


async def test_setup_records_how_the_remote_was_classified(hass):
    flow = _flow(hass)

    class Ok:
        async def open(self, *args):
            return object()

        async def close(self):
            pass

    with (
        patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "P"}),
        patch("custom_components.bt_remote_voice.config_flow.FireTransport", Ok),
    ):
        result = await flow.async_step_configure({"pipeline_id": "p"})
    detected = result["data"]["detected"]
    assert detected["reasons"] == ["manufacturer_0171"]
    assert detected["manufacturer"] == [{"company": "0x0171", "value": "0414027d00000008"}]
    assert detected["adapter"] == "hci0" and HID in detected["uuids"]
    assert "address" not in detected and "name" not in detected


async def test_diagnostics_include_the_report_and_tolerate_missing_inputs():
    from custom_components.bt_remote_voice.diagnostics import async_get_config_entry_diagnostics
    from tests.test_limits import _coordinator

    c = _coordinator({"remote_type": "auto"})
    c.caps = None
    c.last_error = "fire_voice_unsupported"
    entry = SimpleNamespace(
        runtime_data=c,
        options={"remote_type": "auto", "speaker": "media_player.secret"},
        data={
            "address": "A8:42:A7:11:22:33",
            "name": "AR",
            "family": "fire",
            "pipeline_id": "preferred",
            "detected": {
                "reasons": ["manufacturer_0171"],
                "uuids": [HID],
                "manufacturer": [{"company": "0x0171", "value": "0414027d00000008"}],
                "adapter": "hci0",
            },
        },
    )
    text = (await async_get_config_entry_diagnostics(None, entry))["remote_report"]
    block = json_block(text)
    assert (
        block["remote"]["family"] == "fire"
        and block["outcome"]["error"] == "fire_voice_unsupported"
    )
    assert "11:22:33" not in text and "secret" not in text
    assert block["remote"]["uuids"] == [HID] and block["adapter"] == {"name": "hci0"}
    assert block["remote"]["manufacturer"][0]["value"] == "0414027d00000008"
    bare = SimpleNamespace(runtime_data=c, options={}, data={})
    assert json_block((await async_get_config_entry_diagnostics(None, bare))["remote_report"])


def test_capture_script_is_read_only_and_masks_addresses():
    import subprocess

    script = Path(__file__).parents[1] / "scripts" / "capture_remote_info.sh"
    text = script.read_text()
    for forbidden in ("xxd", "gatttool", "menu gatt", "write", "printf", " > /dev", "tee "):
        assert forbidden not in text.replace("writes nothing", ""), forbidden
    assert "od -An -v -tx1" in text and "mask" in text
    assert subprocess.run(["sh", "-n", str(script)], check=False).returncode == 0
    expression = re.search(r"sed -E '([^']+)'", text).group(1)
    masked = subprocess.run(
        ["sed", "-E", expression],
        input="a8:42:a7:11:22:33 HID_UNIQ=A8-42-A7-11-22-33\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "11:22:33" not in masked and "11-22-33" not in masked
    assert masked.lower().count("a8:42:a7:xx:xx:xx") == 2


@pytest.mark.parametrize(
    "text",
    [
        "A8:42:A7:11:22:33A8:42:A7:11:22:34",  # two addresses glued together
        "fA8:42:A7:11:22:33",  # glued to hex characters on the left
        "cafeA8:42:A7:11:22:33",
        "name=0A8:42:A7:11:22:33",
        "a8:42:a7:11:22:33a8:42",  # glued to a fragment on the right
        "A8:42-A7:11:22:33",  # mixed separators
        "A8:42:A7:11-22-33",
        "A8_42:A7-11_22:33",
        "A8:42:A7:11:22:33:44:55",  # seven or more octets
        '{"HID_UNIQ":"a8:42:a7:11:22:33"}',
        "dev_A8_42_A7_11_22_33_dev_A8_42_A7_11_22_34",
    ],
)
def test_mask_address_survives_adversarial_shapes(text):
    masked = mask_address(text)
    assert not LOOSE_MAC.search(masked), masked
    assert "11:22:33" not in masked and "11_22_33" not in masked and "11-22-33" not in masked
    assert "11:22:34" not in masked and "A8:42:A7:xx:xx:xx" in masked


def test_the_known_address_is_masked_even_without_separators():
    text = mask_address("raw a842a7112233 and A842A7112233 ok", known=["A8:42:A7:11:22:33"])
    assert "112233" not in text and text.count("A8:42:A7:xx:xx:xx") == 2


def test_report_masks_a_bare_hex_address_taken_from_the_remote():
    data = ReportInput(
        remote={"address": "A8:42:A7:11:22:33", "name": "AR"},
        hid={"uevent": "HID_UNIQ=a842a7112233"},
        config={"note": "a842a7112233"},
    )
    assert "112233" not in build_report(data)


async def test_the_report_step_and_back_keeps_what_was_entered(hass):
    flow = _flow(hass)
    with patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "P"}):
        await flow.async_step_configure(
            {"pipeline_id": "p", "remote_type": "fire_press_to_talk", "show_report": True}
        )
        back = await flow.async_step_report({})
    schema = back["data_schema"].schema
    assert next(k for k in schema if k == "pipeline_id").default() == "p"
    assert next(k for k in schema if k == "remote_type").default() == "fire_press_to_talk"


async def test_the_report_before_any_attempt_shows_unknown_not_null(hass):
    shown = await _flow(hass).async_step_report()
    block = json_block(next(iter(shown["data_schema"].schema)).description["suggested_value"])
    assert block["remote"]["remote_type"] == "unknown"


def test_the_report_never_includes_the_remote_name_or_alias():
    remote = issue_1_remote().signals()
    remote["name"] = "John's TV remote"
    text = build_report(ReportInput(remote=remote))
    assert "John" not in text and '"name"' not in text


def test_the_byte_reversed_address_in_advertising_data_is_masked_too():
    data = ReportInput(
        remote={
            "address": "A8:42:A7:11:22:33",
            "manufacturer": [{"company": "0x0171", "value": "0414332211a742a8"}],
        }
    )
    text = build_report(data)
    assert "332211a742a8" not in text.lower() and "112233" not in text


@pytest.mark.parametrize(
    "form", ["a8 42 a7 11 22 33", "a842.a711.2233", "A8.42.A7.11.22.33", "33 22 11 a7 42 a8"]
)
def test_the_known_address_is_masked_with_spaces_or_dots_in_either_byte_order(form):
    text = mask_address(f"value {form} end", known=["A8:42:A7:11:22:33"])
    assert "xx" in text  # something was masked
    flat = re.sub(r"[\s.:_-]", "", text.lower())
    assert "112233" not in flat and "332211" not in flat
