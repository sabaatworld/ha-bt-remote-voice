import asyncio
from types import SimpleNamespace

import pytest

from custom_components.bt_remote_voice.preferences import (
    CONVERSATION_RANGE,
    DEFAULTS,
    FORM_ORDER,
    REQUEST_RANGE,
    capture_buffer_bytes,
    preferences,
)
from custom_components.bt_remote_voice.protocol import Capabilities
from custom_components.bt_remote_voice.session import AudioFailure
from tests.test_update import _coordinator


def test_defaults_preserve_today():
    assert DEFAULTS["max_request_seconds"] == 30
    assert DEFAULTS["max_conversation_seconds"] == 120
    assert REQUEST_RANGE == (5, 120, 5)
    assert CONVERSATION_RANGE == (30, 600, 10)
    result = preferences({})
    assert result["max_request_seconds"] == 30 and result["max_conversation_seconds"] == 120


@pytest.mark.parametrize("bad", [4, 121, "30", True, None])
def test_request_range_is_validated(bad):
    with pytest.raises(ValueError, match="invalid_preferences"):
        preferences({"max_request_seconds": bad})


@pytest.mark.parametrize("bad", [29, 601, "120", False, None])
def test_conversation_range_is_validated(bad):
    with pytest.raises(ValueError, match="invalid_preferences"):
        preferences({"max_conversation_seconds": bad})


def test_range_edges_are_accepted():
    assert preferences({"max_request_seconds": 5, "max_conversation_seconds": 30})
    assert preferences({"max_request_seconds": 30, "max_conversation_seconds": 30})
    assert preferences({"max_request_seconds": 120, "max_conversation_seconds": 600})


def test_conversation_shorter_than_request_is_rejected():
    with pytest.raises(ValueError, match="invalid_preferences"):
        preferences({"max_request_seconds": 60, "max_conversation_seconds": 40})


def test_capture_buffer_bytes():
    # 16 kHz 16-bit mono plus one second of headroom (frames arrive in bursts), never below 960000.
    assert capture_buffer_bytes(5) == 960000
    assert capture_buffer_bytes(30) == 992000
    assert capture_buffer_bytes(120) == 3872000
    assert capture_buffer_bytes(0.3) == 960000


def test_form_order_names_every_known_option():
    extras = {"pipeline_id", "mode", "speaker", "reply_condition"}
    assert set(FORM_ORDER) == set(DEFAULTS) | extras
    assert FORM_ORDER == (
        "pipeline_id",
        "mode",
        "speaker",
        "reply_condition",
        "output_mode",
        "stream_head_start",
        "busy_processing",
        "busy_playback",
        "context_mode",
        "input_mode",
        "min_press_seconds",
        "max_request_seconds",
        "max_conversation_seconds",
    )


# ---- C2: applying the limits to capture ----


def _speak(c, frames, pause=0.0):
    held = c.capture.held
    held.vad = SimpleNamespace(Process10ms=lambda pcm: 1)
    return held


async def test_single_hold_ends_at_configured_request_limit():
    c = _coordinator({"max_request_seconds": 0.3})
    c.caps = Capabilities(128)
    c.run_pipeline = lambda s: asyncio.sleep(5)
    c.control(bytes.fromhex("04030201"))
    c.audio(bytes(128))
    assert c.session.error is None
    await asyncio.sleep(0.45)
    assert c.session.error == "capture_limit"
    await c.stop()


async def test_alternating_hold_ends_at_configured_conversation_limit():
    c = _coordinator({"input_mode": "conversation", "max_conversation_seconds": 0.4})
    c.caps = Capabilities(128)
    c.run_pipeline = lambda s: asyncio.sleep(5)
    c.control(bytes.fromhex("04030201"))
    c.audio(bytes(128))
    await asyncio.sleep(0.55)
    assert c.session.error == "capture_limit"
    await c.stop()


async def test_held_turn_ends_at_request_limit_with_a_draining_reader():
    c = _coordinator({"input_mode": "conversation", "max_request_seconds": 0.3})
    c.caps = Capabilities(128)
    results = []

    async def run(s):
        try:
            results.append(len([x async for x in s.stream()]))
        except AudioFailure as err:
            results.append(str(err))
            raise

    c.run_pipeline = run
    c.control(bytes.fromhex("04030201"))
    held = _speak(c, 0)
    for _ in range(60):  # continuous speech for ~0.6 s into one logical turn
        c.audio(bytes(128))
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.2)
    assert "capture_limit" in results, results
    assert held.current is None or held.current.error is None
    await c.stop()


async def test_held_turn_timer_is_cancelled_on_release():
    from custom_components.bt_remote_voice.held import Utterance

    u = Utterance(max_seconds=0.2)
    u.feed(b"x" * 320)
    assert u.limit is not None and not u.limit.cancelled()
    u.release()
    assert u.limit.cancelled()  # the timer itself is gone, not just ignored when it fires
    await asyncio.sleep(0.3)
    assert u.error is None and u.finished


async def test_defaults_unchanged_for_capture():
    c = _coordinator()
    c.caps = Capabilities(128)
    c.run_pipeline = lambda s: asyncio.sleep(5)
    assert c.limits() == (30, 120)
    c.control(bytes.fromhex("04030201"))
    assert c.session.max_bytes == capture_buffer_bytes(30)  # the limit plus a second of headroom
    await c.stop()


async def test_diagnostics_report_limits():
    from custom_components.bt_remote_voice.diagnostics import async_get_config_entry_diagnostics

    c = _coordinator({"max_request_seconds": 45, "max_conversation_seconds": 200})
    c.caps = None
    entry = SimpleNamespace(runtime_data=c, options=c.entry.options, data=c.entry.data)
    data = await async_get_config_entry_diagnostics(None, entry)
    assert data["limits"] == {"max_request_seconds": 45, "max_conversation_seconds": 200}


# ---- C3: options form order, labels, README ----
async def _options_form(hass, options=None):
    from custom_components.bt_remote_voice.config_flow import OptionsFlow

    entry = SimpleNamespace(
        data={"pipeline_id": "p", "name": "Test"}, options=options or {}, title="Test"
    )
    flow = OptionsFlow(entry)
    flow.hass = hass
    from unittest.mock import patch

    with patch(
        "custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "Pipeline"}
    ):
        return flow, await flow.async_step_init()


async def test_options_form_field_order(hass):
    _flow, result = await _options_form(hass)
    keys = [str(k) for k in result["data_schema"].schema]
    assert set(keys) <= set(FORM_ORDER)
    assert keys == [k for k in FORM_ORDER if k in keys]
    assert {"max_request_seconds", "max_conversation_seconds"} <= set(keys)


async def test_old_saved_options_still_open_the_form(hass):
    legacy = {"mode": "commands", "audible_feedback": True, "output_mode": "auto"}
    _flow, result = await _options_form(hass, legacy)
    assert result["type"] == "form"
    defaults = {str(k): k.default() for k in result["data_schema"].schema if hasattr(k, "default")}
    assert defaults["max_request_seconds"] == 30
    assert defaults["max_conversation_seconds"] == 120


async def test_conversation_shorter_than_request_is_an_options_error(hass):
    from unittest.mock import patch

    flow, _ = await _options_form(hass)
    with patch(
        "custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "Pipeline"}
    ):
        result = await flow.async_step_init(
            {
                "pipeline_id": "p",
                "mode": "commands",
                "speaker": "none",
                "max_request_seconds": 90,
                "max_conversation_seconds": 60,
            }
        )
    assert result["errors"] == {"base": "invalid_preferences"}


def test_readme_options_table_follows_form_order():
    import json
    import re
    from pathlib import Path

    root = Path(__file__).parents[1]
    labels = json.loads(
        (root / "custom_components/bt_remote_voice/translations/en.json").read_text()
    )["options"]["step"]["init"]["data"]
    readme = (root / "README.md").read_text()
    table = readme.split("### Options")[1].split("\n\n")[1]
    rows = [re.match(r"\| ([^|]+) \|", line).group(1).strip() for line in table.splitlines()[2:]]
    expected = [labels[k] for k in FORM_ORDER if k in labels]
    assert rows == expected, (rows, expected)


async def test_request_limit_scales_the_session_buffer():
    c = _coordinator({"max_request_seconds": 120, "max_conversation_seconds": 300})
    c.caps = Capabilities(128)
    c.run_pipeline = lambda s: asyncio.sleep(5)
    c.control(bytes.fromhex("04030201"))
    assert c.session.max_bytes == capture_buffer_bytes(120) == 3872000
    await c.stop()


async def test_stop_aborts_the_open_held_turn_and_its_timer():
    c = _coordinator({"input_mode": "conversation", "max_request_seconds": 30})
    c.caps = Capabilities(128)
    c.run_pipeline = lambda s: asyncio.sleep(5)
    c.control(bytes.fromhex("04030201"))
    held = c.capture.held
    held.vad = SimpleNamespace(Process10ms=lambda pcm: 1)
    for _ in range(5):
        c.audio(bytes(128))
    turn = held.current
    assert turn is not None and turn.limit is not None
    await c.stop()
    assert held.current is None and turn.limit.cancelled()
    assert c.capture.turns.qsize() == 0


def test_min_press_seconds_default_range_and_rejections():
    from custom_components.bt_remote_voice.preferences import DEFAULTS, PRESS_RANGE

    assert DEFAULTS["min_press_seconds"] == 0 and PRESS_RANGE == (0, 0.5, 0.05)
    for good in (0, 0.1, 0.5):
        assert preferences({"min_press_seconds": good})["min_press_seconds"] == good
    for bad in (-0.05, 0.55, True, "0.1", None):
        with pytest.raises(ValueError):
            preferences({"min_press_seconds": bad})
