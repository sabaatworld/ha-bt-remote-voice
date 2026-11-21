import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol

from custom_components.bt_remote_voice.bluez import Remote
from custom_components.bt_remote_voice.config_flow import ConfigFlow, pipeline_choices


async def test_picker_lists_bonded_remote_and_manual_fallback(hass):
    flow = ConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    remote = Remote("02:00:00:00:00:01", "Test Remote", "/org/bluez/hci1/dev_test", False, True)
    with patch(
        "custom_components.bt_remote_voice.config_flow.BlueZTransport.list_paired",
        AsyncMock(return_value=[remote]),
    ):
        result = await flow.async_step_user()
    assert result["type"] == "form"
    assert flow.remotes[remote.address].name == "Test Remote"
    result = await flow.async_step_user({"device": "manual"})
    assert result["step_id"] == "manual"


async def test_manual_rejects_invalid_address_without_connecting(hass):
    flow = ConfigFlow()
    flow.hass = hass
    result = await flow.async_step_manual({"address": "not a bluetooth address"})
    assert result["errors"] == {"address": "invalid_address"}


async def test_pipeline_identity_is_not_derived_from_name():
    from types import SimpleNamespace

    items = [SimpleNamespace(id="pipeline-a", name="Renamed assistant")]
    assert pipeline_choices(items) == {"pipeline-a": "Renamed assistant"}


async def test_complete_entry_setup_creates_entities_and_unloads(hass):
    import json
    from pathlib import Path
    from types import MappingProxyType

    from homeassistant import loader
    from homeassistant.config_entries import ConfigEntry

    from custom_components.bt_remote_voice import async_setup_entry, async_unload_entry

    path = Path(__file__).parents[1] / "custom_components" / "bt_remote_voice"
    manifest = json.loads((path / "manifest.json").read_text())
    loader.async_setup(hass)
    integration = loader.Integration(
        hass,
        "custom_components.bt_remote_voice",
        path,
        manifest,
        set(x.name for x in path.iterdir()),
    )
    hass.data[loader.DATA_CUSTOM_COMPONENTS] = {"bt_remote_voice": integration}
    entry = ConfigEntry(
        domain="bt_remote_voice",
        title="Test Remote",
        version=1,
        minor_version=1,
        data={
            "address": "02:00:00:00:00:01",
            "name": "Test Remote",
            "pipeline_id": "test-pipeline",
            "mode": "transcription",
        },
        options={},
        source="user",
        unique_id="02:00:00:00:00:01",
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
    )
    hass.config_entries._entries[entry.entry_id] = entry
    with (
        patch("custom_components.bt_remote_voice.coordinator.VoiceCoordinator.start"),
        patch("homeassistant.components.http.server.HomeAssistantHTTP.async_bind", AsyncMock()),
    ):
        async with entry.setup_lock:
            assert await async_setup_entry(hass, entry)
        await hass.async_block_till_done()
        states = hass.states.async_all()
        assert {s.domain for s in states} >= {
            "assist_satellite",
            "binary_sensor",
            "event",
            "sensor",
        }
        # Names come from the real translation loader, not from code.
        names = {s.name for s in states if s.entity_id.split(".")[0] in {"event", "sensor"}}
        assert {"Test Remote Voice button", "Test Remote Last error"} <= names, names
        error = next(s for s in states if s.entity_id.startswith("sensor."))
        assert error.state == "none"
        assert await async_unload_entry(hass, entry)


async def test_options_speaker_is_optional_configurable_and_preserves_unknown_options(hass):
    from types import SimpleNamespace

    from homeassistant.components.media_player import MediaPlayerEntityFeature

    from custom_components.bt_remote_voice.config_flow import OptionsFlow

    entry = SimpleNamespace(
        data={"pipeline_id": "stable-id"}, options={"mode": "commands", "future_option": True}
    )
    flow = OptionsFlow(entry)
    flow.hass = hass
    hass.states.async_set(
        "media_player.test_speaker",
        "idle",
        {"supported_features": int(MediaPlayerEntityFeature.PLAY_MEDIA)},
    )
    with patch(
        "custom_components.bt_remote_voice.config_flow.pipelines",
        return_value={"stable-id": "Offline"},
    ):
        result = await flow.async_step_init(
            {"pipeline_id": "stable-id", "mode": "commands", "speaker": "media_player.test_speaker"}
        )
    assert result["data"]["speaker"] == "media_player.test_speaker"
    assert result["data"]["future_option"] is True


# ---- Fire family in the config flow ----
async def _configure_fire(hass, transport_open):
    from types import SimpleNamespace

    from custom_components.bt_remote_voice.config_flow import ConfigFlow

    flow = ConfigFlow()
    flow.hass = hass
    flow.selected = SimpleNamespace(address="02:00:00:00:00:01", name="Fire", family="fire")
    closed = []

    class FakeFire:
        async def open(self, address, on_control, on_audio):
            return await transport_open()

        async def close(self):
            closed.append(True)

    with (
        patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "P"}),
        patch("custom_components.bt_remote_voice.config_flow.FireTransport", FakeFire),
    ):
        result = await flow.async_step_configure({"pipeline_id": "p"})
    return result, closed


async def test_picker_lists_both_families(hass):
    flow = ConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    remotes = [
        Remote("02:00:00:00:00:01", "Fire", "/p1", False, True, "fire"),
        Remote("02:00:00:00:00:02", "Atvv", "/p2", False, True),
    ]
    with patch(
        "custom_components.bt_remote_voice.config_flow.BlueZTransport.list_paired",
        AsyncMock(return_value=remotes),
    ):
        result = await flow.async_step_user()
    options = {o["value"] for o in result["data_schema"].schema["device"].config["options"]}
    assert {"02:00:00:00:00:01", "02:00:00:00:00:02"} <= options


async def test_fire_entry_stores_its_family(hass):
    async def ok():
        return object()

    result, closed = await _configure_fire(hass, ok)
    assert result["type"] == "create_entry" and result["data"]["family"] == "fire"
    assert closed == [True]


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ValueError("opus_decoder_unavailable"), "opus_decoder_unavailable"),
        (ValueError("hidraw_permission_denied"), "hidraw_permission_denied"),
        (ConnectionError("hidraw_unavailable"), "hidraw_unavailable"),
        (ConnectionError("wake_remote"), "wake_remote"),
    ],
)
async def test_fire_configure_reports_each_failure_distinctly(hass, failure, expected):
    async def fail():
        raise failure

    result, closed = await _configure_fire(hass, fail)
    assert result["errors"] == {"base": expected}
    assert closed == [True]


async def test_entry_without_family_defaults_to_atvv():
    from types import SimpleNamespace

    from custom_components.bt_remote_voice.coordinator import VoiceCoordinator
    from tests.test_coordinator import FakeHass

    c = VoiceCoordinator(
        FakeHass(),
        SimpleNamespace(
            data={"address": "02:00:00:00:00:01", "name": "T", "pipeline_id": "p"}, options={}
        ),
    )

    class Stop(Exception):
        pass

    class Blue:
        lost = None

        async def open(self, *args):
            c.stopping = True
            raise ConnectionError("wake_remote")

        async def close(self):
            pass

    with (
        patch("custom_components.bt_remote_voice.coordinator.BlueZTransport", Blue) as blue,
        patch("custom_components.bt_remote_voice.coordinator.FireTransport") as fire,
    ):
        await asyncio.wait_for(c.connect_loop(), 2)
    assert blue is Blue and not fire.called


# ---- External dependencies in the options form (pipeline and speaker) ----
def _options_flow(hass, data=None, options=None):
    from types import SimpleNamespace

    from custom_components.bt_remote_voice.config_flow import OptionsFlow

    flow = OptionsFlow(SimpleNamespace(data=data or {"pipeline_id": "p1"}, options=options or {}))
    flow.hass = hass
    return flow


def _field(result, name):
    return next(k for k in result["data_schema"].schema if k == name), result["data_schema"].schema[
        next(k for k in result["data_schema"].schema if k == name)
    ]


async def test_pipeline_field_is_a_dropdown_listing_the_real_pipelines(hass):
    from homeassistant.helpers.selector import SelectSelector

    flow = _options_flow(hass)
    with patch(
        "custom_components.bt_remote_voice.config_flow.pipelines",
        return_value={"p1": "Home Assistant", "p2": "Gemini Live"},
    ):
        result = await flow.async_step_init()
    key, selector = _field(result, "pipeline_id")
    assert isinstance(selector, SelectSelector)
    assert [(o["value"], o["label"]) for o in selector.config["options"]] == [
        ("preferred", "preferred"),  # labelled by selector.pipeline in translations
        ("p1", "Home Assistant"),
        ("p2", "Gemini Live"),
    ]
    assert selector.config["translation_key"] == "pipeline"
    assert key.default() == "p1"
    assert not result["errors"]


async def test_missing_saved_pipeline_is_flagged_not_silently_replaced(hass):
    flow = _options_flow(hass, data={"pipeline_id": "gone"})
    with patch(
        "custom_components.bt_remote_voice.config_flow.pipelines",
        return_value={"p1": "Home Assistant"},
    ):
        result = await flow.async_step_init()
        key, _ = _field(result, "pipeline_id")
        assert result["errors"] == {"pipeline_id": "pipeline_missing"}
        assert key.default is vol.UNDEFINED  # nothing is pre-selected on the user's behalf
        # Saving without choosing one is refused, with the same message.
        again = await flow.async_step_init({"pipeline_id": "gone", "mode": "transcription"})
    assert again["type"] == "form" and again["errors"]["base"] == "invalid_pipeline"


async def test_unavailable_saved_speaker_stays_selectable_and_other_settings_still_save(hass):
    flow = _options_flow(hass, options={"mode": "commands", "speaker": "media_player.office"})
    with patch(
        "custom_components.bt_remote_voice.config_flow.pipelines",
        return_value={"p1": "Home Assistant"},
    ):
        result = await flow.async_step_init()
        key, selector = _field(result, "speaker")
        values = [o["value"] for o in selector.config["options"]]
        assert "media_player.office" in values  # the saved choice is not dropped
        assert key.default() == "media_player.office"
        assert result["errors"] == {"speaker": "speaker_missing"}
        saved = await flow.async_step_init(
            {
                "pipeline_id": "p1",
                "mode": "commands",
                "speaker": "media_player.office",
                "busy_processing": "reject",
            }
        )
    assert saved["type"] == "create_entry"
    assert saved["data"]["speaker"] == "media_player.office"


async def test_a_different_unknown_speaker_is_still_rejected(hass):
    flow = _options_flow(hass, options={"speaker": "media_player.office"})
    with patch(
        "custom_components.bt_remote_voice.config_flow.pipelines",
        return_value={"p1": "Home Assistant"},
    ):
        result = await flow.async_step_init(
            {"pipeline_id": "p1", "mode": "commands", "speaker": "media_player.typo"}
        )
    assert result["errors"]["base"] == "invalid_speaker"


async def test_options_accept_a_short_press_minimum_and_reject_one_out_of_range(hass):
    flow = _options_flow(hass)
    base = {"pipeline_id": "p1", "mode": "commands"}
    with patch(
        "custom_components.bt_remote_voice.config_flow.pipelines",
        return_value={"p1": "Home Assistant"},
    ):
        ok = await flow.async_step_init({**base, "min_press_seconds": 0.1})
        bad = await flow.async_step_init({**base, "min_press_seconds": 0.55})
    assert ok["type"] == "create_entry" and ok["data"]["min_press_seconds"] == 0.1
    assert bad["type"] == "form" and bad["errors"]["base"] == "invalid_preferences"


async def test_preferred_is_the_default_for_new_remotes_and_is_accepted(hass):
    from types import SimpleNamespace

    flow = ConfigFlow()
    flow.hass = hass
    flow.selected = SimpleNamespace(address="02:00:00:00:00:01", name="Remote", family="atvv")
    with patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "P"}):
        form = await flow.async_step_configure()
        (key,) = [k for k in form["data_schema"].schema if k == "pipeline_id"]
        assert key.default() == "preferred"
        assert not form["errors"]
        with patch("custom_components.bt_remote_voice.config_flow.BlueZTransport") as transport:
            transport.return_value.open = AsyncMock(return_value=object())
            transport.return_value.close = AsyncMock()
            done = await flow.async_step_configure({"pipeline_id": "preferred"})
    assert done["type"] == "create_entry" and done["data"]["pipeline_id"] == "preferred"


async def test_saved_preferred_is_not_flagged_and_stays_selected(hass):
    flow = _options_flow(hass, options={"pipeline_id": "preferred"})
    with patch(
        "custom_components.bt_remote_voice.config_flow.pipelines",
        return_value={"p1": "Home Assistant"},
    ):
        result = await flow.async_step_init()
        key, _ = _field(result, "pipeline_id")
        assert not result["errors"] and key.default() == "preferred"
        saved = await flow.async_step_init({"pipeline_id": "preferred", "mode": "transcription"})
    assert saved["type"] == "create_entry" and saved["data"]["pipeline_id"] == "preferred"


# ---- Honest setup errors (issue #1): each failure names what is actually wrong ----
async def _configure_failure(hass, family, failure):
    from types import SimpleNamespace

    flow = ConfigFlow()
    flow.hass = hass
    flow.selected = SimpleNamespace(address="02:00:00:00:00:01", name="R", family=family)

    class Fake:
        async def open(self, *args):
            raise failure

        async def close(self):
            pass

    target = "FireTransport" if family == "fire" else "BlueZTransport"
    with (
        patch("custom_components.bt_remote_voice.config_flow.pipelines", return_value={"p": "P"}),
        patch(f"custom_components.bt_remote_voice.config_flow.{target}", Fake),
    ):
        return (await flow.async_step_configure({"pipeline_id": "p"}))["errors"]


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ValueError("unsupported_capabilities"), "unsupported_remote"),
        (ValueError("unsupported_codec"), "unsupported_remote"),
        (ValueError("invalid_frame_size"), "unsupported_remote"),
        (ValueError("unsupported_write"), "unsupported_remote"),
        (ValueError("something_unforeseen"), "unsupported_remote"),
        (ValueError("voice_service_unavailable"), "voice_service_unavailable"),
        (ValueError("not_paired"), "not_paired"),
        (ValueError("multiple_adapters"), "multiple_adapters"),
        (ConnectionError("wake_remote"), "wake_remote"),
        (RuntimeError("anything else"), "bluetooth_unavailable"),
    ],
)
async def test_atvv_setup_errors_are_specific(hass, failure, expected):
    assert await _configure_failure(hass, "atvv", failure) == {"base": expected}


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ValueError("fire_voice_unsupported"), "fire_voice_unsupported"),
        (ValueError("something_unforeseen"), "fire_voice_unsupported"),
        (ValueError("not_paired"), "not_paired"),
        (ValueError("multiple_adapters"), "multiple_adapters"),
        (RuntimeError("anything else"), "bluetooth_unavailable"),
    ],
)
async def test_fire_setup_errors_never_use_the_atvv_wording(hass, failure, expected):
    errors = await _configure_failure(hass, "fire", failure)
    assert errors == {"base": expected}
    assert errors["base"] != "unsupported_remote"
