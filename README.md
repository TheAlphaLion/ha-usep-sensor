# USEP — Singapore Electricity Price

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/TheAlphaLion/ha-usep-sensor)](https://github.com/TheAlphaLion/ha-usep-sensor/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

<img src="logo.svg" alt="USEP Logo" width="100"/>

A Home Assistant custom integration that fetches **real-time and forecast Singapore electricity prices (USEP)** from the [Energy Market Company (EMC) NEMS Prices page](https://www.nems.emcsg.com/nems-prices) and exposes them as sensor entities.

Designed for households on **SP Group's Wholesale Electricity Price (WEP/USEP) plan** who want to automate load-shifting, battery charging, and appliance scheduling based on live price forecasts.

---

## What it does

The EMC publishes all 48 half-hour USEP prices for the current day — both settled prices for past periods and forecast prices for future periods. This integration polls that data and makes it available in Home Assistant as sensors that you can use it for further purposes.

Examples of further uses of the USEP sensors:
- See the current, next, peak, and cheapest forecast prices on a dashboard
- Build automations that trigger when prices are high (shift loads to battery, pause high power devices) or low (charge battery, run water heater)
- Display a full-day price chart and forecast table in Lovelace

---

## Sensors created

All sensors belong to a single device: **USEP — Singapore Electricity Price**

| Entity | Description | Unit |
|---|---|---|
| `sensor.usep_current` | USEP for the current half-hour period | $/MWh |
| `sensor.usep_next_period` | USEP for the next half-hour period | $/MWh |
| `sensor.usep_peak_today` | Highest USEP remaining today | $/MWh |
| `sensor.usep_lowest_forecast` | Lowest USEP remaining today | $/MWh |
| `sensor.grid_demand` | Current grid demand | MW |
| `sensor.solar_generation` | Current solar generation | MW |
| `sensor.usep_current_period` | Current period label e.g. `08:00-08:30` | — |
| `sensor.usep_data_status` | `confirmed` or `forecast_pending` | — |
| `sensor.usep_forecast_data` | Count of loaded periods; attributes contain full chart + table data | — |

### Key attributes

**`sensor.usep_current`**
```
current_period:       "15:30-16:00"
current_demand:       6931.86
current_solar:        825.78
current_is_forecast:  false
data_status:          "confirmed"
```

**`sensor.usep_lowest_forecast`**
```
lowest_forecast_period:  "22:00-22:30"
lowest_forecast_dt:      "2026-05-09T22:00:00+08:00"
```

**`sensor.usep_forecast_data`**
```
forecast_table:    [{period, usep, demand, solar, status, is_current}, ...]
chart_data_usep:   [{x: "2026-05-09T08:00:00+08:00", y: 176.72}, ...]
chart_data_demand: [{x: ..., y: 6730.8}, ...]
chart_data_solar:  [{x: ..., y: 72.4}, ...]
periods_total:     48
periods_forecast:  32
last_updated:      "2026-05-09T08:02:34+08:00"
```

### `data_status` explained

Each half-hour period has two update moments:

| Time | What happens |
|---|---|
| `HH:00` and `HH:30` | Period pointer advances immediately using cached forecast. `data_status = forecast_pending` |
| `HH:02` and `HH:32` + random second | Fresh data fetched from EMC. `data_status = confirmed` |

The random second (10–55s, fixed per HA instance at startup) spreads load across users so everyone doesn't hit EMC at exactly the same moment. Automations can check `data_status` to distinguish forecast-based actions from confirmed ones.

---

## Installation

### Via HACS (recommended)

1. In HA: **HACS → Integrations → ⋮ → Custom repositories**
2. Add URL: `https://github.com/TheAlphaLion/ha-usep-sensor`
3. Category: **Integration** → Add
4. Find **USEP — Singapore Electricity Price** → Install
5. Restart Home Assistant

### Manual

1. Download the latest release from [GitHub Releases](https://github.com/TheAlphaLion/ha-usep-sensor/releases)
2. Copy the `custom_components/usep/` folder into your HA `config/custom_components/` directory
3. Restart Home Assistant

---

## Setup

1. **Settings → Devices & Services → + Add Integration**
2. Search for **USEP**
3. Click Submit — no configuration needed
4. All 9 sensors appear immediately under the device "USEP — Singapore Electricity Price"

---

## Example automations

The sensors work with standard HA conditions — no extra helpers required.

### Notify when price is high
```yaml
automation:
  trigger:
    - platform: numeric_state
      entity_id: sensor.usep_current
      above: 300
  condition:
    - condition: state
      entity_id: sensor.usep_data_status
      state: confirmed        # only act on confirmed, not forecast
  action:
    - service: notify.mobile_app_your_phone
      data:
        title: "⚡ High USEP"
        message: "Current price is {{ states('sensor.usep_current') }} $/MWh"
```

### Charge battery at cheapest period
```yaml
automation:
  trigger:
    - platform: template
      value_template: >
        {{ states('sensor.usep_current') | float(999)
           <= states('sensor.usep_lowest_forecast') | float(999) + 1 }}
  condition:
    - condition: numeric_state
      entity_id: sensor.usep_current
      below: 160              # only charge if price is actually low
    - condition: state
      entity_id: sensor.usep_data_status
      state: confirmed
  action:
    - service: switch.turn_on
      target:
        entity_id: switch.your_battery_charger
```

---

## Lovelace dashboard [TO BE COMPLETED]

An example dashboard with a full-day chart and forecast table is provided in [`examples/dashboard.yaml`](examples/dashboard.yaml).

It requires [ApexCharts Card](https://github.com/RomRider/apexcharts-card) (install via HACS → Frontend).

To use it:
1. Create a new dashboard in HA
2. Edit → Raw configuration editor → paste the contents of `examples/dashboard.yaml`

---

## Advanced usage [TO BE COMPLETED]

For price-threshold helpers (editable sliders), battery automations, and arbitrage scheduling logic, see [`examples/`](examples/).

---

## Data source & caveats

- Data comes from EMC's public [NEMS Prices page](https://www.nems.emcsg.com/nems-prices) — the same source you see when you open that page in a browser
- Prices are **provisional** and subject to revision up to D+6 business days
- Future-period prices are **forecasts** that can shift significantly, especially before gate close (~30 min before period start)
- This integration uses EMC's public-facing web endpoints, not the paid NEMS API subscription. EMC may change these endpoints without notice. If the integration stops working, check [GitHub Issues](https://github.com/TheAlphaLion/ha-usep-sensor/issues)

---

## ⚠️ Disclaimer

**Terms of use:** EMC's [Terms and Conditions](https://www.emcsg.com/termsandconditions) state that data from their website is provided for **personal and non-commercial use only**, and that all price data and materials are copyright of EMC. This integration accesses the same public endpoints used by the NEMS Prices webpage.

By installing and using this integration, you agree that:

- You are solely responsible for ensuring your use complies with EMC's Terms and Conditions and any applicable laws
- This integration is intended for **personal, non-commercial home automation use only**
- The author(s) of this integration accept **no responsibility or liability** for any consequences arising from its use, including but not limited to: breach of EMC's Terms of Use, incorrect or delayed price data, financial decisions made based on this data, or any disruption to your home systems or appliances
- **This is not financial or energy trading advice.** USEP prices shown are indicative and provisional only

If you are uncertain whether your use is permitted, consult EMC's Terms and Conditions directly or contact EMC at their [official website](https://www.emcsg.com).

EMC offers a [paid data subscription service](https://www.emcsg.com/datasubscription) for those requiring a formally licensed, reliable data feed.

---

## AI attribution

This integration was developed with the assistance of [Claude AI](https://claude.ai) (Anthropic). The code was written, tested, and published by [@TheAlphaLion](https://github.com/TheAlphaLion).

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Integration not found in Add Integration | Check all 6 files are in `config/custom_components/usep/` and HA was fully restarted |
| Sensors show `unavailable` | Check **Settings → System → Logs**, search for "usep". Usually a network issue reaching EMC. |
| Chart shows "Loading..." | Ensure ApexCharts Card is installed via HACS and the frontend was reloaded |
| `data_status` stuck on `forecast_pending` | EMC fetch at :02/:32 may have failed — check logs |

---

## Contributing

Pull requests welcome. Please open an issue first for any significant changes.

---

## License

MIT — see [LICENSE](LICENSE).

Data sourced from the Energy Market Company (EMC) of Singapore under their standard public terms of use.
