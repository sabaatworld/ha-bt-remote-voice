"""Shared entity identity; no household configuration is built into source."""

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo, Entity

from .preferences import DOMAIN


class RemoteEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator, suffix):
        self.coordinator = coordinator
        address = coordinator.entry.data["address"].lower()
        self._attr_unique_id = f"{address}_{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(dr.CONNECTION_BLUETOOTH, address)},
            name=coordinator.entry.data["name"],
        )

    @property
    def available(self):
        return self.coordinator.ready

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.coordinator.listeners.add(self.async_write_ha_state)
        self.async_on_remove(lambda: self.coordinator.listeners.discard(self.async_write_ha_state))
