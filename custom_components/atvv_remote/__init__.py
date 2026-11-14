"""Voice input from already-paired ATVV remotes."""

from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.translation import async_get_translations

from .coordinator import VoiceCoordinator
from .issues import clear_issues, sync_issues, watch_speaker
from .preferences import DOMAIN
from .reply_condition import ReplyCondition

PLATFORMS = [Platform.ASSIST_SATELLITE, Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR]


async def preload_translations(hass):
    """Cache entity translations so runtime messages can be localized."""
    await async_get_translations(hass, hass.config.language, "entity", [DOMAIN])


async def async_setup_entry(hass, entry):
    await preload_translations(hass)
    coordinator = VoiceCoordinator(hass, entry)
    coordinator.reply_condition = ReplyCondition(hass, entry.options.get("reply_condition"))
    await coordinator.reply_condition.async_prepare()
    entry.runtime_data = coordinator
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        coordinator.reply_condition.async_unload()
        raise
    entry.async_on_unload(entry.add_update_listener(async_options_updated))
    # Other integrations may not have loaded yet while Home Assistant is starting.

    @callback
    def started(hass):
        sync_issues(hass, entry)

    entry.async_on_unload(async_at_started(hass, started))
    entry.async_on_unload(watch_speaker(hass, entry))
    entry.runtime_data.start()
    return True


async def async_options_updated(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    await entry.runtime_data.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass, entry):
    # Not on unload: a reload would otherwise reset a notice the owner dismissed.
    clear_issues(hass, entry)
