from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfMass, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .brew_session import BrewSession, detector_device_info
from .const import (
    CONF_DEVICE_TYPE,
    CONF_IS_TI,
    CONF_UID_PREFIX,
    DEVICE_TYPE_DETECTOR,
    DEVICE_TYPE_R2,
    DOMAIN,
)
from .coordinator import DifluidMicrobalanceCoordinator, MicrobalanceData
from .coordinator_r2 import DifluidR2Coordinator, R2Data

#: Measured brews published for the chart.  The session keeps more than this; a
#: control chart with fifty dots on it stops showing where you are and starts showing
#: where you have ever been.
CHART_POINTS = 20

@dataclass(frozen=True)
class DifluidSensorDescription(SensorEntityDescription):
    value_fn: Callable = lambda _: None
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

@dataclass(frozen=True)
class DifluidBrewSensorDescription(SensorEntityDescription):
    value_fn: Callable = lambda _: None
    attrs_fn: Callable | None = None
    #: Set on the two per-day figures.  Their numerator only changes when a brew is
    #: detected, but their *denominator* grows continuously, so unlike every other
    #: sensor here they go stale on their own and need a clock.  See
    #: DifluidBrewRateSensor.
    ticks: bool = False

def _weigh_attrs(event) -> dict:
    if event is None:
        return {}
    rise = event.rise_seconds
    return {
        "detected_at": dt_util.utc_from_timestamp(event.at).isoformat(),
        "plateau_seconds": event.hold_seconds,
        "rise_seconds": None if rise is None else round(float(rise), 1),
        "steps": event.steps,
    }

def _iso(timestamp: float) -> str | None:
    """A POSIX timestamp as an ISO string, or None if it was never set."""
    return dt_util.utc_from_timestamp(timestamp).isoformat() if timestamp else None

def _period_attrs(session) -> dict:
    """When the current period began and how long it has been running.

    Shared by every period sensor so that a reset is legible from any of them: without
    it, "3 brews" and "1.5 cups/d" are two numbers with no visible relationship to each
    other or to when the button was last pressed.

    elapsed_days is the raw age, deliberately *not* the floored value the averages
    divide by — on the first day those two differ, and the honest way to explain a
    daily average that looks low is to show the real age next to it rather than the
    one the formula used.  See BrewTotals.elapsed_days for why the floor exists.
    """
    started = session.totals.period_started
    if not started:
        return {}
    now = dt_util.utcnow().timestamp()
    return {
        "period_started": _iso(started),
        "elapsed_days": round(max(0.0, now - started) / 86400.0, 2),
    }

BREW_SENSORS: tuple[DifluidBrewSensorDescription, ...] = (
    DifluidBrewSensorDescription(
        key="last_dose",
        name="Last Dose",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=1,
        icon="mdi:coffee-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
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
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.last_yield.value if s.last_yield else None,
        attrs_fn=lambda s: _weigh_attrs(s.last_yield),
    ),
    DifluidBrewSensorDescription(
        key="brew_dose",
        name="Brew Dose",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=1,
        icon="mdi:coffee",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: round(s.last_pair.dose, 2) if s.last_pair else None,
        attrs_fn=lambda s: (
            {"paired_at": dt_util.utc_from_timestamp(s.last_pair.yield_at).isoformat()}
            if s.last_pair
            else {}
        ),
    ),
    DifluidBrewSensorDescription(
        key="brew_yield",
        name="Brew Yield",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=1,
        icon="mdi:cup",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: round(s.last_pair.yield_g, 2) if s.last_pair else None,
        attrs_fn=lambda s: (
            {"paired_at": dt_util.utc_from_timestamp(s.last_pair.yield_at).isoformat()}
            if s.last_pair
            else {}
        ),
    ),
    DifluidBrewSensorDescription(
        key="brew_ratio",
        name="Brew Ratio",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:scale-unbalanced",
        entity_category=EntityCategory.DIAGNOSTIC,
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
        key="last_tds",
        name="Last TDS",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=2,
        icon="mdi:water-percent",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.last_measurement.tds if s.last_measurement else None,
        attrs_fn=lambda s: (
            {
                "measured_at": dt_util.utc_from_timestamp(
                    s.last_measurement.measured_at
                ).isoformat(),
                "brew_at": dt_util.utc_from_timestamp(s.last_measurement.at).isoformat(),
            }
            if s.last_measurement
            else {}
        ),
    ),
    DifluidBrewSensorDescription(
        key="last_extraction",
        name="Extraction",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=2,
        icon="mdi:coffee-to-go-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.last_measurement.ext if s.last_measurement else None,
        attrs_fn=lambda s: (
            {
                "dose": s.last_measurement.dose,
                "yield": s.last_measurement.yield_g,
                "tds": s.last_measurement.tds,
                "ratio": (
                    None if s.last_measurement.ratio is None
                    else round(s.last_measurement.ratio, 2)
                ),
                "seconds": s.last_measurement.seconds,
                "brew_at": dt_util.utc_from_timestamp(
                    s.last_measurement.at
                ).isoformat(),
                "measured_at": dt_util.utc_from_timestamp(
                    s.last_measurement.measured_at
                ).isoformat(),
                "last_brew_at": (
                    None if s.last_pair is None else round(s.last_pair.yield_at, 1)
                ),
                "measured_brews": len(s.measurements),
                "points": [
                    [
                        round(m.at, 1), m.ext, m.tds,
                        None if m.ratio is None else round(m.ratio, 2),
                        m.dose, m.yield_g, m.seconds, round(m.measured_at, 1),
                    ]
                    for m in s.measurements[-CHART_POINTS:]
                ],
            }
            if s.last_measurement
            else {}
        ),
    ),
    DifluidBrewSensorDescription(
        key="brew_count",
        name="Brew Count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
        value_fn=lambda s: s.brew_count,
        attrs_fn=lambda s: {
            "average_dose": (
                round(s.totals.total_dose_g / s.brew_count, 2)
                if s.brew_count and s.totals.total_dose_g
                else None
            ),
        },
    ),
    DifluidBrewSensorDescription(
        key="brew_count_period",
        name="Brew Count (Period)",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
        value_fn=lambda s: s.totals.period_brews(s.brew_count),
        attrs_fn=lambda s: _period_attrs(s),
    ),
    DifluidBrewSensorDescription(
        key="brews_per_day",
        name="Brews per Day",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="cups/d",
        suggested_display_precision=2,
        icon="mdi:chart-line",
        ticks=True,
        value_fn=lambda s: s.totals.per_day(
            s.totals.period_brews(s.brew_count), dt_util.utcnow().timestamp()
        ),
        attrs_fn=lambda s: _period_attrs(s),
    ),
    DifluidBrewSensorDescription(
        key="coffee_total",
        name="Coffee Ground",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=0,
        icon="mdi:coffee-maker",
        value_fn=lambda s: s.totals.total_dose_g,
        attrs_fn=lambda s: {
            "counting_since": _iso(s.totals.period_started)
            if not s.totals.total_dose_g
            else None,
        },
    ),
    DifluidBrewSensorDescription(
        key="coffee_period",
        name="Coffee Ground (Period)",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=0,
        icon="mdi:coffee-maker-outline",
        value_fn=lambda s: s.totals.period_dose_g(),
        attrs_fn=lambda s: _period_attrs(s),
    ),
    DifluidBrewSensorDescription(
        key="coffee_per_day",
        name="Coffee per Day",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="g/d",
        suggested_display_precision=1,
        icon="mdi:chart-line",
        ticks=True,
        value_fn=lambda s: s.totals.per_day(
            s.totals.period_dose_g(), dt_util.utcnow().timestamp()
        ),
        attrs_fn=lambda s: _period_attrs(s),
    ),
)

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
    held = hass.data[DOMAIN][entry.entry_id]
    device_type = entry.data.get(CONF_DEVICE_TYPE)

    if device_type == DEVICE_TYPE_DETECTOR:
        session: BrewSession = held
        prefix = entry.data.get(CONF_UID_PREFIX) or entry.entry_id
        device_info = detector_device_info(entry)
        async_add_entities(
            (DifluidBrewRateSensor if desc.ticks else DifluidBrewSensor)(
                session, desc, prefix, device_info
            )
            for desc in BREW_SENSORS
        )
        return

    if device_type == DEVICE_TYPE_R2:
        async_add_entities(
            DifluidR2Sensor(held, desc, entry) for desc in R2_SENSORS
        )
        return

    entities: list = [
        DifluidMicrobalanceSensor(held, desc, entry)
        for desc in MICROBALANCE_SENSORS
    ]

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Difluid",
        model="Microbalance Ti" if entry.data.get(CONF_IS_TI) else "Microbalance",
    )
    entities.append(await DifluidVersionSensor.async_create(hass, entry, device_info))
    async_add_entities(entities)

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
            return None
        return self.entity_description.icon

    @property
    def available(self) -> bool:
        client = self.coordinator._client
        return client is not None and client.is_connected

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
        uid_prefix: str,
        device_info: DeviceInfo,
    ) -> None:
        self._session = session
        self.entity_description = description
        self._attr_unique_id = f"{uid_prefix}_{description.key}"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        self._session.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._session.remove_listener(self._handle_update)

    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        return self.entity_description.value_fn(self._session)

    @property
    def extra_state_attributes(self) -> dict:
        attrs_fn = self.entity_description.attrs_fn
        return attrs_fn(self._session) if attrs_fn else {}

class DifluidBrewRateSensor(DifluidBrewSensor):
    """A per-day average, which goes stale without anyone touching the scale.

    Every other sensor here changes only when the session does, so a listener is all
    they need.  These two divide by the age of the period, and that age grows whether
    or not coffee is being made — left to the session's own updates, the figure shown
    on a quiet Sunday would be the one computed at the last brew on Friday.

    An hour is the coarsest tick that never looks wrong: the denominator is in days, so
    an hour moves a daily average by at most ~4% of itself, and it costs 24 recorder
    rows a day instead of the 8,640 a five-minute tick would.  The timer is registered
    through async_on_remove, so it is torn down with the entity — which matters because
    BrewSession deliberately survives entry reloads and would otherwise accumulate one
    live timer per reload, every options change adding another.
    """

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._tick, timedelta(hours=1))
        )

    @callback
    def _tick(self, _now) -> None:
        self.async_write_ha_state()

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
