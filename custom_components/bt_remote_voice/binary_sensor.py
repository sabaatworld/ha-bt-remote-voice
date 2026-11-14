"""Physical voice activity, independent of pipeline processing."""

from homeassistant.components.binary_sensor import BinarySensorEntity

from .entity import RemoteEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([MicrophoneActive(entry.runtime_data)])


class MicrophoneActive(RemoteEntity, BinarySensorEntity):
    _attr_translation_key = "microphone_active"
    _attr_icon = "mdi:microphone"

    def __init__(self, coordinator):
        super().__init__(coordinator, "microphone_active")

    @property
    def is_on(self):
        return self.coordinator.mic_active
