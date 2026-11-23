"""The setup flow's "Test my remote" step, picker labels and forced-type end to end."""

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from custom_components.bt_remote_voice.bluez import Remote
from custom_components.bt_remote_voice.config_flow import ConfigFlow, remote_label
from custom_components.bt_remote_voice.hid_descriptor import KeyUsage
from custom_components.bt_remote_voice.observe import RESULTS, Observation

EN = json.loads(
    (
        Path(__file__).parents[1] / "custom_components/bt_remote_voice/translations/en.json"
    ).read_text()
)
HID = "00001812-0000-1000-8000-00805f9b34fb"
DESCRIPTOR = bytes.fromhex(
    (Path(__file__).parent / "fixtures" / "fire_0413_report_descriptor.hex").read_text().strip()
)


def flow_for(hass, family="fire"):
    flow = ConfigFlow()
    flow.hass = hass
    flow.selected = Remote(
        "A8:42:A7:11:22:33",
        "AR",
        "/org/bluez/hci0/dev_A8_42_A7_11_22_33",
        True,
        True,
        family,
        uuids=(HID,),
    )
    return flow


def fake_observe(result_obs):
    async def observe(address, family, seconds):
        observe.calls.append((address, family, seconds))
        await asyncio.sleep(0.01)  # a real listen takes seconds; HA starts tasks eagerly
        return result_obs

    observe.calls = []
    return observe


async def test_the_configure_form_offers_the_test_switch_off_by_default(hass):
    flow = flow_for(hass)
    with patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "P"}):
        result = await flow.async_step_configure()
    assert next(k for k in result["data_schema"].schema if k == "test_remote").default() is False


async def test_the_test_step_shows_progress_then_the_matching_result_and_returns(hass):
    flow = flow_for(hass)
    obs = Observation(family="fire", keys={KeyUsage(0x0C, 0x0221): 1}, nodes=1)
    observe = fake_observe(obs)
    with (
        patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "P"}),
        patch("custom_components.bt_remote_voice.config_flow.observe", observe),
    ):
        first = await flow.async_step_configure(
            {"pipeline_id": "p", "remote_type": "fire_press_to_talk", "test_remote": True}
        )
        assert first["type"] == "progress" and first["progress_action"] == "observing"
        await flow.observe_task
        done = await flow.async_step_test()
        assert done["type"] == "progress_done" and done["step_id"] == "test_result_fire_voice"
        shown = await flow.async_step_test_result_fire_voice()
        assert shown["type"] == "form" and shown["step_id"] == "test_result_fire_voice"
        back = await flow.async_step_test_result_fire_voice({})
    assert observe.calls == [("A8:42:A7:11:22:33", "fire", 20)]  # forced type drives the family
    assert back["step_id"] == "configure"
    schema = back["data_schema"].schema
    assert next(k for k in schema if k == "remote_type").default() == "fire_press_to_talk"


@pytest.mark.parametrize("kind", RESULTS)
async def test_every_result_has_a_step_with_translated_text(hass, kind):
    node = EN["config"]["step"][f"test_result_{kind}"]
    assert node["title"] and node["description"]
    flow = flow_for(hass)
    shown = await getattr(flow, f"async_step_test_result_{kind}")()
    assert shown["type"] == "form" and shown["step_id"] == f"test_result_{kind}"


async def test_the_progress_text_and_labels_exist_in_english():
    assert "20 seconds" in EN["config"]["progress"]["observing"]
    assert EN["config"]["step"]["configure"]["data"]["test_remote"] == "Test my remote"


async def test_the_observation_appears_in_the_report_as_counts_and_usages_only(hass):
    flow = flow_for(hass)
    flow.observation = Observation(
        seconds=20, family="fire", keys={KeyUsage(0x0C, 0x0221): 2}, nodes=1, errors=["wake_remote"]
    )
    shown = await flow.async_step_report()
    text = next(iter(shown["data_schema"].schema)).description["suggested_value"]
    block = json.loads(re.search(r"```json\n(.*?)\n```", text, re.S).group(1))
    assert block["observation"]["result"] == "fire_voice"
    assert block["observation"]["keys"] == [{"page": "0x000c", "usage": "0x0221", "presses": 2}]
    assert block["observation"]["errors"] == ["wake_remote"]


async def test_removing_the_flow_cancels_a_running_listen(hass):
    flow = flow_for(hass)
    started = asyncio.Event()

    async def slow(address, family, seconds):
        started.set()
        await asyncio.sleep(30)

    with patch("custom_components.bt_remote_voice.config_flow.observe", slow):
        await flow.async_step_test()
        await started.wait()
        task = flow.observe_task
        flow.async_remove()
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert task.cancelled()


def test_picker_labels_are_data_only_with_the_product_id_when_known():
    fire = Remote("02:00:00:00:00:01", "AR", "/p", True, True, "fire", product_id=0x0421)
    atvv = Remote("02:00:00:00:00:02", "BT19K", "/p", True, True)
    assert remote_label(fire) == "AR (02:00:00:00:00:01) [0x0421]"
    assert remote_label(atvv) == "BT19K (02:00:00:00:00:02)"  # no UI wording built in code


async def test_forced_fire_on_a_remote_without_voice_reports_is_refused_end_to_end(hass):
    """The real transport, a keyboard-only descriptor and a forced Fire type: no write, clear error."""
    from custom_components.bt_remote_voice.fire import FireTransport

    keyboard_only = bytes.fromhex(
        "05010906a101850195037508150025ff19002 9ff8100c0".replace(" ", "")
    )
    writes, opened = [], []

    class Device:
        def write(self, data):
            writes.append(data)

        def close(self):
            pass

    def make():
        return FireTransport(
            opener=lambda path: opened.append(path) or Device(),
            find_all=lambda address: ["/dev/hidraw7"],
            read_descriptor=lambda node: keyboard_only,
            refuse_after_seconds=0.05,
            poll_seconds=0.01,
        )

    flow = flow_for(hass, family="atvv")
    with (
        patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "P"}),
        patch("custom_components.bt_remote_voice.config_flow.FireTransport", make),
    ):
        result = await flow.async_step_configure(
            {"pipeline_id": "p", "remote_type": "fire_press_to_talk"}
        )
    assert result["errors"] == {"base": "fire_voice_unsupported"}
    assert writes == [] and opened == []


async def test_a_failing_listen_still_ends_on_a_result_screen(hass):
    flow = flow_for(hass)

    async def broken(address, family, seconds):
        await asyncio.sleep(0.01)
        raise RuntimeError("boom")

    with patch("custom_components.bt_remote_voice.config_flow.observe", broken):
        first = await flow.async_step_test()
        assert first["type"] == "progress"
        await asyncio.gather(flow.observe_task, return_exceptions=True)
        done = await flow.async_step_test()
    assert done["type"] == "progress_done" and done["step_id"] == "test_result_nothing"
    assert flow.observation.errors == ["bluetooth_unavailable"]


async def test_the_report_uses_the_chosen_type_and_the_tested_descriptor(hass):
    flow = flow_for(hass, family="atvv")  # classified ATVV: no transport descriptor exists
    flow.form = {"remote_type": "fire_press_to_talk"}
    flow.observation = Observation(
        family="fire", nodes=1, descriptors={"/dev/hidraw3": DESCRIPTOR.hex()}
    )
    shown = await flow.async_step_report()
    text = next(iter(shown["data_schema"].schema)).description["suggested_value"]
    block = json.loads(re.search(r"```json\n(.*?)\n```", text, re.S).group(1))
    assert block["remote"]["remote_type"] == "fire_press_to_talk"
    assert (
        block["hid"]["descriptor_hex"] == DESCRIPTOR.hex() and block["hid"]["voice_reports"] is True
    )
    assert block["hid"]["nodes"] == 1


async def test_a_stale_attempt_with_another_remote_type_is_not_reported(hass):
    flow = flow_for(hass)
    flow.attempt = {
        "error": "fire_voice_unsupported",
        "remote_type": "auto",
        "reports": (),
        "nodes": 3,
    }
    flow.form = {"remote_type": "fire_press_to_talk"}  # changed after that attempt
    shown = await flow.async_step_report()
    block = json.loads(
        re.search(
            r"```json\n(.*?)\n```",
            next(iter(shown["data_schema"].schema)).description["suggested_value"],
            re.S,
        ).group(1)
    )
    assert block["remote"]["remote_type"] == "fire_press_to_talk"
    assert (
        block["outcome"]["stage"] == "not_tried"
        and block["outcome"]["error"] != "fire_voice_unsupported"
    )


async def test_the_reported_0414_remote_passes_setup_through_the_real_transport(hass):
    """Issue #1 end to end: real FireTransport, the reporter's descriptor, forced nothing."""
    from custom_components.bt_remote_voice.fire import FireTransport

    descriptor = bytes.fromhex(
        (Path(__file__).parent / "fixtures" / "fire_0414_report_descriptor.hex").read_text().strip()
    )
    opened, writes = [], []

    class Device:
        def write(self, data):
            writes.append(data)

        def close(self):
            pass

    def make():
        return FireTransport(
            opener=lambda path: opened.append(path) or Device(),
            find_all=lambda address: ["/dev/hidraw6"],
            read_descriptor=lambda node: descriptor,
        )

    flow = flow_for(hass, family="fire")
    with (
        patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "P"}),
        patch("custom_components.bt_remote_voice.config_flow.FireTransport", make),
    ):
        result = await flow.async_step_configure({"pipeline_id": "p"})
    assert result["type"] == "create_entry" and result["data"]["family"] == "fire"
    assert opened == ["/dev/hidraw6"] and writes == []  # setup only opens the node
