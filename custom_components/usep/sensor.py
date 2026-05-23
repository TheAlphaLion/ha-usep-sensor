"""
USEP sensor platform.

Creates 10 sensor entities from the coordinator data.
All sensors belong to a single device: "USEP — Singapore Electricity Price".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, UNIT_MWH, ATTRIBUTION
from .coordinator import USEPCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class USEPSensorDescription(SensorEntityDescription):
    """Extends SensorEntityDescription with USEP-specific fields."""
    data_key: str = ""
    extra_keys: list[str] = field(default_factory=list)


SENSORS: tuple[USEPSensorDescription, ...] = (
    # ── Price sensors ─────────────────────────────────────────────────────────
    USEPSensorDescription(
        key="current_usep",
        data_key="current_usep",
        name="USEP Current",
        icon="mdi:lightning-bolt",
        native_unit_of_measurement=UNIT_MWH,
        state_class=SensorStateClass.MEASUREMENT,
        extra_keys=["current_period", "current_demand", "current_solar",
                    "current_is_forecast", "data_status"],
    ),
    USEPSensorDescription(
        key="next_usep",
        data_key="next_usep",
        name="USEP Next Period",
        icon="mdi:lightning-bolt-circle",
        native_unit_of_measurement=UNIT_MWH,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    USEPSensorDescription(
        key="peak_usep_forecast",
        data_key="peak_usep_forecast",
        name="USEP Peak Forecast",
        icon="mdi:chart-line",
        native_unit_of_measurement=UNIT_MWH,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    USEPSensorDescription(
        key="peak_usep_today",
        data_key="peak_usep_today",
        name="USEP Peak Today",
        icon="mdi:chart-bar",
        native_unit_of_measurement=UNIT_MWH,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    USEPSensorDescription(
        key="lowest_usep_today",
        data_key="lowest_usep_today",
        name="USEP Lowest Today",
        icon="mdi:arrow-down-box",
        native_unit_of_measurement=UNIT_MWH,
        state_class=SensorStateClass.MEASUREMENT,
        extra_keys=["lowest_usep_today_period", "lowest_usep_today_dt"],
    ),
    USEPSensorDescription(
        key="lowest_forecast_usep",
        data_key="lowest_forecast_usep",
        name="USEP Lowest Forecast",
        icon="mdi:arrow-down-circle",
        native_unit_of_measurement=UNIT_MWH,
        state_class=SensorStateClass.MEASUREMENT,
        extra_keys=["lowest_forecast_period", "lowest_forecast_dt"],
    ),
    # ── Grid sensors ──────────────────────────────────────────────────────────
    USEPSensorDescription(
        key="current_demand",
        data_key="current_demand",
        name="Grid Demand",
        icon="mdi:transmission-tower",
        native_unit_of_measurement="MW",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    USEPSensorDescription(
        key="current_solar",
        data_key="current_solar",
        name="Solar Generation",
        icon="mdi:solar-power",
        native_unit_of_measurement="MW",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # ── Period / status sensors ───────────────────────────────────────────────
    USEPSensorDescription(
        key="current_period",
        data_key="current_period",
        name="USEP Current Period",
        icon="mdi:clock-outline",
    ),
    USEPSensorDescription(
        key="data_status",
        data_key="data_status",
        name="USEP Data Status",
        icon="mdi:database-clock",
        extra_keys=["data_confirmed", "current_is_forecast", "last_updated"],
    ),
    # ── Forecast data sensor ──────────────────────────────────────────────────
    # State = number of periods loaded.
    # Attributes carry the full dataset for Lovelace charts and tables.
    USEPSensorDescription(
        key="forecast_data",
        data_key="forecast_table",   # state = len(forecast_table)
        name="USEP Forecast Data",
        icon="mdi:database",
        extra_keys=[
            "chart_data_usep", "chart_data_demand", "chart_data_solar",
            "forecast_table", "periods_total", "periods_forecast", "last_updated",
            "data_status",
        ],
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up USEP sensors from a config entry."""
    coordinator: USEPCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([USEPSensor(coordinator, desc) for desc in SENSORS])


class USEPSensor(CoordinatorEntity[USEPCoordinator], SensorEntity):
    """A single USEP sensor entity backed by the coordinator."""

    entity_description: USEPSensorDescription
    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: USEPCoordinator,
        description: USEPSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, "usep_emc")},
            "name": "USEP — Singapore Electricity Price",
            "manufacturer": "Energy Market Company (EMC)",
            "model": "NEMS Real-Time Prices",
            "configuration_url": "https://www.nems.emcsg.com/nems-prices",
        }

    @property
    def native_value(self) -> Any:
        if not self.coordinator.data:
            return None

        # forecast_data: state is the count of loaded periods
        if self.entity_description.key == "forecast_data":
            table = self.coordinator.data.get("forecast_table") or []
            return len(table)

        return self.coordinator.data.get(self.entity_description.data_key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return {
            key: self.coordinator.data.get(key)
            for key in self.entity_description.extra_keys
        }
