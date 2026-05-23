"""
USEPCoordinator — fetches Singapore electricity price data from EMC.

Update schedule (two triggers per half-hour period):

  Trigger 1 — period start  HH:00:00 and HH:30:00
    Re-processes cached data immediately (no HTTP call).
    Sets data_status = "forecast_pending" so automations and the
    dashboard know the current price is still the forecast value.

  Trigger 2 — confirmed fetch  HH:02:SS and HH:32:SS
    SS is a random second between 10 and 55, chosen once at startup.
    Randomisation prevents all HA instances hitting EMC simultaneously
    when this integration is used by many people.
    Sets data_status = "confirmed" once the settled price is retrieved.

Data source:
  CSV  GET /api/sitecore/DataSync/DataDownload?value=10&fromDate=...

The CSV includes all 48 half-hour periods for today:
  - Past periods have settled USEP (RUSEP column populated)
  - Future periods have forecast USEP (RUSEP column is "-")
"""

from __future__ import annotations

import csv
import io
import logging
import random
from datetime import datetime, timedelta
from typing import Any

import aiohttp
import async_timeout

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN, ENDPOINT_CSV, REQUEST_HEADERS, SG_TIMEZONE,
    CSV_COL_DATE, CSV_COL_PERIOD, CSV_COL_DEMAND,
    CSV_COL_SOLAR, CSV_COL_USEP, CSV_COL_RUSEP,
)

_LOGGER = logging.getLogger(__name__)


class USEPCoordinator(DataUpdateCoordinator):
    """Manages all USEP data fetching and processing."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN)
        self._unsub: list = []
        self._cached_periods: list[dict] = []
        # Random second within the :02/:32 minute — fixed per HA instance
        self._fetch_second: int = random.randint(10, 55)
        _LOGGER.info(
            "USEP: confirmed fetch will run at :02:%02d and :32:%02d each hour",
            self._fetch_second, self._fetch_second,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Register time triggers and perform the initial data fetch."""
        # Trigger 1: advance the current-period pointer at period start
        self._unsub.append(
            async_track_time_change(
                self.hass, self._on_period_start, minute=[0, 30], second=0
            )
        )
        # Trigger 2: fetch confirmed price from EMC
        self._unsub.append(
            async_track_time_change(
                self.hass, self._on_confirmed_fetch,
                minute=[2, 32], second=self._fetch_second,
            )
        )
        await self.async_refresh()

    async def async_shutdown(self) -> None:
        """Clean up time listeners."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    # ── Time callbacks ────────────────────────────────────────────────────────

    @callback
    def _on_period_start(self, now: datetime) -> None:
        """Called at HH:00 and HH:30 — advance period pointer using cache."""
        if self._cached_periods:
            self.hass.async_create_task(self._apply_cache(confirmed=False))
        else:
            self.hass.async_create_task(self.async_refresh())

    @callback
    def _on_confirmed_fetch(self, now: datetime) -> None:
        """Called at HH:02:SS and HH:32:SS — fetch fresh data from EMC."""
        self.hass.async_create_task(self.async_refresh())

    async def _apply_cache(self, confirmed: bool) -> None:
        """Re-process cached periods without an HTTP fetch."""
        try:
            now_sg = dt_util.now().astimezone(dt_util.get_time_zone(SG_TIMEZONE))
            data = _process(self._cached_periods, now_sg, confirmed=confirmed)
            self.async_set_updated_data(data)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("USEP: cache re-process failed: %s", exc)

    # ── Main fetch ────────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        now_sg = dt_util.now().astimezone(dt_util.get_time_zone(SG_TIMEZONE))
        today  = now_sg.strftime("%Y-%m-%d")

        periods = await self._fetch_csv(today)

        if periods:
            self._cached_periods = periods
        elif self._cached_periods:
            _LOGGER.warning("USEP: fetch failed — using cached data from previous cycle")
            periods = self._cached_periods
        else:
            raise UpdateFailed(
                "Could not fetch USEP data and no cache is available. "
                "Check HA logs and verify nems.emcsg.com is reachable."
            )

        return _process(periods, now_sg, confirmed=True)

    # ── CSV fetch ─────────────────────────────────────────────────────────────

    async def _fetch_csv(self, today: str) -> list[dict] | None:
        """Fetch today's USEP data from the EMC CSV download endpoint."""
        url = ENDPOINT_CSV.format(date=today)
        try:
            async with async_timeout.timeout(20):
                async with aiohttp.ClientSession(headers=REQUEST_HEADERS) as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            _LOGGER.warning("USEP: CSV endpoint returned HTTP %s", resp.status)
                            return None
                        text = await resp.text()

            from datetime import date as date_t
            periods = _parse_csv(text, date_t.fromisoformat(today))
            _LOGGER.debug("USEP: CSV fetch successful — %d periods", len(periods))
            return periods or None

        except Exception as exc:
            _LOGGER.error("USEP: CSV fetch failed: %s", exc)
            return None


# ── Data processing (pure function, easy to unit-test) ────────────────────────

def _process(periods: list[dict], now_sg: datetime, confirmed: bool) -> dict[str, Any]:
    """
    Derive all coordinator values from a raw period list.
    Returns a flat dict consumed by sensor entities.
    """
    periods = sorted(
        periods,
        key=lambda p: p["period_dt"] or datetime.min.replace(tzinfo=now_sg.tzinfo),
    )

    # Locate current period and the one after it
    current = next_period = None
    for i, p in enumerate(periods):
        pdt = p["period_dt"]
        if pdt and pdt <= now_sg < pdt + timedelta(minutes=30):
            current = p
            if i + 1 < len(periods):
                next_period = periods[i + 1]
            break
    if current is None and periods:
        current = periods[0]

    # Future periods = anything that hasn't started yet
    future = [p for p in periods if p["period_dt"] and p["period_dt"] > now_sg]

    # Peak forecast = highest USEP across current + all remaining periods
    remaining = ([current] if current else []) + future
    peak_forecast = max((p["usep"] for p in remaining), default=None)

    # Peak today = highest USEP across ALL 48 periods (settled + forecast)
    peak_today = max((p["usep"] for p in periods), default=None)

    # Lowest today = lowest USEP across ALL 48 periods (settled + forecast)
    # Use for battery charging decisions — finds the true daily minimum
    # regardless of whether it has already passed
    lowest_today_row   = min(periods, key=lambda p: p["usep"]) if periods else None
    lowest_today       = lowest_today_row["usep"]     if lowest_today_row else None
    lowest_today_label = lowest_today_row["period"]   if lowest_today_row else None
    lowest_today_dt    = lowest_today_row["period_dt"] if lowest_today_row else None

    # Lowest forecast = lowest USEP among remaining (future) periods only
    lowest_forecast = lowest_forecast_label = lowest_forecast_dt = None
    if future:
        lr = min(future, key=lambda p: p["usep"])
        lowest_forecast       = lr["usep"]
        lowest_forecast_label = lr["period"]
        lowest_forecast_dt    = lr["period_dt"]

    # Chart data — ISO timestamp strings as x for ApexCharts time axis
    chart_usep = [
        {"x": p["period_dt"].isoformat(), "y": round(p["usep"], 2)}
        for p in periods if p.get("period_dt") and p.get("usep") is not None
    ]
    chart_demand = [
        {"x": p["period_dt"].isoformat(), "y": round(p["demand"] or 0)}
        for p in periods if p.get("period_dt")
    ]
    chart_solar = [
        {"x": p["period_dt"].isoformat(), "y": round(p["solar"] or 0, 1)}
        for p in periods if p.get("period_dt")
    ]

    # Table — native Python list, iterable directly in Jinja2
    table = [
        {
            "period":     p["period"],
            "usep":       round(p["usep"], 2) if p.get("usep") is not None else None,
            "demand":     round(p["demand"]) if p.get("demand") is not None else None,
            "solar":      round(p["solar"], 1) if p.get("solar") is not None else None,
            "status":     "Forecast" if p.get("is_forecast") else "Settled",
            "is_current": current is not None and p["period"] == current["period"],
        }
        for p in periods if p.get("usep") is not None
    ]

    return {
        # ── Primary sensors ───────────────────────────────────────────────────
        "current_usep":           current["usep"]        if current      else None,
        "next_usep":              next_period["usep"]    if next_period  else None,
        "current_demand":         current["demand"]      if current      else None,
        "current_solar":          current["solar"]       if current      else None,
        "current_period":         current["period"]      if current      else None,
        "peak_usep_forecast":      peak_forecast,
        "peak_usep_today":          peak_today,
        "lowest_usep_today":        lowest_today,
        "lowest_usep_today_period": lowest_today_label,
        "lowest_usep_today_dt":     lowest_today_dt.isoformat() if lowest_today_dt else None,
        "lowest_forecast_usep":     lowest_forecast,
        "lowest_forecast_period":   lowest_forecast_label,
        "lowest_forecast_dt":       lowest_forecast_dt.isoformat() if lowest_forecast_dt else None,
        # ── Status ────────────────────────────────────────────────────────────
        "data_status":         "forecast_pending" if not confirmed else "confirmed",
        "data_confirmed":      confirmed,
        "current_is_forecast": (not confirmed) or (
            current.get("is_forecast", False) if current else False
        ),
        # ── Chart + table ─────────────────────────────────────────────────────
        "chart_data_usep":   chart_usep,
        "chart_data_demand": chart_demand,
        "chart_data_solar":  chart_solar,
        "forecast_table":    table,
        # ── Metadata ──────────────────────────────────────────────────────────
        "last_updated":     now_sg.isoformat(),
        "periods_total":    len(periods),
        "periods_forecast": len(future),
    }


# ── CSV parser ────────────────────────────────────────────────────────────────

def _parse_csv(text: str, today) -> list[dict]:
    reader = csv.reader(io.StringIO(text))
    periods = []
    header_done = False
    for row in reader:
        if not header_done:
            header_done = True
            continue
        if len(row) < 6:
            continue
        usep = _to_float(row[CSV_COL_USEP])
        if usep is None:
            continue
        date_raw  = row[CSV_COL_DATE].strip().strip('"')
        base_date = _parse_date(date_raw) or today
        period    = row[CSV_COL_PERIOD].strip().strip('"')
        rusep     = row[CSV_COL_RUSEP].strip().strip('"') if len(row) > CSV_COL_RUSEP else "-"
        periods.append({
            "period":      period,
            "period_dt":   _period_to_dt(period, base_date),
            "demand":      _to_float(row[CSV_COL_DEMAND]),
            "solar":       _to_float(row[CSV_COL_SOLAR]),
            "usep":        usep,
            "is_forecast": rusep in ("-", "", "—"),
        })
    return periods


# ── Utility functions ─────────────────────────────────────────────────────────

def _to_float(val: Any) -> float | None:
    """Safe float conversion; returns None for missing/dash values."""
    if val is None:
        return None
    s = str(val).strip().replace(",", "")
    if s in ("-", "—", "", "null", "None", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _period_to_dt(period: str, base_date) -> datetime | None:
    """Convert '08:00-08:30' + date → Singapore-timezone-aware datetime."""
    try:
        import pytz
        hh, mm = map(int, period.split("-")[0].strip().split(":"))
        sg = pytz.timezone(SG_TIMEZONE)
        return sg.localize(
            datetime(base_date.year, base_date.month, base_date.day, hh, mm, 0)
        )
    except Exception:
        return None


def _parse_date(raw: str):
    """Parse date strings like '9-May-26', '2026-05-09', '09/05/2026'."""
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None
