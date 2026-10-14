"""Hourly temperature forecast from Open-Meteo (no API key) for the night-time shutoff.

The location comes from --location or from the Home Assistant configuration and
is rounded to 0.01° (about 1 km) before it is sent to Open-Meteo.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
from typing import Any

import aiohttp

from ha_sensors import HAConfig

_LOGGER = logging.getLogger("aquarea_web.forecast")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REFRESH = dt.timedelta(hours=1)
RETRY = dt.timedelta(minutes=10)
MAX_AGE = dt.timedelta(hours=4)  # older forecasts are not used
TIMEOUT = aiohttp.ClientTimeout(total=20)
MIN_DAY_HOURS = 20  # hourly values needed for a daily mean


def parse_location(text: str) -> tuple[float, float]:
    """'48.14,11.58' -> (48.14, 11.58)."""
    try:
        lat, lon = (float(p) for p in text.split(","))
    except ValueError:
        raise ValueError("expected LAT,LON, e.g. 48.14,11.58") from None
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("latitude must be within ±90 and longitude within ±180")
    return round(lat, 2), round(lon, 2)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


class Forecast:
    def __init__(self, location: tuple[float, float] | None = None):
        self.location = location
        self.hourly: list[tuple[dt.datetime, float]] = []
        self.fetched_at: dt.datetime | None = None
        self.attempt_at: dt.datetime | None = None
        self.error: str | None = None

    async def _ha_location(self, http: aiohttp.ClientSession, config: HAConfig) -> tuple[float, float] | None:
        async with http.get(
            f"{config.url}/api/config", headers={"Authorization": f"Bearer {config.token}"}, timeout=TIMEOUT
        ) as response:
            response.raise_for_status()
            data = await response.json()
        lat, lon = _number(data.get("latitude")), _number(data.get("longitude"))
        if lat is None or lon is None:
            return None
        return parse_location(f"{lat},{lon}")

    async def refresh(self, http: aiohttp.ClientSession, ha_config: HAConfig | None) -> None:
        """Fetch a new forecast if the current one is older than REFRESH. Never raises."""
        now = dt.datetime.now(dt.timezone.utc)
        if self.fetched_at and now - self.fetched_at < REFRESH:
            return
        if self.attempt_at and now - self.attempt_at < RETRY:
            return
        self.attempt_at = now
        try:
            if self.location is None and ha_config is not None:
                self.location = await self._ha_location(http, ha_config)
            if self.location is None:
                self.error = "location unknown (start with --location LAT,LON or configure Home Assistant)"
                return
            params = {
                "latitude": self.location[0],
                "longitude": self.location[1],
                "hourly": "temperature_2m",
                "temperature_unit": "celsius",
                "timeformat": "unixtime",
                "past_days": 1,
                "forecast_days": 3,
            }
            async with http.get(FORECAST_URL, params=params, timeout=TIMEOUT) as response:
                response.raise_for_status()
                data = await response.json()
            hourly = data.get("hourly") if isinstance(data, dict) else None
            times = hourly.get("time") if isinstance(hourly, dict) else None
            temps = hourly.get("temperature_2m") if isinstance(hourly, dict) else None
            if not isinstance(times, list) or not isinstance(temps, list) or len(times) != len(temps):
                raise ValueError("unexpected forecast response")
            points = []
            for t, v in zip(times, temps):
                t, v = _number(t), _number(v)
                if t is not None and v is not None:
                    points.append((dt.datetime.fromtimestamp(t, dt.timezone.utc), v))
            if not points:
                raise ValueError("forecast contains no temperatures")
            self.hourly, self.fetched_at, self.error = points, now, None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as ex:
            self.error = f"forecast unavailable ({type(ex).__name__})"
            _LOGGER.warning("Weather forecast unavailable: %s", ex)

    def summary(self, window: tuple[dt.datetime, dt.datetime] | None) -> dict[str, Any]:
        """Mean temperature of the day the night ends on and the lowest temperature of the night."""
        now = dt.datetime.now(dt.timezone.utc)
        result: dict[str, Any] = {
            "source": "Open-Meteo",
            "fetched_at": self.fetched_at.isoformat(timespec="seconds") if self.fetched_at else None,
            "day": None,
            "mean": None,
            "night_start": None,
            "night_end": None,
            "night_min": None,
            "error": self.error,
        }
        if window is None:
            result["error"] = "no night hours set"
            return result
        start, end = window
        result.update(day=end.date().isoformat(), night_start=start.isoformat(), night_end=end.isoformat())
        if not self.hourly or self.fetched_at is None or now - self.fetched_at > MAX_AGE:
            result["error"] = self.error or "no current forecast"
            return result
        day = [v for t, v in self.hourly if t.astimezone().date() == end.date()]
        if len(day) >= MIN_DAY_HOURS:
            result["mean"] = round(sum(day) / len(day), 1)
        night = [(t, v) for t, v in self.hourly if start <= t <= end]
        hour = dt.timedelta(hours=1)
        if night and night[0][0] <= start + hour and night[-1][0] >= end - hour:
            result["night_min"] = round(min(v for _, v in night), 1)
        return result
