"""Optional voice button automation events; audio starts internally."""

from homeassistant.components.event import EventEntity

from .entity import RemoteEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([VoiceButton(entry.runtime_data)])


class VoiceButton(RemoteEntity, EventEntity):
    _attr_name = "Voice button"
    _attr_icon = "mdi:gesture-tap-hold"
    _attr_event_types = ["pressed", "released"]

    def __init__(self, coordinator):
        super().__init__(coordinator, "voice_button")

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.coordinator.event_listeners.add(self.emit)
        self.async_on_remove(lambda: self.coordinator.event_listeners.discard(self.emit))

    def emit(self, kind):
        self._trigger_event(kind)
        self.async_write_ha_state()
