from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfMass, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_TYPE, CONF_IS_TI, DEVICE_TYPE_R2, DOMAIN
from .coordinator import DifluidMicrobalanceCoordinator, MicrobalanceData
from .coordinator_r2 import DifluidR2Coordinator, R2Data


@dataclass(frozen=True)
class DifluidSensorDescription(SensorEntityDescription):
    value_fn: Callable = lambda _: None
    # Optional dynamic-icon callback: (data) -> icon string, or None to fall
    # back to the device_class / static icon.
    icon_fn: Callable | None = None


def _battery_icon(d) -> str | None:
    """Show a lightning-bolt (charging) battery icon while charging.

    Returns None when not charging so HA uses the dynamic battery-level icon
    provided by device_class = battery.
    """
    if not d.charging:
        return None
    level = max(0, min(100, int(d.battery)))
    if level >= 95:
        return "mdi:battery-charging-100"
    if level < 15:
        return "mdi:battery-charging-outline"
    rounded = min(90, max(20, int(round(level / 10.0) * 10)))
    return f"mdi:battery-charging-{rounded}"


# ── Microbalance sensors ──────────────────────────────────────────────────────
# Order here is the intended display order (Weight → Flow → Timer → Status →
# Battery).  Charging is merged into the Battery icon (lightning bolt while
# charging), so there is no separate Charging sensor.

MICROBALANCE_SENSORS: tuple[DifluidSensorDescription, ...] = (
    DifluidSensorDescription(
        key="weight",
        name="Weight",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=1,
        value_fn=lambda d: d.weight,
    ),
    DifluidSensorDescription(
        key="flow_rate",
        name="Flow Rate",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="g/s",
        suggested_display_precision=1,
        icon="mdi:water-flow",
        value_fn=lambda d: d.flow_rate,
    ),
    DifluidSensorDescription(
        key="timer",
        name="Timer",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        icon="mdi:timer-outline",
        value_fn=lambda d: d.timer,
    ),
    DifluidSensorDescription(
        key="device_status",
        name="Device Status",
        icon="mdi:information-outline",
        value_fn=lambda d: d.device_status,
    ),
    DifluidSensorDescription(
        key="battery",
        name="Battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda d: d.battery,
        icon_fn=_battery_icon,
    ),
)

# ── R2 sensors ────────────────────────────────────────────────────────────────

R2_SENSORS: tuple[DifluidSensorDescription, ...] = (
    DifluidSensorDescription(
        key="concentration",
        name="Concentration (TDS)",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=2,
        icon="mdi:water-percent",
        value_fn=lambda d: d.concentration,
    ),
    DifluidSensorDescription(
        key="refractive_index",
        name="Refractive Index",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
        icon="mdi:eye-outline",
        value_fn=lambda d: d.refractive_index,
    ),
    DifluidSensorDescription(
        key="prism_temperature",
        name="Prism Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda d: d.prism_temperature,
    ),
    DifluidSensorDescription(
        key="sample_temperature",
        name="Sample Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda d: d.sample_temperature,
    ),
    DifluidSensorDescription(
        key="test_status",
        name="Test Status",
        icon="mdi:flask-outline",
        value_fn=lambda d: d.test_status,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]

    if entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_R2:
        async_add_entities(
            DifluidR2Sensor(coordinator, desc, entry) for desc in R2_SENSORS
        )
    else:
        async_add_entities(
            DifluidMicrobalanceSensor(coordinator, desc, entry)
            for desc in MICROBALANCE_SENSORS
        )


# ── Microbalance entity ───────────────────────────────────────────────────────

class DifluidMicrobalanceSensor(
    CoordinatorEntity[DifluidMicrobalanceCoordinator], SensorEntity
):
    entity_description: DifluidSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DifluidMicrobalanceCoordinator,
        description: DifluidSensorDescription,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Difluid",
            model="Microbalance Ti" if entry.data.get(CONF_IS_TI) else "Microbalance",
        )

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def icon(self):
        icon_fn = self.entity_description.icon_fn
        if icon_fn is not None and self.coordinator.data is not None:
            dynamic = icon_fn(self.coordinator.data)
            if dynamic is not None:
                return dynamic
            return None  # fall back to device_class default (battery level icon)
        return self.entity_description.icon

    @property
    def available(self) -> bool:
        client = self.coordinator._client
        return client is not None and client.is_connected


# ── R2 entity ─────────────────────────────────────────────────────────────────

class DifluidR2Sensor(CoordinatorEntity[DifluidR2Coordinator], SensorEntity):
    entity_description: DifluidSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DifluidR2Coordinator,
        description: DifluidSensorDescription,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Difluid",
            model="R2 Extract",
        )

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None and self.coordinator.data.authenticated
