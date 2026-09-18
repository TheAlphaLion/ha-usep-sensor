"""Constants for the USEP integration."""

DOMAIN = "usep"
ATTRIBUTION = "Data provided by Energy Market Company (EMC), Singapore"

# ── API endpoints (discovered via Chrome DevTools XHR on nems.emcsg.com/nems-prices) ──
BASE_URL = "https://www.nems.emcsg.com/api/sitecore/DataSync"

# Primary: live JSON used by the website chart — returns all 48 periods for today
ENDPOINT_JSON = f"{BASE_URL}/Get?value=10&fromDate={{date}}&toDate={{date}}&tpcValue=1"

# Fallback: public CSV download — confirmed working, returns same data as tab-separated
ENDPOINT_CSV = f"{BASE_URL}/DataDownload?value=10&fromDate={{date}}&toDate={{date}}&tpcValue=1"

# Tomorrow 72-period forecast (value=12) — available after 12:00 noon SGT each day.
# Returns 72 rows: today's 48 periods + tomorrow's first 24 (00:00–11:30).
# Uses the same date parameter as the today endpoints (fetched on today's date).
ENDPOINT_JSON_TOMORROW = f"{BASE_URL}/Get?value=12&fromDate={{date}}&toDate={{date}}&tpcValue=1"
ENDPOINT_CSV_TOMORROW  = f"{BASE_URL}/DataDownload?value=12&fromDate={{date}}&toDate={{date}}&tpcValue=1"

# Hour after which the tomorrow forecast becomes available (SGT, 24-hour)
TOMORROW_FORECAST_AVAILABLE_HOUR = 12

# CSV column indices (0-based, after skipping header row)
CSV_COL_DATE        = 0
CSV_COL_PERIOD      = 1
CSV_COL_DEMAND      = 2
CSV_COL_SOLAR       = 3
CSV_COL_USEP        = 5
CSV_COL_RUSEP       = 8    # blank / "-" for forecast periods; populated for settled periods
CSV_COL_MAP         = 9    # Moving Average Price — blank for forecast periods
CSV_COL_MAPT        = 10   # MAP Threshold — blank for forecast periods, ~constant otherwise
CSV_COL_TPC_APPLIED = 11   # "Yes"/"No" — blank for forecast periods

# ── Temporary Price Cap (TPC) forecasting ──────────────────────────────────────
# EMC's TPC mechanism caps published USEP to MAPT whenever MAP (the trailing
# 24-hour / 48-period moving average of the *uncapped* reference price) is
# above MAPT. MAP/MAPT/TPC Applied are only populated for settled periods, so
# forecast periods are annotated with an *implied* MAP computed from a rolling
# price window (see coordinator.py `_apply_tpc` / `_implied_map`).
TPC_MOVING_AVERAGE_PERIODS   = 48   # 24 hours of half-hour periods
TPC_PRICE_WINDOW_MAX_AGE_HOURS = 26  # retention margin for the rolling buffer

# Units
UNIT_MWH = "$/MWh"
UNIT_MW  = "MW"

# Timezone
SG_TIMEZONE = "Asia/Singapore"

# HTTP headers — mimic a browser so the EMC server accepts the request
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.nems.emcsg.com/nems-prices",
    "Accept": "application/json, text/plain, */*",
}
