from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_TYPE, CONF_IS_TI, DEVICE_TYPE_R2, DOMAIN
from .coordinator import DifluidMicrobalanceCoordinator

# DF DF 03 02 01 01  checksum=C5  (Power Button single click = tare)
_CMD_TARE = bytes.fromhex("dfdf03020101c5")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_R2:
        return
    coordinator: DifluidMicrobalanceCoordinator = hass.data[DOMAIN][entry.entry_id]
    is_ti = entry.data.get(CONF_IS_TI, False)
    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Difluid",
        model="Microbalance Ti" if is_ti else "Microbalance",
    )
    async_add_entities([
        TareButton(coordinator, entry, device_info),
        PowerOffButton(coordinator, entry, device_info),
    ])


class _DifluidButton(CoordinatorEntity[DifluidMicrobalanceCoordinator], ButtonEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: DifluidMicrobalanceCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        key: str,
        name: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        client = self.coordinator._client
        return client is not None and client.is_connected


class TareButton(_DifluidButton):
    def __init__(self, coordinator, entry, device_info):
        super().__init__(coordinator, entry, device_info, "tare", "Tare", "mdi:scale-balance")

    async def async_press(self) -> None:
        await self.coordinator.async_send_command(_CMD_TARE)


class PowerOffButton(_DifluidButton):
    def __init__(self, coordinator, entry, device_info):
        super().__init__(coordinator, entry, device_info, "power_off", "Power Off", "mdi:power")

    async def async_press(self) -> None:
        await self.coordinator.async_power_off()
