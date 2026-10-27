"""Learned room cooling model.

While the compressor is off, the room cools down following

    dT_in/dt = -k * (T_in - T_out) + g + e(t)

  k     heat loss (1/h; the building time constant is 1/k hours)
  g     constant internal gains (K/h), 0 unless the data shows them
  e(t)  people: `evening` K/h from DAY_END until ACTIVE_END (cooking, lights,
        people), afterwards `release` K/h decaying with `release_tau` hours
        (the air and light surfaces warmed in the evening give their extra
        heat back to the walls and floor, the room cools faster at first)

This module collects samples of the cooling rate from the CSV history and
keeps them in a small JSON file:

* Only compressor-off stretches are used, starting SETTLE after the
  compressor stopped, without defrost or hot water, and only from 17:00 to
  08:00 local time (no sun).
* Each stretch is cut into chunks of up to CHUNK; a chunk needs at least
  MIN_SPAN and MIN_POINTS rows. Its least-squares slope is the sample rate
  (K/h), together with the mean outdoor and indoor temperature.

The model is fitted robustly (medians) once a day, or as soon as new samples
arrive while there is no valid model yet:

1. k (and g) from the quiet night samples starting between QUIET_FROM and
   DAY_START: g only with G_SPREAD K spread of T_in - T_out (Theil-Sen),
   otherwise k = median(-rate / (T_in - T_out)).
2. `evening` from the residuals of samples mostly before ACTIVE_END,
   `release` and `release_tau` (grid) from the residuals of the samples after.

The projection steps through time (STEP) with the outdoor temperature from
the hourly forecast, shifted to the unit's current reading; a rising rate
counts as flat (no gains from the sun are learned, so daytime projections
are on the cool side).

On the first run the whole history is scanned; afterwards only the rows
since the last scan (with LOOKBACK overlap) are read. Changing the indoor
sensor starts a new collection.
"""

from __future__ import annotations

import bisect
import datetime as dt
import itertools
import json
import logging
import math
import os
import statistics
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable

import compressor
from history import HA_PREFIX, read_rows

_LOGGER = logging.getLogger(__name__)

SETTLE = dt.timedelta(minutes=30)
CHUNK = dt.timedelta(hours=2)
MIN_SPAN = dt.timedelta(minutes=60)
MIN_POINTS = 5
MAX_GAP = dt.timedelta(minutes=10)
DAY_START, DAY_END = 8, 17  # local hours without samples (sun)
MAX_SAMPLES = 1000
LOOKBACK = dt.timedelta(hours=18)
UPDATE_INTERVAL = dt.timedelta(minutes=30)

ACTIVE_END = 22  # local hour until which people add heat in the evening
QUIET_FROM = 0  # samples starting from this hour until DAY_START give k
MIN_QUIET = 15  # quiet samples needed for a model
MIN_GROUP = 5  # samples needed for the evening / release terms
MIN_DELTA = 3.0  # K indoor - outdoor; below that the rate says nothing about k
G_SPREAD = 8.0  # K spread of indoor - outdoor needed to fit the gains g
MAX_K = 0.2  # 1/h (5 h time constant); larger means a broken fit
RELEASE_TAUS = (0.5, 1.0, 1.5, 2.0, 3.0)  # h
REFIT_INTERVAL = dt.timedelta(hours=24)
STEP = dt.timedelta(minutes=10)


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _parse(ts: Any) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()


def slope(points: list[tuple[dt.datetime, float]]) -> float | None:
    """Least-squares slope of (time, value) points in K/h."""
    if len(points) < 2:
        return None
    t0 = points[0][0]
    xs = [(t - t0).total_seconds() / 3600 for t, _ in points]
    ys = [v for _, v in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


def _daytime(t: dt.datetime) -> bool:
    return DAY_START <= t.astimezone().hour < DAY_END


def _chunks(points: list[tuple], closed: bool) -> list[list[tuple]]:
    """Cut a stretch into CHUNK windows; an open stretch only yields full ones."""
    out = []
    i = 0
    while i < len(points):
        w0 = points[i][0]
        j = i
        while j < len(points) and points[j][0] < w0 + CHUNK:
            j += 1
        if j < len(points) or closed:
            out.append(points[i:j])
        i = j
    return out


def _sample(points: list[tuple], unit_off: bool) -> dict[str, Any] | None:
    if len(points) < MIN_POINTS or points[-1][0] - points[0][0] < MIN_SPAN:
        return None
    rate = slope([(t, indoor) for t, indoor, _ in points])
    outdoors = [o for _, _, o in points if o is not None]
    if rate is None or not outdoors:
        return None
    return {
        "start": points[0][0].isoformat(timespec="seconds"),
        "end": points[-1][0].isoformat(timespec="seconds"),
        "outdoor": round(statistics.fmean(outdoors), 1),
        "indoor": round(statistics.fmean(i for _, i, _ in points), 2),
        "rate": round(rate, 3),
        "points": len(points),
        "unit_off": unit_off,
    }


def extract_samples(rows: list[dict[str, Any]], column: str) -> list[dict[str, Any]]:
    """Cooling samples from rows processed by compressor.estimate()."""
    samples: list[dict[str, Any]] = []
    stretch: list[tuple] = []
    stretch_off = False

    def close(closed: bool = True) -> None:
        nonlocal stretch
        for chunk in _chunks(stretch, closed):
            sample = _sample(chunk, stretch_off)
            if sample:
                samples.append(sample)
        stretch = []

    stop: dt.datetime | None = None  # compressor stop time, None = unknown
    was_on = False
    prev: dt.datetime | None = None
    for row in rows:
        if row.get("operation_status") not in ("ON", "OFF"):
            continue  # device list rows without status
        t = _parse(row.get("timestamp"))
        if t is None:
            continue
        if prev is not None and t - prev > MAX_GAP:
            close()
        prev = t
        comp = row.get("compressor")
        if comp != "OFF":
            close()
            if comp == "ON":
                was_on = True
            else:
                was_on, stop = False, None
            continue
        if was_on:
            stop, was_on = t, False
        if stop is None:
            continue  # stop time unknown (start of the data)
        indoor = _num(row.get(column))
        unit_off = row.get("operation_status") == "OFF"
        excluded = (
            indoor is None
            or row.get("defrost") == "ON"
            or row.get("direction") == "WATER"
            or _daytime(t)
        )
        if excluded or (stretch and unit_off != stretch_off):
            close()
        if excluded or t < stop + SETTLE:
            continue
        stretch.append((t, indoor, _num(row.get("outdoor_temperature"))))
        stretch_off = unit_off
    close(closed=False)
    return samples


def _hour(t: dt.datetime) -> float:
    local = t.astimezone()
    return local.hour + local.minute / 60 + local.second / 3600


def _active(t: dt.datetime) -> bool:
    return DAY_END <= _hour(t) < ACTIVE_END


def _release_basis(t: dt.datetime, tau: float) -> float:
    """exp(-hours since ACTIVE_END / tau); 0 while people are active."""
    return 0.0 if _active(t) else math.exp(-((_hour(t) - ACTIVE_END) % 24) / tau)


@dataclass(frozen=True)
class CoolingModel:
    k: float  # 1/h
    g: float = 0.0  # K/h
    evening: float = 0.0  # K/h from DAY_END until ACTIVE_END
    release: float = 0.0  # K/h at ACTIVE_END, decaying
    release_tau: float = 1.0  # h
    n: int = 0  # samples fitted
    n_quiet: int = 0  # quiet night samples behind k (and g)
    mad: float | None = None  # median absolute rate error over the samples (K/h)

    def rate(self, indoor: float, outdoor: float, t: dt.datetime) -> float:
        """Cooling rate (K/h, <= 0: a rising rate counts as flat)."""
        extra = self.evening if _active(t) else self.release * _release_basis(t, self.release_tau)
        return min(-self.k * (indoor - outdoor) + self.g + extra, 0.0)

    def project(
        self,
        indoor: float,
        start: dt.datetime,
        end: dt.datetime,
        outdoor: Callable[[dt.datetime], float],
        target: float | None = None,
    ) -> tuple[float, dt.datetime | None]:
        """Indoor temperature at `end`, and when it first falls below `target` (None if not by then)."""
        t, temp, below = start, indoor, None
        while t < end:
            step = min(STEP, end - t)
            temp += self.rate(temp, outdoor(t), t) * step.total_seconds() / 3600
            t += step
            if target is not None and below is None and temp < target:
                below = t
        return temp, below

    @classmethod
    def from_dict(cls, data: Any) -> CoolingModel | None:
        if not isinstance(data, dict):
            return None
        names = {f.name for f in fields(cls)}
        values = {key: value for key, value in data.items() if key in names}
        if not all(_num(values.get(key)) is not None for key in ("k", "g", "evening", "release", "release_tau")):
            return None
        if not 0 < values["k"] <= MAX_K or values["release_tau"] <= 0:
            return None
        return cls(**values)


def _sample_times(sample: dict[str, Any]) -> list[dt.datetime]:
    start, end = _parse(sample.get("start")), _parse(sample.get("end"))
    if start is None or end is None or end < start:
        return []
    times = []
    while start <= end:
        times.append(start)
        start += STEP
    return times


def _theil_sen(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Robust line y = m x + b: median of the pairwise slopes."""
    slopes = [(y2 - y1) / (x2 - x1) for (x1, y1), (x2, y2) in itertools.combinations(points, 2) if abs(x2 - x1) >= 0.5]
    if not slopes:
        return None
    m = statistics.median(slopes)
    return m, statistics.median(y - m * x for x, y in points)


def fit_model(samples: list[dict[str, Any]]) -> tuple[CoolingModel | None, str]:
    """Fitted model, or None, with a short description of the fit."""
    rows = []  # (delta, rate, active fraction, times, quiet)
    for s in samples:
        times = _sample_times(s)
        indoor, outdoor, rate = _num(s.get("indoor")), _num(s.get("outdoor")), _num(s.get("rate"))
        if not times or indoor is None or outdoor is None or rate is None:
            continue
        active = sum(_active(t) for t in times) / len(times)
        quiet = QUIET_FROM <= _hour(times[0]) < DAY_START
        rows.append((indoor - outdoor, rate, active, times, quiet))
    quiet = [(d, r) for d, r, _, _, q in rows if q and d >= MIN_DELTA]
    if len(quiet) < MIN_QUIET:
        return None, f"{len(quiet)} of {MIN_QUIET} night samples (after {QUIET_FROM:02d}:00)"
    k, g = None, 0.0
    deltas = [d for d, _ in quiet]
    if max(deltas) - min(deltas) >= G_SPREAD:
        line = _theil_sen(quiet)
        if line and line[0] < 0 and line[1] > 0:
            k, g = -line[0], line[1]
    if k is None:
        k = statistics.median(-r / d for d, r in quiet)
    if not 0 < k <= MAX_K:
        return None, f"implausible heat loss {k:.4f}/h from {len(quiet)} night samples"
    res = [r + k * d - g for d, r, _, _, _ in rows]
    evening = 0.0
    active = [(e, a) for e, (_, _, a, _, _) in zip(res, rows) if a >= 0.5]
    if len(active) >= MIN_GROUP:
        evening = statistics.median(e / a for e, a in active)
    res = [e - evening * a for e, (_, _, a, _, _) in zip(res, rows)]
    release, release_tau = 0.0, 1.0
    later = [i for i, (_, _, a, _, q) in enumerate(rows) if a < 0.5 and not q]
    if len(later) >= MIN_GROUP:
        best = None
        for tau in RELEASE_TAUS:
            basis = [statistics.fmean(_release_basis(t, tau) for t in times) for _, _, _, times, _ in rows]
            use = [i for i in later if basis[i] >= 0.05]
            if len(use) < MIN_GROUP:
                continue
            p = statistics.median(res[i] / basis[i] for i in use)
            err = statistics.median(abs(e - p * b) for e, b in zip(res, basis))
            if best is None or err < best[0]:
                best = (err, p, tau)
        if best:
            _, release, release_tau = best
    model = CoolingModel(
        k=round(k, 5), g=round(g, 4), evening=round(evening, 4), release=round(release, 4),
        release_tau=release_tau, n=len(rows), n_quiet=len(quiet),
    )
    errors = [abs(statistics.fmean(model.rate(d, 0.0, t) for t in times) - r) for d, r, _, times, _ in rows]
    model = CoolingModel(**{**asdict(model), "mad": round(statistics.median(errors), 4)})
    return model, f"fitted from {len(rows)} samples"


def _interpolate(points: list[tuple[dt.datetime, float]], t: dt.datetime) -> float:
    i = bisect.bisect_left(points, t, key=lambda p: p[0])
    if i <= 0:
        return points[0][1]
    if i >= len(points):
        return points[-1][1]
    (t0, v0), (t1, v1) = points[i - 1], points[i]
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def outdoor_path(
    now: dt.datetime, outdoor: float, hourly: list[tuple[dt.datetime, float]]
) -> Callable[[dt.datetime], float]:
    """Outdoor temperature over time: the hourly forecast shifted to the unit's current
    reading (the unit's sensor differs from the forecast), else constant."""
    points = sorted(hourly)
    if len(points) < 2 or not points[0][0] <= now <= points[-1][0]:
        return lambda t: outdoor
    offset = outdoor - _interpolate(points, now)
    return lambda t: _interpolate(points, t) + offset


class CoolingStore:
    """Cooling samples per device, stored in a JSON file (mode 600)."""

    def __init__(self, path: Path):
        self.path = path
        self.devices: dict[str, dict[str, Any]] = self._load()
        self._updated: dict[str, dt.datetime] = {}

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as ex:
            _LOGGER.error("Could not read cooling file %s: %s", self.path, ex)
            return {}
        devices = data.get("devices") if isinstance(data, dict) else None
        result = {}
        for device_id, entry in (devices or {}).items():
            if not isinstance(entry, dict):
                continue
            samples = [
                s
                for s in entry.get("samples") or []
                if isinstance(s, dict) and _num(s.get("rate")) is not None and _num(s.get("outdoor")) is not None
            ]
            result[device_id] = {
                "sensor": entry.get("sensor"),
                "processed_until": entry.get("processed_until"),
                "samples": samples[-MAX_SAMPLES:],
                "model": entry.get("model") if CoolingModel.from_dict(entry.get("model")) else None,
                "fitted_at": entry.get("fitted_at"),
                "fit_note": entry.get("fit_note"),
            }
        return result

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"devices": self.devices}, fh, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as ex:
            _LOGGER.error("Could not write cooling file %s: %s", self.path, ex)

    def due(self, device_id: str, now: dt.datetime) -> bool:
        last = self._updated.get(device_id)
        return last is None or now - last >= UPDATE_INTERVAL

    def update(self, device_id: str, history_file: Path, sensor: str, now: dt.datetime) -> int:
        """Add new samples from the history and refit the model when due; returns the
        number of samples added. Blocking."""
        self._updated[device_id] = now
        entry = self.devices.get(device_id)
        if entry is None or entry.get("sensor") != sensor:
            entry = {"sensor": sensor, "processed_until": None, "samples": [], "model": None,
                     "fitted_at": None, "fit_note": None}
            self.devices[device_id] = entry
            changed = True
        else:
            changed = False
        until = _parse(entry["processed_until"]) if entry.get("processed_until") else None
        rows = read_rows(history_file, until - LOOKBACK if until else None, device_id)
        new: list[dict[str, Any]] = []
        if rows:
            found = extract_samples(compressor.estimate(rows), f"{HA_PREFIX}{sensor}")
            known = [(s["start"], s["end"]) for s in entry["samples"]]
            new = [
                s for s in found
                if not any(s["start"] <= end and start <= s["end"] for start, end in known)
            ]
            last_ts = rows[-1].get("timestamp")
            if new or last_ts != entry.get("processed_until"):
                samples = sorted(entry["samples"] + new, key=lambda s: s["start"])[-MAX_SAMPLES:]
                entry = {**entry, "processed_until": last_ts, "samples": samples}
                changed = True
        if new:
            _LOGGER.info("Cooling samples for %s: %d new, %d total", device_id, len(new), len(entry["samples"]))
        fitted = _parse(entry.get("fitted_at")) if entry.get("fitted_at") else None
        if fitted is None or now - fitted >= REFIT_INTERVAL or (new and entry.get("model") is None):
            model, note = fit_model(entry["samples"])
            entry = {
                **entry,
                "model": asdict(model) if model else None,
                "fitted_at": now.isoformat(timespec="seconds"),
                "fit_note": note,
            }
            changed = True
            _LOGGER.info("Cooling model for %s: %s (%s)", device_id, entry["model"], note)
        if changed:
            self.devices[device_id] = entry
            self.save()
        return len(new)

    def model(self, device_id: str) -> CoolingModel | None:
        return CoolingModel.from_dict((self.devices.get(device_id) or {}).get("model"))

    def public(self, device_id: str) -> dict[str, Any]:
        entry = self.devices.get(device_id) or {}
        return {
            "samples": len(entry.get("samples") or []),
            "processed_until": entry.get("processed_until"),
            "model": entry.get("model"),
            "fitted_at": entry.get("fitted_at"),
            "note": entry.get("fit_note"),
            "active_end": ACTIVE_END,
        }
