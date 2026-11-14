from unittest.mock import AsyncMock, patch

from custom_components.atvv_remote.bluez import Remote
from custom_components.atvv_remote.config_flow import ConfigFlow, pipeline_choices


async def test_picker_lists_bonded_remote_and_manual_fallback(hass):
    flow = ConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    remote = Remote("02:00:00:00:00:01", "Test Remote", "/org/bluez/hci1/dev_test", False, True)
    with patch(
        "custom_components.atvv_remote.config_flow.BlueZTransport.list_paired",
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

    from custom_components.atvv_remote import async_setup_entry, async_unload_entry

    path = Path(__file__).parents[1] / "custom_components" / "atvv_remote"
    manifest = json.loads((path / "manifest.json").read_text())
    loader.async_setup(hass)
    integration = loader.Integration(
        hass, "custom_components.atvv_remote", path, manifest, set(x.name for x in path.iterdir())
    )
    hass.data[loader.DATA_CUSTOM_COMPONENTS] = {"atvv_remote": integration}
    entry = ConfigEntry(
        domain="atvv_remote",
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
        patch("custom_components.atvv_remote.coordinator.VoiceCoordinator.start"),
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

    from custom_components.atvv_remote.config_flow import OptionsFlow

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
        "custom_components.atvv_remote.config_flow.pipelines", return_value={"stable-id": "Offline"}
    ):
        result = await flow.async_step_init(
            {"pipeline_id": "stable-id", "mode": "commands", "speaker": "media_player.test_speaker"}
        )
    assert result["data"]["speaker"] == "media_player.test_speaker"
    assert result["data"]["future_option"] is True
