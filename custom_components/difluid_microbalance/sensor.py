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
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .brew_session import BREW_KEY, BrewSession
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

# ── brew detector sensors ─────────────────────────────────────────────────────
# These read the shared BrewSession rather than the coordinator, because the last
# detected shot must stay readable after the scale auto-disconnects — which it does
# five minutes after the pour, well before anyone looks at the dashboard.


@dataclass(frozen=True)
class DifluidBrewSensorDescription(SensorEntityDescription):
    value_fn: Callable = lambda _: None
    attrs_fn: Callable | None = None


def _weigh_attrs(event) -> dict:
    if event is None:
        return {}
    return {
        "detected_at": dt_util.utc_from_timestamp(event.at).isoformat(),
        "plateau_seconds": event.hold_seconds,
        "rise_seconds": event.rise_seconds,
        # More than one entry means the weighing was topped up or was still
        # settling; it explains a value that looks off without a trip to the
        # recorder. A single entry is the ordinary case.
        "steps": event.steps,
    }


# state_class = MEASUREMENT on all three: it is what makes Home Assistant keep
# long-term statistics for them (permanent, unlike the recorder's ~10 days) and it
# gives the Prometheus exporter a numeric series to publish.
BREW_SENSORS: tuple[DifluidBrewSensorDescription, ...] = (
    DifluidBrewSensorDescription(
        key="last_dose",
        name="Last Dose",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=1,
        icon="mdi:coffee-outline",
        value_fn=lambda s: s.last_dose.value if s.last_dose else None,
        attrs_fn=lambda s: _weigh_attrs(s.last_dose),
    ),
    DifluidBrewSensorDescription(
        key="last_yield",
        name="Last Yield",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=1,
        icon="mdi:cup-outline",
        value_fn=lambda s: s.last_yield.value if s.last_yield else None,
        attrs_fn=lambda s: _weigh_attrs(s.last_yield),
    ),
    DifluidBrewSensorDescription(
        key="brew_ratio",
        name="Brew Ratio",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:scale-unbalanced",
        value_fn=lambda s: round(s.last_pair.ratio, 2) if s.last_pair else None,
        attrs_fn=lambda s: (
            {
                "dose": round(s.last_pair.dose, 2),
                "yield": round(s.last_pair.yield_g, 2),
                "paired_at": dt_util.utc_from_timestamp(
                    s.last_pair.yield_at
                ).isoformat(),
            }
            if s.last_pair
            else {}
        ),
    ),
    DifluidBrewSensorDescription(
        key="brew_count",
        name="Brew Count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
        value_fn=lambda s: s.brew_count,
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
        return

    entities: list = [
        DifluidMicrobalanceSensor(coordinator, desc, entry)
        for desc in MICROBALANCE_SENSORS
    ]

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Difluid",
        model="Microbalance Ti" if entry.data.get(CONF_IS_TI) else "Microbalance",
    )

    session: BrewSession | None = hass.data.get(DOMAIN, {}).get(BREW_KEY)
    if session is not None:
        entities += [
            DifluidBrewSensor(session, desc, entry, device_info)
            for desc in BREW_SENSORS
        ]

    entities.append(await DifluidVersionSensor.async_create(hass, entry, device_info))
    async_add_entities(entities)


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


# ── brew detector entities ────────────────────────────────────────────────────

class DifluidBrewSensor(SensorEntity):
    """A detected dose / pour / ratio.

    Deliberately not a CoordinatorEntity: it must not go unavailable when the BLE
    link drops.  State is restored from BrewSession's Store, which loads before
    platform setup, so RestoreEntity would be redundant.
    """

    entity_description: DifluidBrewSensorDescription
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        session: BrewSession,
        description: DifluidBrewSensorDescription,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        self._session = session
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        self._session.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._session.remove_listener(self._handle_update)

    def _handle_update(self) -> None:
        # Always invoked from the event loop (BLE notification -> BrewSession).
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        # The last shot stays meaningful whether or not the scale is switched on.
        return True

    @property
    def native_value(self):
        return self.entity_description.value_fn(self._session)

    @property
    def extra_state_attributes(self) -> dict:
        attrs_fn = self.entity_description.attrs_fn
        return attrs_fn(self._session) if attrs_fn else {}


class DifluidVersionSensor(SensorEntity):
    """Which build of the integration is actually loaded.

    Exists to answer that question directly while iterating on pre-release builds,
    instead of guessing from behaviour after a HACS redownload.
    """

    _attr_has_entity_name = True
    _attr_name = "Integration Version"
    _attr_icon = "mdi:package-variant"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, version: str, entry: ConfigEntry, device_info: DeviceInfo):
        self._attr_native_value = version
        self._attr_unique_id = f"{entry.entry_id}_integration_version"
        self._attr_device_info = device_info

    @classmethod
    async def async_create(
        cls, hass: HomeAssistant, entry: ConfigEntry, device_info: DeviceInfo
    ) -> "DifluidVersionSensor":
        version = "unknown"
        try:
            from homeassistant.loader import async_get_integration

            integration = await async_get_integration(hass, DOMAIN)
            version = str(integration.version or "unknown")
        except Exception:  # noqa: BLE001 - diagnostic only, never block setup
            pass
        return cls(version, entry, device_info)

    @property
    def available(self) -> bool:
        return True


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
