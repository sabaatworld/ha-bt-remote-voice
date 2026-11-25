"""Remote type: auto uses the classification recorded at setup; a forced type wins."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.bt_remote_voice.config_flow import ConfigFlow, OptionsFlow
from custom_components.bt_remote_voice.preferences import (
    CHOICES,
    DEFAULTS,
    FORM_ORDER,
    preferences,
)
from custom_components.bt_remote_voice.remote_types import (
    DEFAULT_REMOTE_TYPE,
    REMOTE_TYPES,
    transport_family,
)


def test_remote_types_are_behaviors_not_models():
    assert REMOTE_TYPES == ("auto", "atvv", "fire_press_to_talk")
    assert DEFAULT_REMOTE_TYPE == "auto"
    assert CHOICES["remote_type"] == REMOTE_TYPES
    assert DEFAULTS["remote_type"] == "auto" and "remote_type" in FORM_ORDER


@pytest.mark.parametrize(
    ("options", "data", "expected"),
    [
        ({"remote_type": "fire_press_to_talk"}, {"family": "atvv"}, "fire"),
        ({"remote_type": "atvv"}, {"family": "fire"}, "atvv"),
        ({"remote_type": "auto"}, {"family": "fire"}, "fire"),
        ({"remote_type": "auto"}, {"family": "atvv"}, "atvv"),
        ({}, {"family": "fire"}, "fire"),  # entries from before this option
        ({}, {}, "atvv"),  # nothing recorded: today's default
        ({"remote_type": "bogus"}, {"family": "fire"}, "fire"),  # unknown value = auto
    ],
)
def test_transport_family_precedence(options, data, expected):
    assert transport_family(options, data) == expected


def test_preferences_default_and_validate_remote_type():
    assert preferences({})["remote_type"] == "auto"
    assert preferences({"remote_type": "fire_press_to_talk"})["remote_type"] == "fire_press_to_talk"
    with pytest.raises(ValueError, match="invalid_preferences"):
        preferences({"remote_type": "bogus"})


async def _setup(hass, *, classified, user_input):
    """Run the configure step for a remote classified as ``classified`` and return the result."""
    flow = ConfigFlow()
    flow.hass = hass
    flow.selected = SimpleNamespace(address="02:00:00:00:00:01", name="R", family=classified)
    used = []

    def fake(kind):
        class Fake:
            async def open(self, *args):
                used.append(kind)
                return object()

            async def close(self):
                pass

        return Fake

    with (
        patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "P"}),
        patch("custom_components.bt_remote_voice.config_flow.FireTransport", fake("fire")),
        patch("custom_components.bt_remote_voice.config_flow.BlueZTransport", fake("atvv")),
    ):
        return await flow.async_step_configure(user_input), used


async def test_setup_form_offers_remote_type_defaulting_to_auto(hass):
    flow = ConfigFlow()
    flow.hass = hass
    flow.selected = SimpleNamespace(address="02:00:00:00:00:01", name="R", family="atvv")
    with patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "P"}):
        result = await flow.async_step_configure()
    key = next(k for k in result["data_schema"].schema if k == "remote_type")
    assert key.default() == "auto"
    assert list(result["data_schema"].schema[key].config["options"]) == list(REMOTE_TYPES)


async def test_auto_uses_the_classification_and_records_it(hass):
    result, used = await _setup(
        hass, classified="fire", user_input={"pipeline_id": "p", "remote_type": "auto"}
    )
    assert used == ["fire"]
    assert result["data"]["family"] == "fire"
    assert result["options"]["remote_type"] == "auto"


async def test_setup_can_force_fire_on_a_remote_classified_atvv(hass):
    result, used = await _setup(
        hass,
        classified="atvv",
        user_input={"pipeline_id": "p", "remote_type": "fire_press_to_talk"},
    )
    assert used == ["fire"]
    assert result["data"]["family"] == "atvv"  # the classification stays what was observed
    assert result["options"]["remote_type"] == "fire_press_to_talk"


async def test_forced_atvv_on_a_fire_remote_uses_the_atvv_transport(hass):
    _, used = await _setup(
        hass, classified="fire", user_input={"pipeline_id": "p", "remote_type": "atvv"}
    )
    assert used == ["atvv"]


async def test_a_failed_forced_setup_stays_on_the_form_so_the_type_can_be_changed(hass):
    flow = ConfigFlow()
    flow.hass = hass
    flow.selected = SimpleNamespace(address="02:00:00:00:00:01", name="R", family="atvv")

    class Refuses:
        async def open(self, *args):
            raise ValueError("voice_service_unavailable")

        async def close(self):
            pass

    with (
        patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "P"}),
        patch("custom_components.bt_remote_voice.config_flow.BlueZTransport", Refuses),
    ):
        result = await flow.async_step_configure({"pipeline_id": "p", "remote_type": "atvv"})
    assert result["type"] == "form" and result["step_id"] == "configure"
    assert result["errors"] == {"base": "voice_service_unavailable"}


def _options(hass, options):
    flow = OptionsFlow(SimpleNamespace(data={"pipeline_id": "p1"}, options=options))
    flow.hass = hass
    return flow


async def test_options_show_remote_type_defaulting_to_auto_and_keep_a_saved_value(hass):
    for saved, expected in ((None, "auto"), ("fire_press_to_talk", "fire_press_to_talk")):
        flow = _options(hass, {"remote_type": saved} if saved else {})
        with patch(
            "custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p1": "P"}
        ):
            result = await flow.async_step_settings()
        key = next(k for k in result["data_schema"].schema if k == "remote_type")
        assert key.default() == expected


async def test_options_merge_does_not_overwrite_a_saved_remote_type_with_the_default(hass):
    flow = _options(hass, {"remote_type": "fire_press_to_talk"})
    with patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p1": "P"}):
        result = await flow.async_step_settings({"pipeline_id": "p1", "mode": "commands"})
    assert result["data"]["remote_type"] == "fire_press_to_talk"


def test_form_order_puts_remote_type_with_the_button_behavior_group():
    assert FORM_ORDER.index("remote_type") == FORM_ORDER.index("input_mode") + 1


async def _connect(data, options):
    from custom_components.bt_remote_voice.coordinator import VoiceCoordinator
    from tests.test_coordinator import FakeHass

    c = VoiceCoordinator(FakeHass(), SimpleNamespace(data=data, options=options))
    used = []

    def fake(kind):
        class Fake:
            lost = None

            def __init__(self, *args, **kwargs):
                pass

            async def open(self, *args):
                used.append(kind)
                c.stopping = True
                raise ConnectionError("wake_remote")

            async def close(self):
                pass

        return Fake

    with (
        patch("custom_components.bt_remote_voice.coordinator.FireTransport", fake("fire")),
        patch("custom_components.bt_remote_voice.coordinator.BlueZTransport", fake("atvv")),
    ):
        await asyncio.wait_for(c.connect_loop(), 2)
    return used


@pytest.mark.parametrize(
    ("data_family", "options", "expected"),
    [
        ("atvv", {"remote_type": "fire_press_to_talk"}, "fire"),
        ("fire", {"remote_type": "atvv"}, "atvv"),
        ("fire", {"remote_type": "auto"}, "fire"),
        ("fire", {}, "fire"),
        (None, {}, "atvv"),
    ],
)
async def test_the_coordinator_picks_its_transport_by_remote_type_then_family(
    data_family, options, expected
):
    data = {"address": "02:00:00:00:00:01", "name": "T", "pipeline_id": "p"}
    if data_family:
        data["family"] = data_family
    assert await _connect(data, options) == [expected]
