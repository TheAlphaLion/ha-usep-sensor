# USEP — Singapore Electricity Price

[!\[hacs\_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[!\[GitHub release](https://img.shields.io/github/v/release/TheAlphaLion/ha-usep-sensor)](https://github.com/YOUR_GITHUB_USERNAME/ha-usep-sensor/releases)
[!\[License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A Home Assistant custom integration that fetches **real-time and forecast Singapore electricity prices (USEP)** from the [Energy Market Company (EMC) NEMS Prices page](https://www.nems.emcsg.com/nems-prices) and exposes them as sensor entities.

Designed for households on **SP Group's Wholesale Electricity Price (WEP/USEP) plan** who want to automate load-shifting, battery charging, and appliance scheduling based on live price forecasts.

\---

## What it does

The EMC publishes all 48 half-hour USEP prices for the current day — both settled prices for past periods and forecast prices for future periods. This integration polls that data and makes it available in Home Assistant so you can:

* See the current, next, peak, and cheapest forecast prices on a dashboard
* Build automations that trigger when prices are high (shift loads to battery) or low (charge battery, run water heater)
* Display a full-day price chart and forecast table in Lovelace

\---

## Sensors created

All sensors belong to a single device: **USEP — Singapore Electricity Price**

|Entity|Description|Unit|
|-|-|-|
|`sensor.usep\_current`|USEP for the current half-hour period|$/MWh|
|`sensor.usep\_next\_period`|USEP for the next half-hour period|$/MWh|
|`sensor.usep\_peak\_today`|Highest USEP remaining today|$/MWh|
|`sensor.usep\_lowest\_forecast`|Lowest USEP remaining today|$/MWh|
|`sensor.grid\_demand`|Current grid demand|MW|
|`sensor.solar\_generation`|Current solar generation|MW|
|`sensor.usep\_current\_period`|Current period label e.g. `08:00-08:30`|—|
|`sensor.usep\_data\_status`|`confirmed` or `forecast\_pending`|—|
|`sensor.usep\_forecast\_data`|Count of loaded periods; attributes contain full chart + table data|—|

### Key attributes

**`sensor.usep\_current`**

```
current\_period:       "15:30-16:00"
current\_demand:       6931.86
current\_solar:        825.78
current\_is\_forecast:  false
data\_status:          "confirmed"
```

**`sensor.usep\_lowest\_forecast`**

```
lowest\_forecast\_period:  "22:00-22:30"
lowest\_forecast\_dt:      "2026-05-09T22:00:00+08:00"
```

**`sensor.usep\_forecast\_data`**

```
forecast\_table:    \[{period, usep, demand, solar, status, is\_current}, ...]
chart\_data\_usep:   \[{x: "2026-05-09T08:00:00+08:00", y: 176.72}, ...]
chart\_data\_demand: \[{x: ..., y: 6730.8}, ...]
chart\_data\_solar:  \[{x: ..., y: 72.4}, ...]
periods\_total:     48
periods\_forecast:  32
last\_updated:      "2026-05-09T08:02:34+08:00"
```

### `data\_status` explained

Each half-hour period has two update moments:

|Time|What happens|
|-|-|
|`HH:00` and `HH:30`|Period pointer advances immediately using cached forecast. `data\_status = forecast\_pending`|
|`HH:02` + random second|Fresh data fetched from EMC. `data\_status = confirmed`|

The random second (10–55s, fixed per HA instance at startup) spreads load across users so everyone doesn't hit EMC at exactly the same moment. Automations can check `data\_status` to distinguish forecast-based actions from confirmed ones.

\---

## Installation

### Via HACS (recommended)

1. In HA: **HACS → Integrations → ⋮ → Custom repositories**
2. Add URL: `https://github.com/TheAlphaLion/ha-usep-sensor`
3. Category: **Integration** → Add
4. Find **USEP — Singapore Electricity Price** → Install
5. Restart Home Assistant

### Manual

1. Download the latest release from [GitHub Releases](https://github.com/YOUR_GITHUB_USERNAME/ha-usep-sensor/releases)
2. Copy the `custom\_components/usep/` folder into your HA `config/custom\_components/` directory
3. Restart Home Assistant

\---

## Setup

1. **Settings → Devices \& Services → + Add Integration**
2. Search for **USEP**
3. Click Submit — no configuration needed
4. All 9 sensors appear immediately under the device "USEP — Singapore Electricity Price"

\---

## Example automations

The sensors work with standard HA conditions — no extra helpers required.

### Notify when price is high

```yaml
automation:
  trigger:
    - platform: numeric\_state
      entity\_id: sensor.usep\_current
      above: 300
  condition:
    - condition: state
      entity\_id: sensor.usep\_data\_status
      state: confirmed        # only act on confirmed, not forecast
  action:
    - service: notify.mobile\_app\_your\_phone
      data:
        title: "⚡ High USEP"
        message: "Current price is {{ states('sensor.usep\_current') }} $/MWh"
```

### Charge battery at cheapest period

```yaml
automation:
  trigger:
    - platform: template
      value\_template: >
        {{ states('sensor.usep\_current') | float(999)
           <= states('sensor.usep\_lowest\_forecast') | float(999) + 1 }}
  condition:
    - condition: numeric\_state
      entity\_id: sensor.usep\_current
      below: 160              # only charge if price is actually low
    - condition: state
      entity\_id: sensor.usep\_data\_status
      state: confirmed
  action:
    - service: switch.turn\_on
      target:
        entity\_id: switch.your\_battery\_charger
```

\---

## Lovelace dashboard

An example dashboard with a full-day chart and forecast table is provided in [`examples/dashboard.yaml`](examples/dashboard.yaml).

It requires [ApexCharts Card](https://github.com/RomRider/apexcharts-card) (install via HACS → Frontend).

To use it:

1. Create a new dashboard in HA
2. Edit → Raw configuration editor → paste the contents of `examples/dashboard.yaml`

\---

## Advanced usage

For price-threshold helpers (editable sliders), EcoFlow battery automations, and arbitrage scheduling logic, see [`examples/`](examples/).

\---

## Data source \& caveats

* Data comes from EMC's public [NEMS Prices page](https://www.nems.emcsg.com/nems-prices) — the same source you see when you open that page in a browser
* Prices are **provisional** and subject to revision up to D+6 business days
* Future-period prices are **forecasts** that can shift, especially before gate close (\~30 min before period start)
* This integration uses EMC's public-facing web endpoints, not the paid NEMS API subscription. EMC may change these endpoints without notice. If the integration stops working, check [GitHub Issues](https://github.com/YOUR_GITHUB_USERNAME/ha-usep-sensor/issues)

\---

## Troubleshooting

|Problem|Fix|
|-|-|
|Integration not found in Add Integration|Check all 6 files are in `config/custom\_components/usep/` and HA was fully restarted|
|Sensors show `unavailable`|Check **Settings → System → Logs**, search for "usep". Usually a network issue reaching EMC.|
|Chart shows "Loading..."|Ensure ApexCharts Card is installed via HACS and the frontend was reloaded|
|`data\_status` stuck on `forecast\_pending`|EMC fetch at :02/:32 may have failed — check logs|

\---

## Contributing

Pull requests welcome. Please open an issue first for any significant changes.

\---

## License

MIT — see [LICENSE](LICENSE).

Data sourced from the Energy Market Company (EMC) of Singapore under their standard public terms of use.

