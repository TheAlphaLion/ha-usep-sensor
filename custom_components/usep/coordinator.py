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
    Sets data_status = "confirmed" ONLY when the fetch to EMC actually
    succeeds this cycle. If EMC is unreachable and the coordinator falls
    back to cached data, data_status stays "forecast_pending" — it no
    longer claims "confirmed" for stale or promoted data (fixed in 1.0.7).
    After noon SGT, also refreshes the 72-period tomorrow forecast on
    every cycle — EMC updates it continuously, so the tomorrow sensors
    stay current throughout the afternoon and evening.

next_usep at period 48 (23:30–00:00):
  The today-only 48-period list has no period 49, so next_usep would
  normally be None at 23:30.  If cached tomorrow periods are available,
  the first tomorrow period (00:00–00:30) is used instead.

Midnight rollover (fixed in 1.0.7):
  As soon as the date rolls over — checked at every HH:00/HH:30
  period-start tick, and again before each confirmed fetch — if the
  cached today-data is still dated yesterday AND cached tomorrow-periods
  cover the new day, those tomorrow periods are immediately promoted to
  the today cache and the tomorrow cache is cleared. This means:
    - "USEP Current" reflects the new day's forecasted midnight price
      right at 00:00:00, not just after the next confirmed fetch at
      00:02:SS, and works even through an overnight EMC outage.
    - "USEP Forecast Data Tomorrow" no longer keeps showing yesterday's
      (now same-day) data relabelled as "tomorrow" — it correctly goes
      empty/unavailable until the genuine next-day forecast is fetched
      after noon SGT.

Data source:
  JSON  GET /api/sitecore/DataSync/Get?value=10&fromDate=...   (primary)
  CSV   GET /api/sitecore/DataSync/DataDownload?value=10&...   (fallback)

The response includes all 48 half-hour periods for today:
  - Past periods have settled USEP (RUSEP column populated)
  - Future periods have forecast USEP (RUSEP column is "-")

Temporary Price Cap (TPC) awareness (added in 1.0.8):
  EMC's TPC mechanism publishes USEP as `min(RUSEP, MAPT)` whenever MAP
  (the trailing 24-hour / 48-period moving average of the *uncapped*
  reference price, RUSEP) is above MAPT. That's why the "current" price
  can jump sharply between HH:00/:30 (forecast, uncapped) and HH:02/:32
  (confirmed, capped) — and why the raw forecast for periods later in the
  day can show implausible spikes the real settlement will never reach.

  MAP / MAPT / TPC Applied are only populated by EMC for settled periods,
  so forecast periods need their own estimate. Two approaches:

    Approach 1 — carry-forward: reuse the last *confirmed* TPC Applied
      flag/MAPT for every forecast period. Simple, needs no history, but
      lags by exactly one period at the moment the cap regime turns on
      or off — precisely the moments an arbitrage strategy cares about
      most.

    Approach 2 — implied MAP: reconstruct the same trailing 48-period
      average EMC would compute, using RUSEP for settled periods and the
      raw (not-yet-capped) forecast USEP as a working estimate for
      periods that haven't settled yet — including earlier forecast
      periods later in the same window. Requires a rolling price-history
      buffer that spans the trailing 24 hours (so, on a fresh restart, a
      one-time backfill fetch of yesterday's data — see `_ensure_backfill`).

  This module implements Approach 2 as the primary method (see
  `_apply_tpc` / `_implied_map`), and falls back to Approach 1 whenever
  the rolling window doesn't yet have full 24-hour coverage (e.g. right
  after a fresh install, before backfill completes, or during a genuine
  EMC data gap) — see `_apply_tpc`'s "carry_forward" branch.  Every period
  is annotated with `usep_raw` (EMC's number, unchanged) and
  `usep_effective` (the TPC-aware number); all aggregates, charts, and
  the current/next sensors use `usep_effective`, so existing automations
  and dashboards benefit without needing any changes of their own.
"""

from __future__ import annotations

import csv
import io
import logging
import random
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp
import async_timeout

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN, ENDPOINT_JSON, ENDPOINT_CSV,
    ENDPOINT_JSON_TOMORROW, ENDPOINT_CSV_TOMORROW,
    REQUEST_HEADERS, SG_TIMEZONE,
    CSV_COL_DATE, CSV_COL_PERIOD, CSV_COL_DEMAND,
    CSV_COL_SOLAR, CSV_COL_USEP, CSV_COL_RUSEP,
    CSV_COL_MAP, CSV_COL_MAPT, CSV_COL_TPC_APPLIED,
    TOMORROW_FORECAST_AVAILABLE_HOUR,
    TPC_MOVING_AVERAGE_PERIODS, TPC_PRICE_WINDOW_MAX_AGE_HOURS,
)

_LOGGER = logging.getLogger(__name__)


class USEPCoordinator(DataUpdateCoordinator):
    """Manages all USEP data fetching and processing."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN)
        self._unsub: list = []
        self._cached_periods: list[dict] = []
        self._cached_tomorrow_periods: list[dict] = []
        # Random second within the :02/:32 minute — fixed per HA instance
        self._fetch_second: int = random.randint(10, 55)
        # ── TPC rolling state (see module docstring) ────────────────────────
        # period_dt -> best-known underlying price (RUSEP if settled, else
        # the raw forecast USEP as a working estimate).
        self._price_window: dict[datetime, float] = {}
        self._last_mapt: float | None = None
        self._last_tpc_applied: bool | None = None
        _LOGGER.info(
            "USEP: confirmed fetch will run at :02:%02d and :32:%02d each hour",
            self._fetch_second, self._fetch_second,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Register time triggers and perform the initial data fetch."""
        now_sg = dt_util.now().astimezone(dt_util.get_time_zone(SG_TIMEZONE))
        # Seed the TPC rolling window with yesterday's settled prices so the
        # implied-MAP calculation works from the first cycle, not just after
        # 24h of organic uptime.
        await self._ensure_backfill(now_sg)
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
        now_sg = dt_util.now().astimezone(dt_util.get_time_zone(SG_TIMEZONE))
        # Midnight rollover check happens here too (not just on the next
        # confirmed fetch) so "USEP Current" is correct from 00:00:00
        # onward, even if EMC is unreachable at 00:02:SS.
        self._maybe_promote_tomorrow(now_sg)
        if self._cached_periods:
            self.hass.async_create_task(self._apply_cache(confirmed=False))
        else:
            self.hass.async_create_task(self.async_refresh())

    @callback
    def _on_confirmed_fetch(self, now: datetime) -> None:
        """Called at HH:02:SS and HH:32:SS — fetch fresh data from EMC."""
        self.hass.async_create_task(self.async_refresh())

    def _maybe_promote_tomorrow(self, now_sg: datetime) -> None:
        """
        Promote cached tomorrow-periods to today's cache once the date has
        rolled over, and clear the tomorrow cache at the same time.

        Runs on every period-start tick and at the top of every confirmed
        fetch, so it fires as soon as possible after midnight regardless of
        whether EMC is reachable. Being idempotent (it only acts once the
        cached today-date is actually behind), it is safe to call often.
        """
        if not self._cached_periods or not self._cached_tomorrow_periods:
            return
        today_date = now_sg.date()
        cached_date = (
            self._cached_periods[0]["period_dt"].date()
            if self._cached_periods[0].get("period_dt") else None
        )
        tomorrow_cache_date = (
            self._cached_tomorrow_periods[0]["period_dt"].date()
            if self._cached_tomorrow_periods[0].get("period_dt") else None
        )
        if (
            cached_date is not None
            and cached_date < today_date
            and tomorrow_cache_date == today_date
        ):
            _LOGGER.info(
                "USEP: midnight rollover — promoting cached tomorrow periods "
                "to today cache and clearing tomorrow cache"
            )
            self._cached_periods = self._cached_tomorrow_periods
            self._cached_tomorrow_periods = []

    async def _apply_cache(self, confirmed: bool) -> None:
        """Re-process cached periods without an HTTP fetch."""
        try:
            now_sg = dt_util.now().astimezone(dt_util.get_time_zone(SG_TIMEZONE))
            today_periods = self._augment_with_tpc(self._cached_periods, now_sg)
            data = _process(today_periods, now_sg, confirmed=confirmed)
            tomorrow_periods = (
                self._augment_with_tpc(self._cached_tomorrow_periods, now_sg)
                if self._cached_tomorrow_periods else []
            )
            data.update(_process_tomorrow(tomorrow_periods, now_sg))
            # Supplement next_usep from tomorrow cache when at the last period of today
            if data.get("next_usep") is None and tomorrow_periods:
                tomorrow_sorted = sorted(
                    tomorrow_periods,
                    key=lambda p: p["period_dt"] or datetime.min.replace(tzinfo=now_sg.tzinfo),
                )
                if tomorrow_sorted:
                    data["next_usep"] = tomorrow_sorted[0]["usep_effective"]
            self.async_set_updated_data(data)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("USEP: cache re-process failed: %s", exc)

    # ── TPC rolling window (see module docstring) ───────────────────────────────

    def _update_price_window(self, periods: list[dict], now_sg: datetime) -> None:
        """
        Upsert this batch's per-period prices into the rolling window used
        for the implied-MAP calculation, then trim anything older than we
        could ever need. Settled periods contribute RUSEP (the true
        underlying, uncapped price); forecast periods contribute their raw
        forecast USEP as a working estimate — automatically refined once
        they settle and this method is called again with real RUSEP.
        """
        for p in periods:
            pdt = p.get("period_dt")
            if pdt is None:
                continue
            value = p["rusep"] if p.get("rusep") is not None else p["usep"]
            self._price_window[pdt] = value

        cutoff = now_sg - timedelta(hours=TPC_PRICE_WINDOW_MAX_AGE_HOURS)
        for stale_dt in [dt for dt in self._price_window if dt < cutoff]:
            del self._price_window[stale_dt]

    def _augment_with_tpc(self, periods: list[dict], now_sg: datetime) -> list[dict]:
        """Update the rolling window, then annotate periods for TPC-aware display."""
        if not periods:
            return []
        periods = sorted(
            periods, key=lambda p: p["period_dt"] or datetime.min.replace(tzinfo=now_sg.tzinfo)
        )
        self._update_price_window(periods, now_sg)
        periods, self._last_mapt, self._last_tpc_applied = _apply_tpc(
            periods, self._price_window, self._last_mapt, self._last_tpc_applied,
        )
        return periods

    async def _ensure_backfill(self, now_sg: datetime) -> None:
        """
        Seed the rolling price window with yesterday's settled prices so the
        implied-MAP calculation has a full 24-hour window from the moment HA
        starts, rather than needing a full day to accumulate it organically.
        Cheap no-op once coverage already reaches back far enough — checked
        before making any HTTP call, so it's safe to call on every cycle as
        a safety net (e.g. if the very first attempt at startup failed
        because EMC was briefly unreachable).
        """
        earliest_needed = now_sg - timedelta(hours=24)
        if self._price_window and min(self._price_window) <= earliest_needed:
            return
        yesterday = (now_sg.date() - timedelta(days=1)).isoformat()
        periods = await self._fetch_periods(
            json_url=ENDPOINT_JSON.format(date=yesterday),
            csv_url=ENDPOINT_CSV.format(date=yesterday),
            base_date=date.fromisoformat(yesterday),
            label="backfill",
        )
        if periods:
            self._update_price_window(periods, now_sg)
            _LOGGER.info(
                "USEP: backfilled %d periods from %s for the TPC moving-average window",
                len(periods), yesterday,
            )
        else:
            _LOGGER.warning(
                "USEP: backfill fetch for %s failed — TPC forecasting will use "
                "carry-forward (Approach 1) until enough history accumulates",
                yesterday,
            )

    # ── Main fetch ────────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        now_sg = dt_util.now().astimezone(dt_util.get_time_zone(SG_TIMEZONE))
        today  = now_sg.strftime("%Y-%m-%d")

        # Midnight rollover: promote cached tomorrow-periods to today's cache
        # (and clear the tomorrow cache) BEFORE attempting the fetch below.
        # This runs whether the fetch that follows succeeds or fails, so
        # "Forecast Data Tomorrow" never keeps showing stale same-day data
        # just because EMC happened to be reachable this cycle.
        self._maybe_promote_tomorrow(now_sg)

        # Safety net — retries the (cheap, self-checking) backfill in case
        # the one at startup failed, e.g. EMC was briefly unreachable.
        await self._ensure_backfill(now_sg)

        periods = await self._fetch_csv(today)
        fetch_succeeded = bool(periods)

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

        # data_status only reports "confirmed" when this cycle's fetch
        # actually succeeded. A fallback to cached/promoted data — e.g.
        # NEMS stuck at the :02/:32 mark — now correctly stays
        # "forecast_pending" instead of falsely claiming "confirmed".
        today_periods = self._augment_with_tpc(periods, now_sg)
        data = _process(today_periods, now_sg, confirmed=fetch_succeeded)

        # Fetch tomorrow forecast on every cycle once past noon — EMC updates
        # the 72-period forecast continuously, so we refresh it each half-hour
        # alongside today's data rather than fetching it only once at 12:05.
        if now_sg.hour >= TOMORROW_FORECAST_AVAILABLE_HOUR:
            tomorrow_periods = await self._fetch_csv_tomorrow(today)
            if tomorrow_periods:
                self._cached_tomorrow_periods = tomorrow_periods

        # Augmenting tomorrow's periods after today's also extends the price
        # window forward and uses today's freshest carried-forward MAPT/TPC
        # state for tomorrow's (all-forecast) periods.
        tomorrow_periods_aug = (
            self._augment_with_tpc(self._cached_tomorrow_periods, now_sg)
            if self._cached_tomorrow_periods else []
        )
        data.update(_process_tomorrow(tomorrow_periods_aug, now_sg))

        # ── Change 1: next_usep at period 48 (23:30–00:00) ───────────────────
        # The today-only period list has no period 49, so next_usep is None at
        # 23:30.  If tomorrow periods are available, use the first one (00:00)
        # as the next period so the sensor stays populated through midnight.
        if data.get("next_usep") is None and tomorrow_periods_aug:
            tomorrow_sorted = sorted(
                tomorrow_periods_aug,
                key=lambda p: p["period_dt"] or datetime.min.replace(tzinfo=now_sg.tzinfo),
            )
            if tomorrow_sorted:
                data["next_usep"] = tomorrow_sorted[0]["usep_effective"]
                _LOGGER.debug(
                    "USEP: next_usep supplemented from tomorrow cache — %s $/MWh",
                    data["next_usep"],
                )

        return data

    # ── CSV fetch ─────────────────────────────────────────────────────────────

    async def _fetch_csv_tomorrow(self, today: str) -> list[dict] | None:
        """
        Fetch the 72-period forecast (value=12 endpoint), JSON-primary / CSV-fallback.

        Filters the response to return only tomorrow's 48 periods.
        Only call this after TOMORROW_FORECAST_AVAILABLE_HOUR SGT.
        """
        from datetime import date as date_t, timedelta as td
        tomorrow_date = date_t.fromisoformat(today) + td(days=1)
        all_periods = await self._fetch_periods(
            json_url=ENDPOINT_JSON_TOMORROW.format(date=today),
            csv_url=ENDPOINT_CSV_TOMORROW.format(date=today),
            base_date=date_t.fromisoformat(today),
            label="tomorrow",
        )
        if all_periods is None:
            return None
        tomorrow_periods = [
            p for p in all_periods
            if p.get("period_dt") and p["period_dt"].date() == tomorrow_date
        ]
        _LOGGER.debug(
            "USEP: tomorrow forecast — %d tomorrow periods (of %d total in response)",
            len(tomorrow_periods), len(all_periods),
        )
        return tomorrow_periods or None

    async def _fetch_csv(self, today: str) -> list[dict] | None:
        """Fetch today's 48-period data, JSON-primary / CSV-fallback."""
        from datetime import date as date_t
        return await self._fetch_periods(
            json_url=ENDPOINT_JSON.format(date=today),
            csv_url=ENDPOINT_CSV.format(date=today),
            base_date=date_t.fromisoformat(today),
            label="today",
        )

    async def _fetch_periods(
        self,
        json_url: str,
        csv_url: str,
        base_date,
        label: str,
    ) -> list[dict] | None:
        """
        Shared fetch helper: try JSON endpoint first, fall back to CSV.

        The JSON endpoint (/Get) returns an array of objects; the CSV
        endpoint (/DataDownload) returns tab-separated rows.  Both carry
        the same underlying data.
        """
        # ── Attempt 1: JSON (primary) ─────────────────────────────────────────
        try:
            async with async_timeout.timeout(20):
                async with aiohttp.ClientSession(headers=REQUEST_HEADERS) as session:
                    async with session.get(json_url) as resp:
                        if resp.status == 200:
                            payload = await resp.json(content_type=None)
                            periods = _parse_json(payload, base_date)
                            if periods:
                                _LOGGER.debug(
                                    "USEP: JSON fetch (%s) OK — %d periods", label, len(periods)
                                )
                                return periods
                            _LOGGER.warning(
                                "USEP: JSON fetch (%s) returned empty payload — trying CSV", label
                            )
                        else:
                            _LOGGER.warning(
                                "USEP: JSON endpoint (%s) returned HTTP %s — trying CSV",
                                label, resp.status,
                            )
        except Exception as exc:
            _LOGGER.warning("USEP: JSON fetch (%s) failed: %s — trying CSV", label, exc)

        # ── Attempt 2: CSV (fallback) ─────────────────────────────────────────
        try:
            async with async_timeout.timeout(20):
                async with aiohttp.ClientSession(headers=REQUEST_HEADERS) as session:
                    async with session.get(csv_url) as resp:
                        if resp.status != 200:
                            _LOGGER.warning(
                                "USEP: CSV endpoint (%s) returned HTTP %s", label, resp.status
                            )
                            return None
                        text = await resp.text()
            periods = _parse_csv(text, base_date)
            if periods:
                _LOGGER.debug("USEP: CSV fetch (%s) OK — %d periods", label, len(periods))
                return periods
            return None
        except Exception as exc:
            _LOGGER.error("USEP: CSV fetch (%s) failed: %s", label, exc)
            return None


# ── TPC (Temporary Price Cap) forecasting — pure functions, easy to unit-test ──

def _implied_map(target_dt: datetime, price_window: dict[datetime, float]) -> float | None:
    """
    Trailing 24-hour (48 half-hour period) moving average ending at and
    including target_dt, computed from the rolling price window.

    Returns None if any of the 48 slots is missing from the window — the
    caller should fall back to carry-forward (Approach 1) in that case
    rather than average over a gap.
    """
    total = 0.0
    for i in range(TPC_MOVING_AVERAGE_PERIODS):
        slot = target_dt - timedelta(minutes=30 * i)
        value = price_window.get(slot)
        if value is None:
            return None
        total += value
    return round(total / TPC_MOVING_AVERAGE_PERIODS, 2)


def _apply_tpc(
    periods: list[dict],
    price_window: dict[datetime, float],
    last_mapt: float | None,
    last_tpc_applied: bool | None,
) -> tuple[list[dict], float | None, bool | None]:
    """
    Annotate each period (in chronological order) with the fields the rest
    of the coordinator needs to treat forecast prices consistently with
    settled ones:

      usep_raw       - the value exactly as returned by EMC (unchanged)
      usep_effective - the value to actually use everywhere downstream
                        (charts, peak/lowest, current/next sensors)
      implied_map    - EMC's own MAP for settled periods; our Approach-2
                        estimate for forecast ones (None if uncomputable)
      mapt_used      - the MAPT actually used for this period's cap decision
      tpc_status     - "Capped" / "Not Capped" / "Unknown"
      tpc_method      - "confirmed" / "implied_map" / "carry_forward" / "none"

    `last_mapt` / `last_tpc_applied` carry the most recently *confirmed*
    MAPT and TPC Applied flag forward across the period list — and, via the
    return value, across coordinator update cycles — so forecast periods
    always have something to fall back on, even before the first confirmed
    fetch of the day.
    """
    for p in periods:
        p["usep_raw"] = p["usep"]

        if not p["is_forecast"]:
            # Settled — EMC has already applied (or not applied) the real
            # cap, so just pass its numbers straight through.
            p["usep_effective"] = p["usep"]
            p["implied_map"] = p.get("map_value")
            p["mapt_used"] = p.get("mapt")
            if p.get("mapt") is not None:
                last_mapt = p["mapt"]
            if p.get("tpc_applied") is not None:
                last_tpc_applied = p["tpc_applied"]
            if p.get("tpc_applied") is True:
                p["tpc_status"] = "Capped"
            elif p.get("tpc_applied") is False:
                p["tpc_status"] = "Not Capped"
            else:
                p["tpc_status"] = "Unknown"
            p["tpc_method"] = "confirmed"
            continue

        # Forecast — try the implied-MAP method (Approach 2) first.
        implied = _implied_map(p["period_dt"], price_window) if p.get("period_dt") else None
        p["implied_map"] = implied

        if implied is not None and last_mapt is not None:
            tpc_now = implied > last_mapt
            p["mapt_used"] = last_mapt
            p["tpc_status"] = "Capped" if tpc_now else "Not Capped"
            p["usep_effective"] = min(p["usep"], last_mapt) if tpc_now else p["usep"]
            p["tpc_method"] = "implied_map"
        elif last_mapt is not None and last_tpc_applied is not None:
            # Approach 1 fallback — rolling window not (yet) complete, e.g.
            # right after a fresh install before backfill lands, or a gap
            # in EMC's own data.
            p["mapt_used"] = last_mapt
            p["tpc_status"] = "Capped" if last_tpc_applied else "Not Capped"
            p["usep_effective"] = min(p["usep"], last_mapt) if last_tpc_applied else p["usep"]
            p["tpc_method"] = "carry_forward"
        else:
            # No history at all yet — nothing to base a cap decision on.
            p["mapt_used"] = None
            p["tpc_status"] = "Unknown"
            p["usep_effective"] = p["usep"]
            p["tpc_method"] = "none"

    return periods, last_mapt, last_tpc_applied


# ── Data processing (pure function, easy to unit-test) ────────────────────────

def _process(periods: list[dict], now_sg: datetime, confirmed: bool) -> dict[str, Any]:
    """
    Derive all coordinator values from a raw period list.
    Returns a flat dict consumed by sensor entities.

    Assumes `periods` has already been through `_apply_tpc` (via
    `USEPCoordinator._augment_with_tpc`), i.e. every period carries
    `usep_effective` / `usep_raw` / `tpc_status` etc. All aggregates below
    use `usep_effective` so forecast-period TPC capping is reflected
    everywhere a plain "usep" was previously used.
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

    # Peak forecast = highest effective USEP across current + all remaining periods
    remaining = ([current] if current else []) + future
    peak_forecast = max((p["usep_effective"] for p in remaining), default=None)

    # Peak today = highest effective USEP across ALL 48 periods (settled + forecast)
    peak_today = max((p["usep_effective"] for p in periods), default=None)

    # Lowest today = lowest effective USEP across ALL 48 periods (settled + forecast)
    # Use for battery charging decisions — finds the true daily minimum
    # regardless of whether it has already passed
    lowest_today_row   = min(periods, key=lambda p: p["usep_effective"]) if periods else None
    lowest_today       = lowest_today_row["usep_effective"] if lowest_today_row else None
    lowest_today_label = lowest_today_row["period"]         if lowest_today_row else None
    lowest_today_dt    = lowest_today_row["period_dt"]      if lowest_today_row else None

    # Lowest forecast = lowest effective USEP among remaining (future) periods only
    lowest_forecast = lowest_forecast_label = lowest_forecast_dt = None
    if future:
        lr = min(future, key=lambda p: p["usep_effective"])
        lowest_forecast       = lr["usep_effective"]
        lowest_forecast_label = lr["period"]
        lowest_forecast_dt    = lr["period_dt"]

    # Chart data — ISO timestamp strings as x for ApexCharts time axis.
    # Uses usep_effective so forecast spikes that TPC will actually cap
    # don't show up as misleading peaks on the dashboard graph.
    chart_usep = [
        {"x": p["period_dt"].isoformat(), "y": round(p["usep_effective"], 2)}
        for p in periods if p.get("period_dt") and p.get("usep_effective") is not None
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
            "usep":       round(p["usep_effective"], 2) if p.get("usep_effective") is not None else None,
            "usep_raw":   round(p["usep_raw"], 2) if p.get("usep_raw") is not None else None,
            "demand":     round(p["demand"]) if p.get("demand") is not None else None,
            "solar":      round(p["solar"], 1) if p.get("solar") is not None else None,
            "status":     "Forecast" if p.get("is_forecast") else "Settled",
            "tpc":        p.get("tpc_status"),
            "is_current": current is not None and p["period"] == current["period"],
        }
        for p in periods if p.get("usep_effective") is not None
    ]

    return {
        # ── Primary sensors ───────────────────────────────────────────────────
        "current_usep":           current["usep_effective"] if current else None,
        "current_usep_raw":       current["usep_raw"]        if current else None,
        "next_usep":              next_period["usep_effective"] if next_period else None,
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
        # ── TPC (Temporary Price Cap) ───────────────────────────────────────────
        "current_tpc_status":     current.get("tpc_status")  if current else None,
        "current_tpc_method":     current.get("tpc_method")  if current else None,
        "current_implied_map":    current.get("implied_map") if current else None,
        "current_mapt":           current.get("mapt_used")   if current else None,
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


def _process_tomorrow(periods: list[dict], now_sg: datetime) -> dict[str, Any]:
    """
    Derive tomorrow-forecast values from the 48-period tomorrow list.

    Returns a dict of  tomorrow_*  keys that are merged into the main
    coordinator data dict.  When no data is available (before noon or if
    the fetch failed) every value is None / False / empty-list so sensors
    degrade gracefully rather than raising KeyError.

    Assumes `periods` has already been through `_apply_tpc`, so — like
    `_process` — every aggregate below uses `usep_effective` rather than
    the raw (potentially-uncapped) forecast value.
    """
    if not periods:
        return {
            "tomorrow_available":          False,
            "peak_usep_tomorrow":          None,
            "peak_usep_tomorrow_period":   None,
            "peak_usep_tomorrow_dt":       None,
            "lowest_usep_tomorrow":        None,
            "lowest_usep_tomorrow_period": None,
            "lowest_usep_tomorrow_dt":     None,
            "avg_usep_tomorrow":           None,
            "chart_data_usep_tomorrow":    [],
            "forecast_table_tomorrow":     [],
            "periods_tomorrow":            0,
        }

    periods = sorted(
        periods,
        key=lambda p: p["period_dt"] or datetime.min.replace(tzinfo=now_sg.tzinfo),
    )

    peak_row    = max(periods, key=lambda p: p["usep_effective"])
    lowest_row  = min(periods, key=lambda p: p["usep_effective"])
    usep_values = [p["usep_effective"] for p in periods if p.get("usep_effective") is not None]
    avg_usep    = round(sum(usep_values) / len(usep_values), 2) if usep_values else None

    chart_usep = [
        {"x": p["period_dt"].isoformat(), "y": round(p["usep_effective"], 2)}
        for p in periods if p.get("period_dt") and p.get("usep_effective") is not None
    ]

    table = [
        {
            "period": p["period"],
            "usep":   round(p["usep_effective"], 2) if p.get("usep_effective") is not None else None,
            "usep_raw": round(p["usep_raw"], 2) if p.get("usep_raw") is not None else None,
            "demand": round(p["demand"]) if p.get("demand") is not None else None,
            "solar":  round(p["solar"], 1) if p.get("solar") is not None else None,
            "status": "Forecast",   # tomorrow periods are always forecast
            "tpc":    p.get("tpc_status"),
        }
        for p in periods if p.get("usep_effective") is not None
    ]

    return {
        "tomorrow_available":          True,
        "peak_usep_tomorrow":          peak_row["usep_effective"],
        "peak_usep_tomorrow_period":   peak_row["period"],
        "peak_usep_tomorrow_dt":       peak_row["period_dt"].isoformat() if peak_row["period_dt"] else None,
        "lowest_usep_tomorrow":        lowest_row["usep_effective"],
        "lowest_usep_tomorrow_period": lowest_row["period"],
        "lowest_usep_tomorrow_dt":     lowest_row["period_dt"].isoformat() if lowest_row["period_dt"] else None,
        "avg_usep_tomorrow":           avg_usep,
        "chart_data_usep_tomorrow":    chart_usep,
        "forecast_table_tomorrow":     table,
        "periods_tomorrow":            len(periods),
    }

# ── JSON parser ───────────────────────────────────────────────────────────────

def _parse_json(payload: Any, today) -> list[dict]:
    """
    Parse the EMC /Get JSON response into the same period-dict format as _parse_csv.

    The API returns a list of objects.  Known field names (discovered via
    Chrome DevTools on nems.emcsg.com/nems-prices):
      Date, Period, Demand, Solar, USEP, RUSEP  (names may vary in casing)
    plus, when tpcValue=1 is set, MAP, MAPT and "TPC Applied".
    We do case-insensitive, space-insensitive key lookup so minor
    server-side renames don't break parsing.
    """
    if not isinstance(payload, list):
        # Some responses wrap the list in a dict — try common wrapper keys
        if isinstance(payload, dict):
            for key in ("data", "Data", "result", "Result", "records"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
        if not isinstance(payload, list):
            _LOGGER.warning("USEP: JSON response has unexpected structure: %s", type(payload))
            return []

    periods = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        # Case-insensitive, space-insensitive key lookup (e.g. "TPC Applied" -> "tpcapplied")
        ikeys = {k.lower().replace(" ", ""): v for k, v in item.items()}
        usep = _to_float(ikeys.get("usep"))
        if usep is None:
            continue
        date_raw  = str(ikeys.get("date", "")).strip()
        base_date = _parse_date(date_raw) or today
        period    = str(ikeys.get("period", "")).strip()
        rusep_raw = str(ikeys.get("rusep", "-")).strip()
        periods.append({
            "period":      period,
            "period_dt":   _period_to_dt(period, base_date),
            "demand":      _to_float(ikeys.get("demand")),
            "solar":       _to_float(ikeys.get("solar")),
            "usep":        usep,
            "rusep":       _to_float(rusep_raw),
            "map_value":   _to_float(ikeys.get("map")),
            "mapt":        _to_float(ikeys.get("mapt")),
            "tpc_applied": _to_bool_yn(ikeys.get("tpcapplied")),
            "is_forecast": rusep_raw in ("-", "", "—", "null", "None"),
        })
    return periods


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
        map_raw   = row[CSV_COL_MAP].strip().strip('"')         if len(row) > CSV_COL_MAP         else "-"
        mapt_raw  = row[CSV_COL_MAPT].strip().strip('"')        if len(row) > CSV_COL_MAPT        else "-"
        tpc_raw   = row[CSV_COL_TPC_APPLIED].strip().strip('"') if len(row) > CSV_COL_TPC_APPLIED else "-"
        periods.append({
            "period":      period,
            "period_dt":   _period_to_dt(period, base_date),
            "demand":      _to_float(row[CSV_COL_DEMAND]),
            "solar":       _to_float(row[CSV_COL_SOLAR]),
            "usep":        usep,
            "rusep":       _to_float(rusep),
            "map_value":   _to_float(map_raw),
            "mapt":        _to_float(mapt_raw),
            "tpc_applied": _to_bool_yn(tpc_raw),
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


def _to_bool_yn(val: Any) -> bool | None:
    """Parse EMC's 'Yes'/'No' TPC Applied flag; blank/dash -> None (not yet known)."""
    if val is None:
        return None
    s = str(val).strip().strip('"').lower()
    if s in ("yes", "y", "true"):
        return True
    if s in ("no", "n", "false"):
        return False
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
