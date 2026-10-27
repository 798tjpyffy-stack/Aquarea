"""Thermo off / restart of the compressor, from HeishaMon's live values.

Panasonic service manual (L series), heating thermostat control:
  OFF: water outlet − water target (Main_Target_Temp) > 3 °C continuously for 3 min.
  ON:  after 3 min, water outlet < water inlet at thermo OFF − 3 °C.
The OFF timer is followed live (start at the first reading above the threshold,
reset at a reading at or below it or a target change), so a shift step can be
sent shortly before the unit would stop.

Every stop is checked against the OFF rule (time above the threshold), every
restart is recorded with the values that may decide it. The ON rule above does
not match observations (the restart depends on the target), so the rule is
learned: candidates "outlet − target at restart" and "outlet − (inlet at stop −
3 K) at restart"; with RESTART_MIN restarts the one with the smaller spread
gives a restart estimate.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import os
import statistics
from pathlib import Path
from typing import Any

from compressor import THERMO_MS, THERMO_OFF_K

_LOGGER = logging.getLogger("aquarea_thermo")

THERMO_S = THERMO_MS / 1000
KEEP = 100  # stored stop / restart events each
RESTART_MIN = 5
RESTART_MAX_SPREAD = 0.5  # K, median absolute deviation
MIN_PAUSE_S = 180  # shorter pauses are no thermo cycle
SLOPE_WINDOW_S = 15 * 60
# a stop counts as the thermo off rule if the outlet was above the threshold this long
STOP_RULE_RANGE_S = (THERMO_S - 30, THERMO_S + 60)


def _iso(t: float | None) -> str | None:
    return None if t is None else dt.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def _r(v: float | None, d: int = 1) -> float | None:
    return None if v is None else round(v, d)


def _fit(values: list[float]) -> tuple[float, float]:
    med = statistics.median(values)
    return med, statistics.median(abs(v - med) for v in values)


class ThermoWatch:
    def __init__(self, path: Path):
        self.path = path
        data = self._load()
        self.stops: list[dict[str, Any]] = [e for e in data.get("stops") or [] if isinstance(e, dict)][-KEEP:]
        self.restarts: list[dict[str, Any]] = [e for e in data.get("restarts") or [] if isinstance(e, dict)][-KEEP:]
        self.over_since: float | None = None  # outlet above target + K since (live timer)
        self._over_target: float | None = None
        self._running: bool | None = None
        self.running_since: float | None = None  # last compressor on/off edge seen live
        self._pause: dict[str, Any] | None = None  # values at the last thermo stop
        self._last: dict[str, Any] = {}
        self._outlets: collections.deque[tuple[float, float]] = collections.deque()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as ex:
            _LOGGER.error("Could not read thermo file %s: %s", self.path, ex)
            return {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"stops": self.stops, "restarts": self.restarts}, fh, indent=1)
            os.replace(tmp, self.path)
        except OSError as ex:
            _LOGGER.error("Could not write thermo file %s: %s", self.path, ex)

    @property
    def running(self) -> bool | None:
        return self._running

    # ------------------------------------------------------------------ live
    def update(
        self,
        t: float,
        outlet: float | None,
        inlet: float | None,
        target: float | None,
        freq: float | None,
        defrost: bool,
        unit_on: bool | None,
        shift: float | None,
    ) -> None:
        if outlet is not None:
            self._outlets.append((t, outlet))
            while self._outlets and self._outlets[0][0] < t - SLOPE_WINDOW_S:
                self._outlets.popleft()
        # OFF timer: strictly above target + K, continuously, same target
        if outlet is None or target is None:
            pass
        elif outlet - target > THERMO_OFF_K and not defrost:
            if self.over_since is None or self._over_target != target:
                self.over_since, self._over_target = t, target
        else:
            self.over_since = self._over_target = None

        running = None if freq is None else freq > 0
        prev_outlet = self._last.get("outlet")
        if running is not None and self._running is not None and running != self._running:
            self.running_since = t
            if unit_on is False:
                self._pause = None  # the unit was switched off: no thermo cycle
            elif not running and not defrost:
                self._stop(t, outlet, inlet, target, shift)
            elif running and self._pause is not None:
                self._restart(t, prev_outlet if prev_outlet is not None else outlet, inlet, target, shift)
        if unit_on is False:
            self._pause = None
        if running is not None:
            self._running = running
        self._last = {"outlet": outlet, "inlet": inlet, "target": target, "shift": shift, "t": t}

    def _stop(self, t: float, outlet: float | None, inlet: float | None, target: float | None, shift: float | None) -> None:
        over_s = None if self.over_since is None else round(t - self.over_since)
        lo, hi = STOP_RULE_RANGE_S
        rule = over_s is not None and lo <= over_s <= hi
        event = {
            "time": _iso(t),
            "outlet": outlet,
            "inlet": inlet,
            "target": target,
            "shift": shift,
            "diff": _r(outlet - target) if outlet is not None and target is not None else None,
            "over_s": over_s,
            "rule": rule,
        }
        self.stops = (self.stops + [event])[-KEEP:]
        self._pause = {"t": t, "inlet": inlet, "outlet": outlet, "target": target}
        if not rule:
            _LOGGER.warning(
                "Compressor stop not explained by the thermo off rule: outlet %s, target %s, above target + %s K for %s s",
                outlet, target, THERMO_OFF_K, over_s,
            )
        self.save()

    def _restart(self, t: float, outlet: float | None, inlet: float | None, target: float | None, shift: float | None) -> None:
        pause = self._pause or {}
        self._pause = None
        pause_s = round(t - pause["t"]) if "t" in pause else None
        if pause_s is None or pause_s < MIN_PAUSE_S:
            return
        inlet_off = pause.get("inlet")
        event = {
            "time": _iso(t),
            "pause_s": pause_s,
            "outlet": outlet,
            "inlet": inlet,
            "target": target,
            "shift": shift,
            "target_at_stop": pause.get("target"),
            "inlet_at_stop": inlet_off,
            "d_target": _r(outlet - target) if outlet is not None and target is not None else None,
            "d_inlet": (
                _r(outlet - (inlet_off - THERMO_OFF_K)) if outlet is not None and inlet_off is not None else None
            ),
        }
        self.restarts = (self.restarts + [event])[-KEEP:]
        _LOGGER.info("Compressor restart: %s", event)
        self.save()

    # ------------------------------------------------------------------ views
    def restart_rule(self) -> dict[str, Any]:
        """The learned restart rule: outlet at restart relative to target or inlet at stop."""
        recent = self.restarts[-20:]
        fits = {}
        for key, text in (("d_target", "target"), ("d_inlet", "inlet at stop − 3 K")):
            values = [e[key] for e in recent if isinstance(e.get(key), (int, float))]
            if len(values) >= 2:
                med, spread = _fit(values)
                fits[key] = {"offset": round(med, 2), "spread": round(spread, 2), "n": len(values), "relative_to": text}
        best = min(fits.values(), key=lambda f: f["spread"], default=None)
        ready = best is not None and best["n"] >= RESTART_MIN and best["spread"] <= RESTART_MAX_SPREAD
        return {"candidates": fits, "best": best, "ready": ready, "restarts": len(self.restarts)}

    def _slope(self) -> float | None:
        """Outlet trend in K/s over SLOPE_WINDOW_S (least squares)."""
        pts = list(self._outlets)
        if len(pts) < 3 or pts[-1][0] - pts[0][0] < 120:
            return None
        n = len(pts)
        mt = sum(t for t, _ in pts) / n
        mv = sum(v for _, v in pts) / n
        den = sum((t - mt) ** 2 for t, _ in pts)
        return sum((t - mt) * (v - mv) for t, v in pts) / den if den else None

    def public(self, now: float, step_lead_s: int | None = None) -> dict[str, Any]:
        last = self._last
        outlet, target = last.get("outlet"), last.get("target")
        timer = None
        if self.over_since is not None:
            off_at = self.over_since + THERMO_S
            timer = {
                "since": _iso(self.over_since),
                "off_at": _iso(off_at),
                "step_at": _iso(off_at - step_lead_s) if step_lead_s is not None else None,
            }
        rule = self.restart_rule()
        restart = None
        if self._running is False and self._pause is not None and rule["ready"] and outlet is not None:
            best = rule["best"]
            base = target if best["relative_to"] == "target" else (
                None if self._pause.get("inlet") is None else self._pause["inlet"] - THERMO_OFF_K
            )
            if base is not None:
                threshold = base + best["offset"]
                slope = self._slope()
                eta = None
                if outlet <= threshold:
                    eta = now
                elif slope is not None and slope < -1e-5:
                    eta = now + (outlet - threshold) / -slope
                restart = {"threshold": _r(threshold), "eta": _iso(eta), "slope_k_per_h": _r(slope * 3600, 2) if slope else None}
        return {
            "threshold": None if target is None else _r(target + THERMO_OFF_K),
            "outlet": outlet,
            "target": target,
            "running": self._running,
            "timer": timer,
            "paused_since": _iso(self._pause["t"]) if self._running is False and self._pause else None,
            "restart_rule": rule,
            "restart": restart,
            "stops": self.stops[-5:],
            "restarts": self.restarts[-5:],
        }
