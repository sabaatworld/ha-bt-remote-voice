"""Latched root cause of the most recent failed request."""

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory

from .entity import RemoteEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([LastError(entry.runtime_data)])


class LastError(RemoteEntity, SensorEntity):
    """State is the failure code or "none"; cleared when a later request succeeds.

    Free-form provider codes are possible, so this is deliberately not an enum.
    Busy rejections are not faults and never latch here (see the event entity).
    """

    _attr_name = "Last error"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator, "last_error")

    @property
    def available(self):
        # Disconnects are among the failures this entity must be able to report.
        return True

    @property
    def native_value(self):
        failure = self.coordinator.last_failure
        return failure["code"] if failure else "none"

    @property
    def extra_state_attributes(self):
        failure = self.coordinator.last_failure or {}
        return {key: failure.get(key) for key in ("message", "stage", "since", "session")}
