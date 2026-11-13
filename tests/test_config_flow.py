from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.atvv_remote.bluez import Remote
from custom_components.atvv_remote.config_flow import ConfigFlow, pipeline_choices


@pytest.fixture
async def hass(tmp_path):
    from homeassistant import auth, config_entries, loader
    from homeassistant.helpers import (
        area_registry,
        device_registry,
        entity,
        entity_registry,
        floor_registry,
        issue_registry,
        label_registry,
    )

    h = HomeAssistant(str(tmp_path))
    h.config.skip_pip = True
    h.config_entries = config_entries.ConfigEntries(h, {})
    h.config_entries._initialized.set()
    loader.async_setup(h)
    entity.async_setup(h)
    device_registry.async_setup(h)
    for registry in (
        floor_registry,
        area_registry,
        label_registry,
        device_registry,
        entity_registry,
        issue_registry,
    ):
        await registry.async_load(h)
    h.auth = await auth.auth_manager_from_config(h, [], [])
    yield h
    await h.async_stop(force=True)


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
        assert {s.domain for s in states} >= {"assist_satellite", "binary_sensor", "event"}
        assert await async_unload_entry(hass, entry)
