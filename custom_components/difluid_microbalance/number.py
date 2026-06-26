from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_TYPE, CONF_IS_TI, DEVICE_TYPE_R2, DOMAIN
from .coordinator import DifluidMicrobalanceCoordinator
from .coordinator_r2 import DifluidR2Coordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    is_r2 = entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_R2
    coordinator = hass.data[DOMAIN][entry.entry_id]

    if is_r2:
        device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Difluid",
            model="R2 Extract",
        )
    else:
        is_ti = entry.data.get(CONF_IS_TI, False)
        device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Difluid",
            model="Microbalance Ti" if is_ti else "Microbalance",
        )

    async_add_entities([AutoShutdownNumber(coordinator, entry, device_info)])


class AutoShutdownNumber(
    CoordinatorEntity, NumberEntity, RestoreEntity  # type: ignore[type-arg]
):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Auto Shutdown"
    _attr_icon = "mdi:timer-off-outline"
    _attr_native_min_value = 0
    _attr_native_max_value = 60
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: DifluidMicrobalanceCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_auto_shutdown"
        self._attr_device_info = device_info
        self._current_value: float = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        if state and state.state not in (None, "unavailable", "unknown"):
            try:
                self._current_value = float(state.state)
            except ValueError:
                self._current_value = 0
        self.coordinator.set_auto_shutdown_minutes(int(self._current_value))

    @property
    def available(self) -> bool:
        # Always available — user can configure it even when device is off
        return True

    @property
    def native_value(self) -> float:
        return self._current_value

    async def async_set_native_value(self, value: float) -> None:
        self._current_value = value
        self.coordinator.set_auto_shutdown_minutes(int(value))
        self.async_write_ha_state()
