"""Energy totals from HeishaMon's power values (electrical in, thermal out).

The power values hold until the next change, so each one is integrated over the
time until the next update (a step function). Intervals while the data is stale
(no MQTT messages) or longer than MAX_GAP_S are not counted. Totals are kept per
local hour (HOUR_DAYS days) and per local day, in a JSON file.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger("aquarea_energy")

MAX_GAP_S = 300
SAVE_EVERY_S = 300
HOUR_DAYS = 8
LIVE_WINDOW_S = 300  # live COP over this window
LIVE_MIN_IN_W = 100  # below this average input no live COP


def _local(t: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(t).astimezone()


def _hour_key(t: float) -> str:
    return _local(t).strftime("%Y-%m-%dT%H")


def _cop(kwh_in: float | None, kwh_out: float | None) -> float | None:
    if not kwh_in or kwh_out is None or kwh_in <= 0:
        return None
    return round(kwh_out / kwh_in, 2)


class EnergyMeter:
    def __init__(self, path: Path):
        self.path = path
        data = self._load()
        self.days: dict[str, dict[str, Any]] = data.get("days") or {}
        self.hours: dict[str, dict[str, Any]] = data.get("hours") or {}
        self.seeded = bool(data.get("seeded"))
        self._t: float | None = None
        self._p_in: float | None = None
        self._p_out: float | None = None
        self._total_in = 0.0  # kWh since start, for the live COP
        self._total_out = 0.0
        self._samples: collections.deque[tuple[float, float, float]] = collections.deque()
        self._saved_at = time.time()
        self._dirty = False

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as ex:
            _LOGGER.error("Could not read energy file %s: %s", self.path, ex)
            return {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"days": self.days, "hours": self.hours, "seeded": self.seeded}, fh, indent=1)
            os.replace(tmp, self.path)
            self._saved_at, self._dirty = time.time(), False
        except OSError as ex:
            _LOGGER.error("Could not write energy file %s: %s", self.path, ex)

    def maybe_save(self, now: float) -> None:
        if self._dirty and now - self._saved_at >= SAVE_EVERY_S:
            self.save()

    # ------------------------------------------------------------------ integration
    def _add(self, t0: float, t1: float, p_in: float | None, p_out: float | None) -> None:
        """Add constant power over [t0, t1), split at local hour boundaries."""
        while t0 < t1:
            start = _local(t0).replace(minute=0, second=0, microsecond=0)
            end = min(t1, (start + dt.timedelta(hours=1)).timestamp())
            if end <= t0:  # DST edge: move on by the remaining time
                end = t1
            hours = (end - t0) / 3600
            hk, dk = _hour_key(t0), _local(t0).date().isoformat()
            for bucket in (self.hours.setdefault(hk, {}), self.days.setdefault(dk, {})):
                if p_in is not None:
                    bucket["kwh_in"] = round(bucket.get("kwh_in", 0.0) + p_in / 1000 * hours, 6)
                if p_out is not None:
                    bucket["kwh_out"] = round((bucket.get("kwh_out") or 0.0) + p_out / 1000 * hours, 6)
            if p_in is not None:
                self._total_in += p_in / 1000 * hours
            if p_out is not None:
                self._total_out += p_out / 1000 * hours
            t0 = end
        self._dirty = True

    def update(self, now: float, p_in: float | None, p_out: float | None, alive: bool) -> None:
        """Integrate the previous power values up to now, then take the new ones."""
        if self._t is not None and alive and now > self._t and now - self._t <= MAX_GAP_S:
            if self._p_in is not None or self._p_out is not None:
                self._add(self._t, now, self._p_in, self._p_out)
        if self._t is None or now > self._t:
            self._t = now
        self._p_in, self._p_out = (p_in, p_out) if alive else (None, None)
        self._samples.append((now, self._total_in, self._total_out))
        while self._samples and self._samples[0][0] < now - LIVE_WINDOW_S - 60:
            self._samples.popleft()
        if len(self.hours) > HOUR_DAYS * 24 + 2:
            for key in sorted(self.hours)[: len(self.hours) - HOUR_DAYS * 24]:
                del self.hours[key]

    def seed(self, daily_cloud_kwh: dict[str, float], today: str) -> None:
        """Daily consumption from the old cloud history (only past days without own data)."""
        for day, kwh in daily_cloud_kwh.items():
            if day not in self.days and day < today:
                self.days[day] = {"kwh_in": round(kwh, 3), "source": "cloud"}
        self.seeded = True
        self.save()

    # ------------------------------------------------------------------ views
    def live(self, now: float, running: bool) -> dict[str, Any]:
        """Power now and COP over the last LIVE_WINDOW_S (None while the compressor is off)."""
        old = next((s for s in self._samples if s[0] >= now - LIVE_WINDOW_S), None)
        cop, avg_in = None, None
        if old is not None and self._samples and now - old[0] >= 60:
            span_h = (self._samples[-1][0] - old[0]) / 3600
            d_in = self._total_in - old[1]
            d_out = self._total_out - old[2]
            avg_in = d_in * 1000 / span_h if span_h > 0 else None
            if running and avg_in is not None and avg_in >= LIVE_MIN_IN_W:
                cop = round(d_out / d_in, 2)
        return {
            "power_in_w": self._p_in,
            "power_out_w": self._p_out,
            "cop": cop,
            "avg_in_w": None if avg_in is None else round(avg_in),
            "window_min": LIVE_WINDOW_S // 60,
        }

    def today(self, now: float) -> dict[str, Any]:
        day = _local(now).date().isoformat()
        b = self.days.get(day) or {}
        return {
            "date": day,
            "kwh_in": round(b["kwh_in"], 3) if "kwh_in" in b else None,
            "kwh_out": round(b["kwh_out"], 3) if b.get("kwh_out") is not None else None,
            "cop": _cop(b.get("kwh_in"), b.get("kwh_out")),
        }

    def public(self, now: float, running: bool) -> dict[str, Any]:
        local = _local(now)
        day, month, year = local.date().isoformat(), local.strftime("%Y-%m"), local.strftime("%Y")

        def item(label: str, b: dict[str, Any]) -> dict[str, Any]:
            kin, kout = b.get("kwh_in"), b.get("kwh_out")
            return {
                "label": label,
                "kwh_in": None if kin is None else round(kin, 3),
                "kwh_out": None if kout is None else round(kout, 3),
                # months: only from days with both values (cloud days have no output)
                "cop": _cop(b.get("cop_in", kin), b.get("cop_out", kout)),
                "source": b.get("source"),
            }

        hours = [item(k[11:], b) for k, b in sorted(self.hours.items()) if k.startswith(day)]
        days = [item(k[8:], b) for k, b in sorted(self.days.items()) if k.startswith(month)]
        months: dict[str, dict[str, Any]] = {}
        for k, b in self.days.items():
            if k.startswith(year):
                m = months.setdefault(k[5:7], {})
                for key in ("kwh_in", "kwh_out"):
                    if b.get(key) is not None:
                        m[key] = m.get(key, 0.0) + b[key]
                if b.get("kwh_in") is not None and b.get("kwh_out") is not None:
                    m["cop_in"] = m.get("cop_in", 0.0) + b["kwh_in"]
                    m["cop_out"] = m.get("cop_out", 0.0) + b["kwh_out"]
                if b.get("source") == "cloud":
                    m["source"] = "cloud"
        return {
            "live": self.live(now, running),
            "today": self.today(now),
            "day": {"date": day, "items": hours},
            "month": {"date": month, "items": days},
            "year": {"date": year, "items": [item(k, b) for k, b in sorted(months.items())]},
        }
