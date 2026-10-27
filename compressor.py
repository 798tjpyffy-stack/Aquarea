"""Compressor on/off estimate from the CSV history (the cloud has no compressor flag).

Standby ~10 W, circulation pump only ~40 W, compressor several hundred W
(15-min averages). Between power updates the Zone 1 water temperature decides:
after power-on the pump first circulates cold water (dip), an uptick from that
low means the compressor started.

Panasonic service manuals, heating "Thermostat Control" (§14.1.2.1):
  OFF: water outlet − internal water set temp > 3 °C (L series; J and older: 2 °C)
       for continuously 3 minutes; the water pump keeps running.
  ON:  after waiting 3 minutes, water outlet < water inlet at thermo OFF − 3 °C.
The inlet is not in the cloud data and the outlet only drifts down slowly after
a stop (here ~1 K per 10 min), so it is no stand-in for the inlet: a restart is
only detected from evidence (outlet rising again, or the average power).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any

COMPRESSOR_ON_KW = 0.2
COMPRESSOR_OFF_KW = 0.08
WATER_STEP_K = 1
MINUTE_MS = 60_000
STARTUP_MS = 60 * MINUTE_MS
THERMO_OFF_K = 3
THERMO_MS = 3 * MINUTE_MS
# When the unit picks up a new curve target is unknown (seemingly ~hourly), so the
# target may be any value computed during this window.
TARGET_UPDATE_MS = 60 * MINUTE_MS
# The cloud kWh total lags: the first step after a change seen in the water
# temperature may still hold energy from before it.
POWER_LAG_MS = 15 * MINUTE_MS
# A defrost (only while the compressor runs, which runs again afterwards) cools the
# water: during it and this long after, the compressor counts as running. A water
# drop this short before the defrost report is taken as the defrost itself (the
# cloud may report it late). Afterwards a lower water level is no sign of a stop
# until the water has risen again.
DEFROST_SETTLE_MS = 10 * MINUTE_MS
# history needed before a range so the estimate has settled at its start
CONTEXT = dt.timedelta(hours=2)


@dataclass
class PowerSegment:
    start: float  # ms
    end: float  # ms
    kw: float


def _ms(row: dict[str, Any]) -> float | None:
    try:
        return dt.datetime.fromisoformat(row["timestamp"]).timestamp() * 1000
    except (KeyError, TypeError, ValueError):
        return None


def _n(v: float) -> str:
    """Number as JavaScript prints it (30.0 -> "30")."""
    return str(int(v)) if float(v).is_integer() else str(v)


def _minutes(ms: float) -> int:
    return math.floor(ms / MINUTE_MS + 0.5)  # Math.round


def _num(v: Any) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _defrost(row: dict[str, Any]) -> bool:
    return str(row.get("defrost")).upper() == "ON"


def defrost_hold_until(rows: list[dict[str, Any]], now: dt.datetime, defrost_now: bool) -> dt.datetime | None:
    """End of the settle time after the last defrost, if not yet over (now + settle
    while a defrost is running); automatic actions wait until then."""
    if defrost_now:
        return now + dt.timedelta(milliseconds=DEFROST_SETTLE_MS)
    last = max((t for r in rows if _defrost(r) and (t := _ms(r)) is not None), default=None)
    if last is None:
        return None
    until = dt.datetime.fromtimestamp((last + DEFROST_SETTLE_MS) / 1000).astimezone()
    return until if until > now else None


def power_segments(rows: list[dict[str, Any]], times: list[float]) -> list[PowerSegment]:
    """Average power between consecutive changes of today's kWh total (the cloud
    updates it in steps); the first step after a start/reset has no known start."""
    segments: list[PowerSegment] = []
    prev_e: float | None = None
    last_change: float | None = None
    for row, t in zip(rows, times):
        e = _num(row.get("energy_today_kwh"))
        if e is None:
            continue
        if prev_e is not None and e < prev_e:
            last_change = None  # new day
        elif prev_e is not None and e != prev_e:
            if last_change is not None:
                segments.append(PowerSegment(last_change, t, (e - prev_e) / ((t - last_change) / 3_600_000)))
            last_change = t
        prev_e = e
    return segments


def estimate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sets compressor ("ON"/"OFF"/None), compressor_why and near_thermo_off
    ("NEAR"/"ABOVE"/None) on each row; rows must be in chronological order.
    Rows without a parsable timestamp are dropped from the result.

    Water readings are whole degrees and flicker, so a level only counts when two
    consecutive readings agree. ON: water rose WATER_STEP_K above its low since the
    last change; OFF: water fell WATER_STEP_K below its high since the last change.
    """
    pts = [(t, r) for r in rows if (t := _ms(r)) is not None]
    pts.sort(key=lambda p: p[0])
    times = [t for t, _ in pts]
    power = power_segments([r for _, r in pts], times)

    def power_at(t: float) -> PowerSegment | None:
        return next((s for s in power if s.start < t <= s.end), None)

    state: str | None = None
    low = high = prev_w = None
    prev_op: str | None = None
    on_at = trend_at = above_since = None
    defrost_until: float | None = None
    recovering = False  # after a defrost until the water rises again
    water_off: int | None = None  # index where the water last showed a stop
    j = 0
    for i, (t, p) in enumerate(pts):
        w = _num(p.get("zone1_water_temperature"))
        op = str(p.get("operation_status") or "").upper()
        if w is None or not op:  # incomplete row: keep the state
            p["compressor"] = state
            p["compressor_why"] = "unchanged" if state else None
            p["near_thermo_off"] = None
            continue
        up = w if prev_w is None else min(w, prev_w)  # level reached by both readings
        down = w if prev_w is None else max(w, prev_w)
        prev_w = w
        if op == "ON" and prev_op == "OFF":  # heat pump just switched on
            on_at, low, high = t, w, w
        prev_op = op
        low = down if low is None else min(low, down)
        high = up if high is None else max(high, up)

        # range of targets the unit may currently use
        while times[j] < t - TARGET_UPDATE_MS:
            j += 1
        targets = [v for _, r in pts[j : i + 1] if (v := _num(r.get("zone1_target_temperature"))) is not None]
        has_target = bool(targets)
        t_low, t_high = (min(targets), max(targets)) if targets else (None, None)
        t_range = (_n(t_high) if t_low == t_high else f"{_n(t_low)}–{_n(t_high)}") if has_target else ""
        # thermo OFF: above even the highest possible target + K for 3 min
        if has_target and w - t_high >= THERMO_OFF_K:
            above_since = t if above_since is None else above_since
        else:
            above_since = None
        thermo_off = above_since is not None and t - above_since >= THERMO_MS

        pw = power_at(t)
        # a 15-min average that started before (or just after, as the kWh total
        # lags) a water-detected change or a power-on mixes both states (after
        # power-on the pump circulates first; the water uptick shows the start)
        pw_ok = (
            pw is not None
            and (trend_at is None or pw.start >= trend_at + POWER_LAG_MS)
            and (on_at is None or pw.start >= on_at + POWER_LAG_MS)
        )
        since_on = _minutes(t - on_at) if on_at is not None and t - on_at < STARTUP_MS else None
        nxt, why, by_trend = state, ("unchanged" if state else None), False
        defrost = _defrost(p)
        hold = defrost or (defrost_until is not None and t < defrost_until)
        if recovering and not hold and up >= low + WATER_STEP_K:
            recovering = False
        if op == "OFF":
            nxt, why = "OFF", "operation off"
            recovering = False
        elif hold:
            if defrost:
                defrost_until = t + DEFROST_SETTLE_MS
                if state == "OFF" and water_off is not None and t - times[water_off] <= DEFROST_SETTLE_MS:
                    for _, r in pts[water_off:i]:  # that drop was this defrost
                        r.update(compressor="ON", compressor_why="defrost (before the cloud reported it)")
                        r["near_thermo_off"] = None
                    state = "ON"
            nxt = "ON"
            why = "defrost" if defrost else f"after defrost (settling {DEFROST_SETTLE_MS // MINUTE_MS} min)"
            low, high = down, up  # water levels start fresh after the defrost
            above_since, recovering = None, True
        elif state != "OFF" and thermo_off:
            nxt, by_trend = "OFF", True
            why = (
                f"thermo off: water {_n(w)} °C ≥ target {t_range} + {THERMO_OFF_K} K "
                f"for {_minutes(t - above_since)} min"
            )
        elif state != "ON" and up >= low + WATER_STEP_K:
            nxt, by_trend = "ON", True
            why = (
                f"water {_n(low)} → {_n(up)} °C after pump-start dip, {since_on} min after power on"
                if since_on is not None
                else f"water up from {_n(low)} to {_n(up)} °C"
            )
        elif state != "OFF" and not recovering and down <= high - WATER_STEP_K:
            nxt, why, by_trend = "OFF", f"water down from {_n(high)} to {_n(down)} °C", True
        elif pw_ok and pw.kw >= COMPRESSOR_ON_KW:
            nxt, why = "ON", f"avg power {pw.kw:.2f} kW"
        elif pw_ok and pw.kw <= COMPRESSOR_OFF_KW:
            nxt, why = "OFF", f"avg power {pw.kw:.2f} kW"
        if op == "ON" and state == "OFF" and nxt == "OFF" and since_on is not None:
            why = f"pump only, {since_on} min after power on"
        if recovering and not hold and why == "unchanged":
            why = "after defrost, water not risen again yet"
        if nxt == "OFF":
            recovering = False
        # NEAR: within 1 K of the off threshold (lowest possible target); ABOVE: over the
        # threshold of the highest possible target, the 3-min thermo-off timer is running
        near = None
        if nxt == "ON" and has_target:
            if w - t_high >= THERMO_OFF_K:
                near = "ABOVE"
            elif w - t_low >= THERMO_OFF_K - 1:
                near = "NEAR"
        if near == "ABOVE":
            why += (
                f" · above thermo off threshold for {_minutes(t - above_since)} min "
                f"(target {t_range} °C + {THERMO_OFF_K} K)"
            )
        elif near == "NEAR":
            why += f" · near thermo off (target {t_range} °C + {THERMO_OFF_K} K)"

        if nxt != state:
            low, high = down, up
            if by_trend:
                trend_at = t
            water_off = i if nxt == "OFF" and why.startswith("water down") else None
        state = nxt
        p["compressor"] = state
        p["compressor_why"] = why
        p["near_thermo_off"] = near
    return [r for _, r in pts]


def daily_stats(rows: list[dict[str, Any]], since: dt.datetime, max_gap: dt.timedelta) -> dict[str, Any]:
    """Compressor starts and running hours from `since` on, for rows already passed
    through estimate() (in order, with context before `since`). A run that started
    before `since` is not counted as a start; gaps longer than max_gap count as unknown,
    unless the compressor runs on both sides of a gap up to BRIDGE_GAP."""
    start_ms, gap_ms = since.timestamp() * 1000, max_gap.total_seconds() * 1000
    starts, on_ms, prev = 0, 0.0, None
    timed = [(t, r) for r in rows if (t := _ms(r)) is not None]
    for i, (t, r) in enumerate(timed):
        state = r.get("compressor")
        if state is None:
            continue
        if state == "ON" and prev == "OFF" and t >= start_ms:
            starts += 1
        prev = state
        if state == "ON" and i + 1 < len(timed):
            nt, nr = timed[i + 1]
            if nt - t <= gap_ms or (nr.get("compressor") == "ON" and nt - t <= BRIDGE_GAP_MS):
                on_ms += max(0.0, nt - max(t, start_ms))
    hours = on_ms / 3_600_000
    return {
        "starts": starts,
        "hours": round(hours, 2),
        "hours_per_start": round(hours / starts, 2) if starts else None,
        **current_state(timed),
    }


# A data gap (server stopped, computer asleep) with the same state on both sides
# is taken as one unbroken run up to this length.
BRIDGE_GAP_MS = 2 * 3_600_000


def _run_start(known: list[tuple[float, str]], end: int) -> tuple[int, bool]:
    """Index of the first row of the run ending at known[end]; True if it starts at a
    state change (False: at the data start or a gap longer than BRIDGE_GAP_MS)."""
    state = known[end][1]
    for i in range(end, 0, -1):
        t_prev, s_prev = known[i - 1]
        if known[i][0] - t_prev > BRIDGE_GAP_MS:
            return i, False
        if s_prev != state:
            return i, True
    return 0, False


def state_of(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """current_state() for rows already passed through estimate()."""
    return current_state([(t, r) for r in rows if (t := _ms(r)) is not None])


def current_state(timed: list[tuple[float, dict[str, Any]]]) -> dict[str, Any]:
    """State of the last row, since when, and while off the length of the run before."""
    known = [(t, r["compressor"]) for t, r in timed if r.get("compressor") is not None]
    if not known:
        return {"state": None, "since": None, "since_exact": False, "last_run_min": None, "last_run_exact": False}
    state = known[-1][1]
    start, exact = _run_start(known, len(known) - 1)
    since = known[start][0]
    last_run_min, last_run_exact = None, False
    if state == "OFF" and exact:
        run_start, last_run_exact = _run_start(known, start - 1)
        last_run_min = round((since - known[run_start][0]) / 60_000)
    return {
        "state": state,
        "since": dt.datetime.fromtimestamp(since / 1000).astimezone().isoformat(timespec="seconds"),
        "since_exact": exact,
        "last_run_min": last_run_min,
        "last_run_exact": last_run_exact,
    }
