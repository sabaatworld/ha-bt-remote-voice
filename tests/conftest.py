import pytest
from homeassistant.core import HomeAssistant


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
