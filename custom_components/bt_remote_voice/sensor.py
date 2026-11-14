"""Latched root cause of the most recent failed request."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory

from .entity import RemoteEntity
from .preferences import ERROR_CODES, error_state


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([LastError(entry.runtime_data)])


class LastError(RemoteEntity, SensorEntity):
    """State is a translated failure code, ``none`` or ``other``.

    Cleared when a later request succeeds. Provider-defined codes report
    ``other`` with the raw value in the ``code`` attribute. Busy rejections are
    not faults and never latch here (see the event entity).
    """

    _attr_translation_key = "last_error"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["none", *ERROR_CODES, "other"]

    def __init__(self, coordinator):
        super().__init__(coordinator, "last_error")

    @property
    def available(self):
        # Disconnects are among the failures this entity must be able to report.
        return True

    @property
    def native_value(self):
        failure = self.coordinator.last_failure
        return error_state(failure["code"]) if failure else "none"

    @property
    def extra_state_attributes(self):
        failure = self.coordinator.last_failure or {}
        return {key: failure.get(key) for key in ("code", "stage", "since", "session")}
