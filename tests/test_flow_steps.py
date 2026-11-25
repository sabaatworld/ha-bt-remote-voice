"""Pausing the live remote for a test, and the shared test/clip screens."""

import asyncio
import json
import re
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.bt_remote_voice.bluez import Remote
from custom_components.bt_remote_voice.clips import Clip, SavedClips
from custom_components.bt_remote_voice.config_flow import ConfigFlow
from custom_components.bt_remote_voice.flow_steps import (
    PauseRefused,
    configured_report,
    paused_entry,
)
from custom_components.bt_remote_voice.hid_descriptor import KeyUsage
from custom_components.bt_remote_voice.observe import Observation

ADDRESS = "A8:42:A7:11:22:33"
HID = "00001812-0000-1000-8000-00805f9b34fb"


def make_entry(state=ConfigEntryState.LOADED):
    return SimpleNamespace(state=state, entry_id="e1", title="AR", data={}, options={})


def wire(hass, entry, *, unload_ok=True, unload_delay=0):
    """Replace the config entries' unload/setup with fakes that move ``entry.state``."""
    calls = []

    async def unload(entry_id):
        calls.append("unload")
        await asyncio.sleep(unload_delay)
        if unload_ok:
            entry.state = ConfigEntryState.NOT_LOADED
        return unload_ok

    async def setup(entry_id):
        calls.append("setup")
        entry.state = ConfigEntryState.LOADED
        return True

    hass.config_entries.async_unload = unload
    hass.config_entries.async_setup = setup
    return calls


async def test_paused_entry_unloads_and_restores(hass):
    entry = make_entry()
    calls = wire(hass, entry)
    async with paused_entry(hass, ADDRESS, entry):
        assert calls == ["unload"] and entry.state is ConfigEntryState.NOT_LOADED
    assert calls == ["unload", "setup"] and entry.state is ConfigEntryState.LOADED


async def test_restore_on_exception(hass):
    entry = make_entry()
    calls = wire(hass, entry)
    with pytest.raises(RuntimeError):
        async with paused_entry(hass, ADDRESS, entry):
            raise RuntimeError("boom")
    assert calls == ["unload", "setup"]


async def test_cancel_while_the_test_runs_restores_once(hass):
    entry = make_entry()
    calls = wire(hass, entry)
    started = asyncio.Event()

    async def body():
        async with paused_entry(hass, ADDRESS, entry):
            started.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(body())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["unload", "setup"] and entry.state is ConfigEntryState.LOADED


async def test_cancel_during_unload_still_restores_once(hass):
    entry = make_entry()
    calls = wire(hass, entry, unload_delay=0.05)

    async def body():
        async with paused_entry(hass, ADDRESS, entry):
            await asyncio.sleep(30)

    task = asyncio.create_task(body())
    await asyncio.sleep(0.01)  # inside async_unload
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await hass.async_block_till_done()
    assert calls == ["unload", "setup"] and entry.state is ConfigEntryState.LOADED


@pytest.mark.parametrize("state", [ConfigEntryState.NOT_LOADED, None])
async def test_nothing_unloaded_or_restored_when_there_is_no_live_entry(hass, state):
    entry = make_entry(state) if state else None
    calls = wire(hass, entry or make_entry())
    async with paused_entry(hass, ADDRESS, entry):
        pass
    assert calls == []


@pytest.mark.parametrize("state", [ConfigEntryState.SETUP_RETRY, ConfigEntryState.SETUP_ERROR])
async def test_recoverable_states_are_unloaded_and_restored(hass, state):
    entry = make_entry(state)
    calls = wire(hass, entry)
    async with paused_entry(hass, ADDRESS, entry):
        pass
    assert calls == ["unload", "setup"]


@pytest.mark.parametrize(
    "state", [ConfigEntryState.SETUP_IN_PROGRESS, ConfigEntryState.UNLOAD_IN_PROGRESS]
)
async def test_in_progress_states_are_refused(hass, state):
    entry = make_entry(state)
    calls = wire(hass, entry)
    with pytest.raises(PauseRefused, match="test_running"):
        async with paused_entry(hass, ADDRESS, entry):
            pass
    assert calls == []


async def test_unload_failure_refuses_and_does_not_set_up(hass):
    entry = make_entry()
    calls = wire(hass, entry, unload_ok=False)
    with pytest.raises(PauseRefused, match="pause_failed"):
        async with paused_entry(hass, ADDRESS, entry):
            pass
    assert calls == ["unload"]
    async with paused_entry(hass, ADDRESS, None):  # and the address is free again
        pass


async def test_second_test_for_the_same_remote_is_refused_until_the_first_ends(hass):
    entry = make_entry()
    wire(hass, entry)
    async with paused_entry(hass, ADDRESS, entry):
        with pytest.raises(PauseRefused, match="test_running"):
            async with paused_entry(hass, ADDRESS, None):
                pass
    async with paused_entry(hass, ADDRESS, entry):
        pass


async def test_restore_skipped_when_the_entry_is_no_longer_unloaded(hass, caplog):
    entry = make_entry()
    calls = wire(hass, entry)
    async with paused_entry(hass, ADDRESS, entry):
        entry.state = ConfigEntryState.LOADED  # something else brought it back
    assert calls == ["unload"]
    assert "Not restoring" in caplog.text


# ---- the screens ----


def flow_for(hass):
    flow = ConfigFlow()
    flow.hass = hass
    flow.selected = Remote(ADDRESS, "AR", "/org/bluez/hci0/dev_x", True, True, "fire", uuids=(HID,))
    return flow


def run_patches(observation, clips):
    async def fake_observe(address, family, seconds, **kwargs):
        return observation

    async def fake_record(address, family, seconds, **kwargs):
        return clips

    return (
        patch("custom_components.bt_remote_voice.flow_steps.observe", fake_observe),
        patch("custom_components.bt_remote_voice.flow_steps.record_test", fake_record),
    )


async def finish_test(flow):
    first = await flow.async_step_test()
    assert first["type"] == "progress"
    await flow.observe_task
    return await flow.async_step_test()


@pytest.fixture(autouse=True)
def pipelines():
    with patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "P"}):
        yield


def pressed():
    return Observation(family="fire", keys={KeyUsage(0x0C, 0x0221): 1}, nodes=1)


async def test_result_then_clip_screen_lists_signed_links(hass, tmp_path):
    from homeassistant.components.http.auth import STORAGE_KEY

    hass.data[STORAGE_KEY] = "content-user"
    hass.config.media_dirs = {"local": str(tmp_path)}
    flow = flow_for(hass)
    clip = Clip(datetime(2026, 11, 25, 14, 30, 7), b"\x01\x00" * 3200)
    a, b = run_patches(pressed(), [clip])
    with a, b:
        done = await finish_test(flow)
    assert done["type"] == "progress_done" and flow.observation.clip_saved is True
    screen = await flow.async_step_test_result_fire_voice({})
    assert screen["step_id"] == "test_clips"
    links = screen["description_placeholders"]["clips"]
    assert (
        "/media/local/Bluetooth%20Remote%20Voice/Test%20clip%202026-11-25%2014-30-07.wav?" in links
    )
    assert (tmp_path / "Bluetooth Remote Voice").is_dir()
    back = await flow.async_step_test_clips({})
    assert back["step_id"] == "configure"


async def test_no_media_folder_screen_says_no_clip_saved_and_test_still_completes(hass):
    hass.config.media_dirs = {}
    flow = flow_for(hass)
    a, b = run_patches(pressed(), [Clip(datetime(2026, 11, 25, 14, 30, 7), b"\x01\x00" * 3200)])
    with a, b:
        done = await finish_test(flow)
    assert done["step_id"] == "test_result_fire_voice" and flow.observation.clip_saved is False
    assert (await flow.async_step_test_result_fire_voice({}))["step_id"] == "test_clips_none"


async def test_a_remote_that_was_not_pressed_goes_straight_back(hass):
    flow = flow_for(hass)
    a, b = run_patches(Observation(family="fire"), [])
    with a, b:
        done = await finish_test(flow)
    assert done["step_id"] == "test_result_nothing"
    assert (await flow.async_step_test_result_nothing({}))["step_id"] == "configure"


async def test_a_refused_pause_aborts_with_its_reason(hass):
    flow = flow_for(hass)
    entry = make_entry()
    wire(hass, entry)
    flow.test_entry = lambda: entry
    a, b = run_patches(pressed(), [])
    running = hass.data.setdefault("bt_remote_voice", {}).setdefault("tests", set())
    running.add(ADDRESS)  # another test of this remote is running
    with a, b:
        first = await flow.async_step_test()
        if first["type"] == "progress":
            await flow.observe_task
            first = await flow.async_step_test()
    assert first["type"] == "abort" and first["reason"] == "test_running"


def test_report_for_a_configured_remote_needs_no_setup_state():
    entry = SimpleNamespace(
        data={
            "address": ADDRESS,
            "name": "AR",
            "family": "fire",
            "detected": {"vendor_id": "0x0171"},
        },
        options={"remote_type": "auto"},
    )
    obs = Observation(family="fire", keys={KeyUsage(0x0C, 0x0221): 1}, nodes=1, clip_saved=True)
    text = configured_report(entry, obs)
    block = json.loads(re.search(r"```json\n(.*?)\n```", text, re.S).group(1))
    assert block["schema"] == 2 and block["observation"]["clip_saved"] is True
    assert block["outcome"]["stage"] == "not_tried"
    assert ADDRESS not in text  # masked


# ---- the Configure menu of an already-configured remote ----


def options_flow(hass, family="fire", **extra):
    from custom_components.bt_remote_voice.config_flow import OptionsFlow

    entry = SimpleNamespace(
        state=ConfigEntryState.NOT_LOADED,
        entry_id="e1",
        title="AR",
        data={"address": ADDRESS, "name": "AR", "pipeline_id": "p", "family": family},
        options={"remote_type": "auto", **extra},
    )
    flow = OptionsFlow(entry)
    flow.hass = hass
    return flow


async def test_options_init_is_a_menu_with_the_three_actions(hass):
    result = await options_flow(hass).async_step_init()
    assert result["type"] == "menu" and result["step_id"] == "init"
    assert result["menu_options"] == ["settings", "test", "report"]


async def test_options_test_runs_for_the_saved_remote_and_returns_to_the_menu(hass, tmp_path):
    hass.config.media_dirs = {"local": str(tmp_path)}
    calls = []

    async def fake_observe(address, family, seconds, **kwargs):
        calls.append((address, family))
        return pressed()

    async def fake_record(address, family, seconds, **kwargs):
        calls.append({"min_press_seconds": kwargs["min_press_seconds"]})
        return []

    flow = options_flow(hass, min_press_seconds=0.2)
    with (
        patch("custom_components.bt_remote_voice.flow_steps.observe", fake_observe),
        patch("custom_components.bt_remote_voice.flow_steps.record_test", fake_record),
    ):
        done = await finish_test(flow)
        assert done["step_id"] == "test_result_fire_voice"
        none = await flow.async_step_test_result_fire_voice({})
        back = await flow.async_step_test_clips_none({})
    assert calls == [(ADDRESS, "fire"), {"min_press_seconds": 0.2}]  # the entry's own filter
    assert none["step_id"] == "test_clips_none"
    assert back["type"] == "menu" and back["step_id"] == "init"


async def test_options_report_shows_the_report_and_returns_to_the_menu(hass):
    flow = options_flow(hass)
    shown = await flow.async_step_report()
    text = next(iter(shown["data_schema"].schema)).description["suggested_value"]
    assert '"schema": 2' in text
    assert (await flow.async_step_report({}))["type"] == "menu"


async def test_setup_of_an_already_configured_remote_still_aborts(hass):
    from types import MappingProxyType

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.data_entry_flow import AbortFlow

    entry = ConfigEntry(
        domain="bt_remote_voice",
        title="AR",
        version=1,
        minor_version=1,
        data={"address": ADDRESS},
        options={},
        source="user",
        unique_id=ADDRESS,
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
    )
    hass.config_entries._entries[entry.entry_id] = entry
    flow = flow_for(hass)
    flow.handler, flow.context = "bt_remote_voice", {"source": "user"}
    with pytest.raises(AbortFlow, match="already_configured"):
        await flow.choose(flow.selected)


async def test_setup_test_also_records_and_shows_the_clip(hass, tmp_path):
    hass.config.media_dirs = {"local": str(tmp_path)}
    from homeassistant.components.http.auth import STORAGE_KEY

    hass.data[STORAGE_KEY] = "x"
    flow = flow_for(hass)
    flow.form = {"pipeline_id": "p", "remote_type": "auto"}
    clip = Clip(datetime(2026, 11, 25, 14, 30, 7), b"\x01\x00" * 3200)
    a, b = run_patches(pressed(), [clip])
    with a, b:
        await finish_test(flow)
    screen = await flow.async_step_test_result_fire_voice({})
    assert screen["step_id"] == "test_clips"


# ---- the test follows the saved settings and tells what it noticed ----


def test_the_options_flow_test_records_with_the_saved_settings(hass):
    flow = options_flow(
        hass, "atvv", min_press_seconds=0.2, ignore_early_stop=True, gap_tolerance_ms=240
    )
    assert flow.recording_settings() == {
        "min_press_seconds": 0.2,
        "ignore_early_stop": True,
        "early_stop_applies": True,
        "max_gap_ms": 240,
    }


def test_the_test_ignores_early_stop_where_a_real_request_would(hass):
    fire = options_flow(hass, "fire", ignore_early_stop=True, gap_tolerance_ms=240)
    assert (
        fire.recording_settings()["ignore_early_stop"] is False
    )  # a Fire STOP is the real release
    assert fire.recording_settings()["early_stop_applies"] is False
    held = options_flow(hass, "atvv", ignore_early_stop=True, input_mode="conversation")
    assert held.recording_settings()["ignore_early_stop"] is False  # conversation manages turns
    assert held.recording_settings()["early_stop_applies"] is False


def test_unusable_saved_settings_mean_off(hass):
    flow = options_flow(
        hass, "atvv", min_press_seconds="x", ignore_early_stop="yes", gap_tolerance_ms=9999
    )
    assert flow.recording_settings() == {
        "min_press_seconds": 0,
        "ignore_early_stop": False,
        "early_stop_applies": True,
        "max_gap_ms": 0,
    }


def test_setup_records_with_the_defaults(hass):
    assert flow_for(hass).recording_settings() == {
        "min_press_seconds": 0,
        "ignore_early_stop": False,
        "early_stop_applies": False,  # the fake remote here is a Fire remote
        "max_gap_ms": 0,
    }


async def test_what_the_test_noticed_is_shown_after_the_clips_only_when_something_was(hass):
    flow = flow_for(hass)
    flow.saved = SavedClips()
    flow.observation = pressed()
    flow.observation.early_stops = 2
    flow.observation.audio_gaps = 1
    shown = await flow.async_step_test_clips_none({})
    assert shown["step_id"] == "test_hints"
    assert shown["description_placeholders"] == {"early": "2", "gaps": "1"}
    assert (await flow.async_step_test_hints({}))["step_id"] == "configure"
    flow.observation.early_stops = flow.observation.audio_gaps = 0
    assert (await flow.async_step_test_clips_none({}))["step_id"] == "configure"


def test_only_problems_the_settings_did_not_handle_are_reported():
    import time

    from custom_components.bt_remote_voice.observe import merge_recording

    obs = Observation(family="atvv", started=time.monotonic())
    merge_recording(
        obs,
        [],
        [],
        [],
        {
            "early_stops": 3,
            "early_stops_ignored": 3,
            "gaps": 2,
            "gaps_covered": 1,
            "lost_frames": 9,
        },
    )
    assert (obs.early_stops, obs.early_stops_handled) == (0, 3)  # nothing left to act on
    assert (obs.audio_gaps, obs.gaps_handled) == (1, 1)  # one gap was longer than the setting
