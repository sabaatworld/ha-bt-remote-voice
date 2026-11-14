"""Voice input from already-paired ATVV remotes."""

from homeassistant.const import Platform
from homeassistant.helpers.translation import async_get_translations

from .coordinator import VoiceCoordinator
from .preferences import DOMAIN

PLATFORMS = [Platform.ASSIST_SATELLITE, Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR]


async def preload_translations(hass):
    """Cache entity translations so runtime messages can be localized."""
    await async_get_translations(hass, hass.config.language, "entity", [DOMAIN])


async def async_setup_entry(hass, entry):
    await preload_translations(hass)
    entry.runtime_data = VoiceCoordinator(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_options_updated))
    entry.runtime_data.start()
    return True


async def async_options_updated(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    await entry.runtime_data.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
