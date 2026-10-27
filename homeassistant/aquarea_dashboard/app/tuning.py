"""Automatic hysteresis tuning through the Zone 1 heating curve shift.

Panasonic stops the compressor when the water outlet stays > target + 3 K for
3 min (thermo off). Lengthening runs: while the compressor runs and the room
needs heat ("up" mode), the shift is raised by 1 whenever the water gets within
1 K of that threshold, up to normal + up_max. When the compressor stops the
shift goes back to normal, or to normal − down_max in "down" mode (night hours
with a shutoff planned or running, or room warmer than the indoor target), which
lowers the target for the next run. In the night hours without a shutoff (night_off
off, forecast conditions not met, unit on again after the gate / turned on elsewhere,
dynamic start not reached) the indoor temperature decides as by day.
While the compressor is off, a change to "down" mode (e.g. the room gets warmer
than the target after the stop) also lowers the shift to normal − down_max.
When the compressor starts again the shift returns to normal (also later while
it runs, should that command have been skipped or failed). Until a run has
lasted min_run_min the +1 steps are also made outside "up" mode (never above
normal + up_max); after that the shift goes back to normal and the compressor
stops on its own. If the compressor stays off longer than max_pause_min with
the shift below normal and the room not warmer than the indoor target, the shift
goes back to normal so that it restarts on its own (no shift down again until
the compressor has run). Below the outdoor
threshold min_outdoor tuning is off and the shift goes back to normal.
The +1 step follows the unit's thermo off timer (thermo.ThermoWatch, from the
live HeishaMon values): the unit stops 3 min after the outlet first went above
target + 3 K (continuously, same target); the step is sent step_lead_s before
that, which lifts the target and resets the timer. At most one step per timer
run, and only after the unit reports the previous value. A stop of the compressor
while a step was still possible (shift below normal + up_max) is logged.

Safety: commands only with --allow-control, heating on and the zone in curve
mode; a command the unit has not taken over after RESEND_AFTER is sent once
more; at most MAX_COMMANDS_PER_HOUR commands per hour and
MAX_COMMANDS_PER_DAY per 24 h (the unit stores settings in EEPROM, whose write
endurance is limited); a shift changed
elsewhere (app, unit, this dashboard's manual control) pauses tuning. During a
defrost and compressor.DEFROST_SETTLE_MS after it (Observation.hold_until) no
shift or power command is sent; compressor edges are acted on afterwards.

Night-time shutoff (separate switch): during the night hours the whole unit is
turned off if the forecast mean temperature of the following day is above
night_off_min_mean and the forecast night minimum is not below
night_off_min_low; it is turned back on at the end of the night, or earlier if
the night forecast drops below that minimum. Only a unit the dashboard turned
off is turned back on; switching it on elsewhere ends the shutoff for that night.
The unit is only turned off when the compressor has been off for
night_off_early_wait_min (a run is never cut short, it may last into the night).
When it reports on again after the dashboard turned it on, the shift (still
lowered from the night) goes back to normal. Within
night_off_early_min minutes before the night this can already happen, so the
compressor does not start once more just before. With night_off_trend ("prevent
an unnecessary last run") the same can happen up to night_off_trend_min before
the night if the indoor temperature, projected with the learned cooling model
(see cooling.py: heat loss to the outdoor temperature from the forecast, evening
gains until 22:00; a rising rate counts as flat),
is still at or above the indoor target at the start of the night. Only while the
compressor is off, and as always night_off_early_wait_min after its stop.
Stay off after the night (night_off_extend): if the indoor temperature is at or
above the indoor target at the end of a shutoff night, the unit stays off until
it is below the target (then on). Projected with the learned cooling model, a room
that stays warm until the next night stays off the whole day: within the early
shutoff time, or (with night_off_trend) within night_off_trend_min if the
projection to the next night is still at or above the target, the next shutoff
night takes over without turning the unit on in between.
Dynamic start (night_off_dynamic, alternative to night_off_early_min / night_off_trend,
which are then ignored): between night_off_dynamic_from and night_off_dynamic_until
the unit is turned off once the cooling model projects the room (unit off) to be at
or above night_off_gate_temp + night_off_dynamic_margin at the end of the night; as
always only night_off_early_wait_min after a compressor stop and with the forecast
conditions. Not reached by night_off_dynamic_until: no shutoff that night. Without
a cooling model the night starts at night_start. A unit still off after the last
night only takes over the next night if this projection is met.
6am gate (night_off_gate): from night_off_gate_time on, an indoor temperature
below night_off_gate_temp ends the shutoff night or early off (the first gate time
after its start) early: the unit is turned on and stays on for the rest of that
night; an early off is ended.

Early off (manual, e.g. wood stove or sun): from now until a chosen time the
same applies as during a shutoff night, without the forecast conditions and
the night_off switch: "down" mode (runs still reach min_run_min), shutoff once
the compressor has been off for night_off_early_wait_min, on again at the end.
No max. pause reset during early off. An overlapping shutoff night continues
without turning the unit on in between.

Auto early off (early_off_auto): when the indoor temperature rises above
early_off_auto_temp while the unit is on, an early off starts that lasts until
the room is below the indoor target again. It only starts again once the room
has been below the indoor target (also after ending an early off manually).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from compressor import THERMO_MS, THERMO_OFF_K
from cooling import CoolingModel
import quiet

_LOGGER = logging.getLogger("aquarea_tuning")

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "normal_shift": 0,
    "up_max": 3,
    "down_max": 3,
    "min_run_min": 120,  # shift up (within up_max) until a compressor run lasts this long; 0 = off
    "step_lead_s": 60,  # +1 step this many seconds before the unit's thermo off
    "max_pause_min": 180,  # shift below normal back to normal after the compressor is off this long; 0 = off
    "indoor_target": 22.5,
    "min_outdoor": 0,  # below this outdoor °C no tuning; shift back to normal
    "night_start": "22:00",
    "night_end": "10:00",
    "indoor_sensor": "sensor.lumi_lumi_weather_temperatur",
    "night_off": False,
    "night_off_min_mean": 8,  # °C, forecast mean of the following day must be above
    "night_off_min_low": -4,  # °C, forecast night minimum must not be below
    "night_off_early_min": 60,  # min before the night: off early while the compressor is off (0 = never)
    "night_off_early_wait_min": 30,  # min after the last compressor stop before turning off
    "night_off_trend": False,  # prevent an unnecessary last run (learned cooling model)
    "night_off_trend_min": 180,  # min before the night: off if the room stays at/above target until then
    "night_off_extend": True,  # stay off after the night while the room is at/above the indoor target
    "night_off_dynamic": False,  # dynamic night start from the cooling model (instead of early/trend)
    "night_off_dynamic_from": "18:00",  # earliest start
    "night_off_dynamic_until": "03:00",  # latest start; not reached: no shutoff that night
    "night_off_dynamic_margin": 0.0,  # K above night_off_gate_temp at the end of the night
    "night_off_gate": True,  # 6am gate: end the shutoff night from the gate time if the room is colder than
    "night_off_gate_temp": 21.0,  # °C
    "night_off_gate_time": "06:00",
    "early_off_auto": True,  # early off while the room is warmer than early_off_auto_temp
    "early_off_auto_temp": 23.0,  # °C; lasts until the room is below indoor_target
}
LOG_LIMIT = 200
PUBLIC_LOG = 20
MIN_STEP_LEAD_S, MAX_STEP_LEAD_S = 20, 150
CONFIRM_TIMEOUT = dt.timedelta(minutes=20)
RESEND_AFTER = dt.timedelta(seconds=20)  # unconfirmed command: send once more
MAX_COMMANDS_PER_HOUR = 6
MAX_COMMANDS_PER_DAY = 48
POWER_RETRY = dt.timedelta(minutes=10)  # between power commands
MAX_OFF_ATTEMPTS = 3
MIN_EVAL_INTERVAL = dt.timedelta(seconds=30)
AUTO_EARLY_OFF_MAX = dt.timedelta(days=7)  # safety end of an auto early off
# history the compressor estimate needs to know the current state
HISTORY_WINDOW = dt.timedelta(hours=6)  # > the longest min_run_min / max_pause_min
NEAR_K = THERMO_OFF_K - 1
THERMO_OFF_AFTER = dt.timedelta(milliseconds=THERMO_MS)
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_SENSOR_RE = re.compile(r"^sensor\.[a-z0-9_]{1,120}$")


def _int(value: Any, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f"{name} must be a whole number between {lo} and {hi}.")
    return value


def _float(value: Any, name: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        raise ValueError(f"{name} must be a number between {lo} and {hi}.")
    return round(float(value), 1)


def validate_settings(body: Any) -> dict[str, Any]:
    """Complete, type-checked settings; raises ValueError with a user message."""
    if not isinstance(body, dict):
        raise ValueError("settings must be an object.")
    unknown = set(body) - set(DEFAULT_SETTINGS)
    missing = set(DEFAULT_SETTINGS) - set(body)
    if unknown or missing:
        raise ValueError(f"settings: unknown {sorted(unknown)}, missing {sorted(missing)}.")
    if not isinstance(body["enabled"], bool):
        raise ValueError("enabled must be true or false.")
    if not isinstance(body["night_off"], bool):
        raise ValueError("night_off must be true or false.")
    if not isinstance(body["night_off_trend"], bool):
        raise ValueError("night_off_trend must be true or false.")
    if not isinstance(body["night_off_extend"], bool):
        raise ValueError("night_off_extend must be true or false.")
    if not isinstance(body["night_off_dynamic"], bool):
        raise ValueError("night_off_dynamic must be true or false.")
    if not isinstance(body["night_off_gate"], bool):
        raise ValueError("night_off_gate must be true or false.")
    if not isinstance(body["early_off_auto"], bool):
        raise ValueError("early_off_auto must be true or false.")
    indoor = body["indoor_target"]
    if isinstance(indoor, bool) or not isinstance(indoor, (int, float)) or not 15 <= indoor <= 28:
        raise ValueError("indoor_target must be a number between 15 and 28.")
    min_outdoor = body["min_outdoor"]
    if isinstance(min_outdoor, bool) or not isinstance(min_outdoor, (int, float)) or not -25 <= min_outdoor <= 20:
        raise ValueError("min_outdoor must be a number between -25 and 20.")
    for key in ("night_start", "night_end", "night_off_gate_time", "night_off_dynamic_from", "night_off_dynamic_until"):
        if not isinstance(body[key], str) or not _TIME_RE.match(body[key]):
            raise ValueError(f"{key} must be a time HH:MM.")
    if body["night_off_dynamic"] and body["night_start"] != body["night_end"]:
        # circular order from the night end: end < earliest <= night start <= latest < end
        def rel(key: str) -> int:
            return (_minutes(body[key]) - _minutes(body["night_end"])) % 1440

        if not 0 < rel("night_off_dynamic_from") <= rel("night_start") <= rel("night_off_dynamic_until"):
            raise ValueError(
                "dynamic start: the earliest start must be at or before the night start and the latest "
                "start at or after it, both outside the night end."
            )
    if not isinstance(body["indoor_sensor"], str) or not _SENSOR_RE.match(body["indoor_sensor"]):
        raise ValueError("indoor_sensor must be a sensor entity ID (sensor.xyz).")
    auto_temp = _float(body["early_off_auto_temp"], "early_off_auto_temp", 15, 30)
    if auto_temp <= round(float(indoor), 1):
        raise ValueError("early_off_auto_temp must be above indoor_target.")
    return {
        "enabled": body["enabled"],
        "normal_shift": _int(body["normal_shift"], "normal_shift", -5, 5),
        "up_max": _int(body["up_max"], "up_max", 0, 5),
        "down_max": _int(body["down_max"], "down_max", 0, 5),
        "min_run_min": _int(body["min_run_min"], "min_run_min", 0, 240),
        "step_lead_s": _int(body["step_lead_s"], "step_lead_s", MIN_STEP_LEAD_S, MAX_STEP_LEAD_S),
        "max_pause_min": _int(body["max_pause_min"], "max_pause_min", 0, 300),
        "indoor_target": round(float(indoor), 1),
        "min_outdoor": round(float(min_outdoor), 1),
        "night_start": body["night_start"],
        "night_end": body["night_end"],
        "indoor_sensor": body["indoor_sensor"],
        "night_off": body["night_off"],
        "night_off_min_mean": _float(body["night_off_min_mean"], "night_off_min_mean", -20, 25),
        "night_off_min_low": _float(body["night_off_min_low"], "night_off_min_low", -30, 15),
        "night_off_early_min": _int(body["night_off_early_min"], "night_off_early_min", 0, 180),
        "night_off_early_wait_min": _int(body["night_off_early_wait_min"], "night_off_early_wait_min", 0, 120),
        "night_off_trend": body["night_off_trend"],
        "night_off_trend_min": _int(body["night_off_trend_min"], "night_off_trend_min", 0, 360),
        "night_off_extend": body["night_off_extend"],
        "night_off_dynamic": body["night_off_dynamic"],
        "night_off_dynamic_from": body["night_off_dynamic_from"],
        "night_off_dynamic_until": body["night_off_dynamic_until"],
        "night_off_dynamic_margin": _float(body["night_off_dynamic_margin"], "night_off_dynamic_margin", 0, 2),
        "night_off_gate": body["night_off_gate"],
        "night_off_gate_temp": _float(body["night_off_gate_temp"], "night_off_gate_temp", 10, 28),
        "night_off_gate_time": body["night_off_gate_time"],
        "early_off_auto": body["early_off_auto"],
        "early_off_auto_temp": auto_temp,
    }


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def dynamic_window(
    settings: dict[str, Any], window: tuple[dt.datetime, dt.datetime]
) -> tuple[dt.datetime, dt.datetime]:
    """(earliest, latest) dynamic start of the night `window`."""
    start = _minutes(settings["night_start"])
    before = (start - _minutes(settings["night_off_dynamic_from"])) % 1440
    after = (_minutes(settings["night_off_dynamic_until"]) - start) % 1440
    earliest = (window[0] - dt.timedelta(minutes=before)).astimezone()
    latest = (window[0] + dt.timedelta(minutes=after)).astimezone()
    return earliest, min(latest, window[1])


def in_night(settings: dict[str, Any], now: dt.datetime) -> bool:
    """Local time within [night_start, night_end); the window may cross midnight."""
    start = int(settings["night_start"][:2]) * 60 + int(settings["night_start"][3:])
    end = int(settings["night_end"][:2]) * 60 + int(settings["night_end"][3:])
    minute = now.hour * 60 + now.minute
    if start == end:
        return False
    return start <= minute < end if start < end else minute >= start or minute < end


def night_window(settings: dict[str, Any], now: dt.datetime) -> tuple[dt.datetime, dt.datetime] | None:
    """The current night if now is within it, else the next one (local, aware)."""
    if settings["night_start"] == settings["night_end"]:
        return None
    local = now.astimezone()

    def at(day: dt.date, hhmm: str) -> dt.datetime:
        return dt.datetime.combine(day, dt.time(int(hhmm[:2]), int(hhmm[3:]))).astimezone()

    today = local.date()
    start, end = at(today, settings["night_start"]), at(today, settings["night_end"])
    if end <= start:  # crosses midnight
        if local < end:
            start = at(today - dt.timedelta(days=1), settings["night_start"])
        else:
            end = at(today + dt.timedelta(days=1), settings["night_end"])
    elif local >= end:
        start = at(today + dt.timedelta(days=1), settings["night_start"])
        end = at(today + dt.timedelta(days=1), settings["night_end"])
    return start, end


def night_off_conditions(settings: dict[str, Any], fc: dict[str, Any] | None) -> tuple[bool, str]:
    """Whether the forecast allows the night-time shutoff, with the reason."""
    mean = (fc or {}).get("mean")
    low = (fc or {}).get("night_min")
    if mean is None or low is None:
        return False, (fc or {}).get("error") or "forecast incomplete"
    min_mean, min_low = settings["night_off_min_mean"], settings["night_off_min_low"]
    if mean <= min_mean:
        return False, f"forecast mean {_n(mean)} °C ≤ {_n(min_mean)} °C"
    if low < min_low:
        return False, f"forecast night minimum {_n(low)} °C < {_n(min_low)} °C"
    return True, f"forecast mean {_n(mean)} °C > {_n(min_mean)} °C, night minimum {_n(low)} °C ≥ {_n(min_low)} °C"


@dataclass
class Observation:
    now: dt.datetime  # aware, local time zone
    control: bool  # server runs with --allow-control
    unit_on: bool  # heating on (operation mode not OFF and a zone on)
    operation_on: bool  # Zone 1 heating on
    curve_mode: bool
    shift: int | None  # current Zone 1 shift reported by the unit
    limits: tuple[int, int] | None  # Zone 1 heat min/max
    water: float | None
    target: float | None  # curve + current shift
    indoor: float | None
    outdoor: float | None
    compressor: str | None  # latest estimate, None if unknown
    heating: bool  # not defrosting and not heating the hot water tank
    status_ok: bool = True  # live status available (unit_on is meaningful)
    defrost: bool = False
    quiet: int | None = None  # current quiet mode level
    # during a defrost and its settle time: no tuning / power actions until then
    hold_until: dt.datetime | None = None
    compressor_since: dt.datetime | None = None  # start of the current compressor state (estimate)
    cooling: CoolingModel | None = None  # learned room cooling model (see cooling.py)
    outdoor_path: Callable[[dt.datetime], float] | None = None  # outdoor temperature over the next hours
    thermo_since: dt.datetime | None = None  # outlet above target + 3 K since (unit's thermo off timer)


@dataclass
class PowerCommand:
    on: bool
    reason: str


@dataclass
class Command:
    value: int
    previous: int
    reason: str
    step: bool = False  # +1 step before thermo off
    restore: bool = False  # back to normal after disabling
    resend: bool = False  # the same value again, the unit has not taken it over
    over_since: str | None = None  # step: start of the thermo off timer it was made for


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def _parse(value: Any) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


_sent_within = quiet.sent_within


def _n(v: float | None) -> str:
    return "–" if v is None else (str(int(v)) if float(v).is_integer() else str(v))


def _run_minutes(obs: Observation) -> int | None:
    """Minutes the compressor has been running, None if it is not (known to be) running."""
    if obs.compressor != "ON" or obs.compressor_since is None:
        return None
    return max(0, int((obs.now - obs.compressor_since).total_seconds() // 60))


def _pause_minutes(obs: Observation) -> int | None:
    """Minutes the compressor has been off, None if it is not (known to be) off."""
    if obs.compressor != "OFF" or obs.compressor_since is None:
        return None
    return max(0, int((obs.now - obs.compressor_since).total_seconds() // 60))


def _early_off(state: dict[str, Any], now: dt.datetime) -> tuple[dt.datetime, dt.datetime] | None:
    """(since, until) of an active early off."""
    eo = state.get("early_off")
    if not isinstance(eo, dict):
        return None
    since, until = _parse(eo.get("since")), _parse(eo.get("until"))
    if since is None or until is None or not now < until:
        return None
    return since, until


def wake_times(settings: dict[str, Any], state: dict[str, Any], now: dt.datetime) -> list[dt.datetime]:
    """Future times the night-time shutoff acts at (night start/end, gate time,
    early off end), so a slow poll does not miss them."""
    window = night_window(settings, now)
    times: list[dt.datetime] = list(window or ())
    if window is not None and settings["night_off_dynamic"]:
        times.extend(dynamic_window(settings, window))
    gt = settings["night_off_gate_time"]
    day = now.astimezone().date()
    for d in (day, day + dt.timedelta(days=1)):
        times.append(dt.datetime.combine(d, dt.time(int(gt[:2]), int(gt[3:]))).astimezone())
    eo = _early_off(state, now)
    if eo:
        times.append(eo[1])
    return sorted(t for t in times if t > now)


def _early_off_text(state: dict[str, Any]) -> str:
    eo = state.get("early_off") or {}
    if eo.get("auto"):
        return f"auto early off (indoor above {_n(eo.get('above'))} °C) until below {_n(eo.get('below'))} °C"
    until = _parse(eo.get("until"))
    return f"early off until {until.astimezone():%a %H:%M}" if until else "early off"


def _projection(obs: Observation, at: dt.datetime, target: float | None = None) -> tuple[float, str, dt.datetime | None] | None:
    """Indoor temperature projected to `at` with the learned cooling model, a text, and when it
    falls below `target` (None if not before `at`)."""
    if obs.indoor is None or obs.cooling is None or obs.outdoor_path is None:
        return None
    end = max(at, obs.now)
    value, below_at = obs.cooling.project(obs.indoor, obs.now, end, obs.outdoor_path, target)
    projected = round(value, 1)
    hours = (end - obs.now).total_seconds() / 3600
    mean = f", mean {(value - obs.indoor) / hours:+.2f} K/h" if hours > 0 else ""
    out_end = round(obs.outdoor_path(end), 1)
    outdoor = _n(obs.outdoor) if out_end == obs.outdoor else f"{_n(obs.outdoor)} → {_n(out_end)}"
    text = (
        f"indoor {_n(obs.indoor)} °C, outdoor {outdoor} °C"
        f"{mean} (cooling model) → {_n(projected)} °C at {at:%H:%M}"
    )
    return projected, text, below_at


def _extend_note(obs: Observation, target: float, next_night: dt.datetime | None) -> str:
    base = f"unit off after the night: indoor {_n(obs.indoor)} °C ≥ target {_n(target)} °C"
    proj = _projection(obs, next_night, target) if next_night is not None else None
    if proj is None:
        return f"{base}; on when below the target (no cooling model)"
    projected, text, below_at = proj
    if projected >= target:
        return f"{base}; {text} ≥ target: likely off until the night"
    about = f" (about {below_at:%H:%M})" if below_at else ""
    return f"{base}; {text}: on when below the target{about}"


class Tuner:
    """Settings, state and log per device, stored in a JSON file (mode 600)."""

    def __init__(self, path: Path):
        self.path = path
        self.devices: dict[str, dict[str, Any]] = self._load()
        self._live: dict[str, dict[str, Any]] = {}  # not persisted: last evaluation
        self._night_live: dict[str, dict[str, Any]] = {}
        self._quiet_live: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ storage
    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as ex:
            _LOGGER.error("Could not read tuning file %s: %s", self.path, ex)
            return {}
        devices = data.get("devices") if isinstance(data, dict) else None
        result = {}
        for device_id, entry in (devices or {}).items():
            if not isinstance(entry, dict):
                continue
            try:
                stored = dict(entry.get("settings") or {})
                stored.pop("night_off_max_mean", None)  # replaced by night_off_min_mean (0.1.21)
                stored.pop("up_wait_pct", None)  # replaced by step_lead_s (0.2.0)
                settings = validate_settings({**DEFAULT_SETTINGS, **stored})
            except ValueError as ex:
                _LOGGER.error("Invalid tuning settings for %s, using defaults: %s", device_id, ex)
                settings = dict(DEFAULT_SETTINGS)
            state = entry.get("state") if isinstance(entry.get("state"), dict) else {}
            log = [e for e in entry.get("log") or [] if isinstance(e, dict)][-LOG_LIMIT:]
            q = entry.get("quiet") if isinstance(entry.get("quiet"), dict) else {}
            try:
                q_settings = quiet.validate_quiet({**quiet.DEFAULT_QUIET, **(q.get("settings") or {})})
            except ValueError as ex:
                _LOGGER.error("Invalid quiet auto settings for %s, using defaults: %s", device_id, ex)
                q_settings = dict(quiet.DEFAULT_QUIET)
            q_state = q.get("state") if isinstance(q.get("state"), dict) else {}
            result[device_id] = {
                "settings": settings,
                "state": state,
                "log": log,
                "quiet": {"settings": q_settings, "state": q_state},
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
            _LOGGER.error("Could not write tuning file %s: %s", self.path, ex)

    def entry(self, device_id: str) -> dict[str, Any]:
        return self.devices.setdefault(
            device_id,
            {
                "settings": dict(DEFAULT_SETTINGS),
                "state": {},
                "log": [],
                "quiet": {"settings": dict(quiet.DEFAULT_QUIET), "state": {}},
            },
        )

    def _log(self, entry: dict[str, Any], now: dt.datetime, event: str, text: str, **extra: Any) -> None:
        entry["log"].append({"time": _iso(now), "event": event, "text": text, **extra})
        del entry["log"][:-LOG_LIMIT]

    def public(self, device_id: str) -> dict[str, Any]:
        entry = self.entry(device_id)
        st = entry["state"]
        return {
            "settings": entry["settings"],
            "state": {
                k: st.get(k)
                for k in ("paused", "commanded", "confirmed", "compressor", "commanded_at", "resent")
            },
            "live": self._live.get(device_id, {}),
            "log": entry["log"][-PUBLIC_LOG:],
            "near_k": NEAR_K,
            "commands": {
                "hour": len(_sent_within(st, dt.datetime.now().astimezone(), dt.timedelta(hours=1))),
                "day": len(_sent_within(st, dt.datetime.now().astimezone(), dt.timedelta(days=1))),
                "max_hour": MAX_COMMANDS_PER_HOUR,
                "max_day": MAX_COMMANDS_PER_DAY,
            },
            "night_off": {
                **self._night_live.get(device_id, {}),
                "active": bool((st.get("night_off") or {}).get("active")),
            },
            "early_off": (
                {
                    "since": _iso(eo[0]),
                    "until": _iso(eo[1]),
                    "auto": bool(st["early_off"].get("auto")),
                    "text": _early_off_text(st),
                }
                if (eo := _early_off(st, dt.datetime.now().astimezone()))
                else None
            ),
        }

    def start_early_off(self, device_id: str, until: dt.datetime, now: dt.datetime) -> None:
        entry = self.entry(device_id)
        entry["state"]["early_off"] = {"since": _iso(now), "until": _iso(until)}
        self._log(entry, now, "early off", f"Early off started, until {until.astimezone():%a %d.%m. %H:%M}")
        self.save()

    def cancel_early_off(self, device_id: str, now: dt.datetime) -> None:
        entry = self.entry(device_id)
        if entry["state"].pop("early_off", None) is not None:
            entry["state"]["early_off_auto_blocked"] = True  # no auto restart until the room has cooled
            self._log(entry, now, "early off", "Early off ended manually")
            self.save()

    def _auto_early_off(self, entry: dict[str, Any], obs: Observation) -> None:
        """Start/end the automatic early off (room warmer than early_off_auto_temp)."""
        s, st, now = entry["settings"], entry["state"], obs.now
        if obs.indoor is not None and obs.indoor < s["indoor_target"]:
            st.pop("early_off_auto_blocked", None)
            cur = st.get("early_off")
            if isinstance(cur, dict) and cur.get("auto"):
                st.pop("early_off")
                self._log(
                    entry, now, "early off",
                    f"Auto early off ended: indoor {_n(obs.indoor)} °C < target {_n(s['indoor_target'])} °C",
                )
        if (
            s["early_off_auto"]
            and obs.control
            and obs.status_ok
            and obs.unit_on
            and obs.indoor is not None
            and obs.indoor > s["early_off_auto_temp"]
            and _early_off(st, now) is None
            and not (st.get("night_off") or {}).get("active")
            and not st.get("early_off_auto_blocked")
        ):
            st["early_off"] = {
                "since": _iso(now),
                "until": _iso(now + AUTO_EARLY_OFF_MAX),
                "auto": True,
                "above": s["early_off_auto_temp"],
                "below": s["indoor_target"],
            }
            st["early_off_auto_blocked"] = True
            self._log(
                entry, now, "early off",
                f"Auto early off started: indoor {_n(obs.indoor)} °C > {_n(s['early_off_auto_temp'])} °C, "
                f"until below {_n(s['indoor_target'])} °C",
            )

    # ------------------------------------------------------------------ settings
    def update_settings(self, device_id: str, settings: dict[str, Any], now: dt.datetime) -> None:
        entry = self.entry(device_id)
        old, st = entry["settings"], entry["state"]
        if settings == old:
            return
        if settings["enabled"] and not old["enabled"]:
            st.update(paused=None, commanded=None, confirmed=False, sent=False)
            st.pop("restore", None)
            self._log(entry, now, "enabled", "Tuning enabled")
        elif not settings["enabled"] and old["enabled"]:
            if st.get("sent"):
                st["restore"] = settings["normal_shift"]
            st["commanded"] = None
            self._log(
                entry, now, "disabled", "Tuning disabled" + ("; shift goes back to normal" if st.get("sent") else "")
            )
        changed = [f"{k} {old[k]} → {settings[k]}" for k in settings if k != "enabled" and settings[k] != old[k]]
        if changed:
            self._log(entry, now, "settings", "; ".join(changed))
        entry["settings"] = settings
        self.save()

    def _pause(self, entry: dict[str, Any], now: dt.datetime, reason: str) -> None:
        entry["settings"]["enabled"] = False
        entry["state"].update(paused=reason, commanded=None, confirmed=False)
        entry["state"].pop("restore", None)
        self._log(entry, now, "paused", f"Tuning paused: {reason}")

    # ------------------------------------------------------------------ control
    def evaluate(self, device_id: str, obs: Observation, force: bool = False) -> Command | None:
        """Decide on a shift command; the caller sends it and calls record().
        force: also within MIN_EVAL_INTERVAL of the last evaluation (thermo timer, resend)."""
        live = self._live.setdefault(device_id, {})
        last = _parse(live.get("checked_at"))
        if not force and last is not None and obs.now - last < MIN_EVAL_INTERVAL:
            return None
        entry = self.entry(device_id)
        before = json.dumps(entry, sort_keys=True)
        live.clear()
        live["checked_at"] = _iso(obs.now)
        live["indoor"] = obs.indoor
        live["outdoor"] = obs.outdoor
        try:
            cmd = self._evaluate(entry, live, obs)
            if obs.hold_until is not None:
                live["note"] = f"{live.get('note') or ''} · defrost: no changes until {obs.hold_until:%H:%M}".lstrip(" ·")
                return None
            return cmd
        finally:
            if json.dumps(entry, sort_keys=True) != before:
                self.save()

    def _evaluate(self, entry: dict[str, Any], live: dict[str, Any], obs: Observation) -> Command | None:
        s, st, now = entry["settings"], entry["state"], obs.now
        prev = st.get("compressor")
        # during a defrost hold the compressor edge waits, so it is acted on afterwards
        if obs.compressor is not None and obs.hold_until is None:
            st["compressor"] = obs.compressor
        ready = obs.curve_mode and obs.shift is not None and obs.limits is not None

        if not obs.unit_on:
            # nothing at all while the unit is off (no commands, restore or pause);
            # an unconfirmed command gets its confirmation time from power-on
            live["mode"] = "disabled" if not s["enabled"] else "idle"
            live["note"] = "unit off"
            if st.get("commanded") is not None and not st.get("confirmed"):
                st["commanded_at"] = _iso(now)
            return None
        early_off = _early_off(st, now)
        if not s["enabled"]:
            st.pop("reset_after_on", None)
            mode, why = self._mode(s, obs, _early_off_text(st) if early_off else None, self._night_down(entry, obs))
            live["mode"] = "disabled"
            live["note"] = f"would be {mode}: {why}" if ready and obs.operation_on else (
                "Zone 1 not in curve mode or status unavailable" if not ready else "heating off"
            )
            if "restore" in st and ready and obs.control:
                lo, hi = obs.limits
                value = max(lo, min(hi, st.pop("restore")))
                if value != obs.shift:
                    return Command(value, obs.shift, "tuning disabled: back to normal shift", restore=True)
            return None
        if not ready:
            live["mode"], live["note"] = "idle", "Zone 1 not in curve mode or status unavailable"
            return None

        # Has the unit taken over our last command? A different value afterwards
        # means someone else changed the shift.
        commanded = st.get("commanded")
        if commanded is None:
            st.update(commanded=obs.shift, confirmed=True)
        elif obs.shift == commanded:
            if not st.get("confirmed"):
                st["confirmed"] = True
                st.pop("resent", None)
                self._log(entry, now, "confirmed", f"Unit reports shift {obs.shift}")
        elif st.get("confirmed") or obs.shift != st.get("previous"):
            self._pause(entry, now, f"shift changed outside tuning ({commanded} → {obs.shift})")
            return None
        else:
            sent = _parse(st.get("commanded_at"))
            if sent is None or now - sent > CONFIRM_TIMEOUT:
                self._pause(entry, now, f"unit still reports shift {obs.shift}, not {commanded}")
                return None
            if obs.control and not st.get("resent") and now - sent >= RESEND_AFTER:
                live["mode"] = "resend"
                live["note"] = f"unit still reports shift {obs.shift} after {int((now - sent).total_seconds())} s"
                cmd = Command(
                    commanded, obs.shift, f"{live['note']}: shift {commanded} sent again", resend=True
                )
                return None if self._limited(entry, live, now, cmd, live["note"]) else cmd

        if not obs.operation_on:
            live["mode"], live["note"] = "idle", "heating off"
            return None
        mode, why = self._mode(s, obs, _early_off_text(st) if early_off else None, self._night_down(entry, obs))
        live["mode"], live["note"] = mode, why
        if not obs.control:
            live["note"] = f"{why} · control disabled (--allow-control)"
            return None

        normal, lo, hi = s["normal_shift"], *obs.limits
        if st.get("reset_after_on") and obs.shift == normal:
            st.pop("reset_after_on")
        run_min = _run_minutes(obs)
        pause_min = _pause_minutes(obs)
        pause_key = _iso(obs.compressor_since) if obs.compressor == "OFF" and obs.compressor_since else None
        # after a max. pause reset no shift down until the compressor has run again (a flag: the start
        # of a long pause slides with the history window, so it cannot identify the pause)
        if obs.compressor == "ON":
            st.pop("pause_reset", None)
        pause_reset = obs.compressor == "OFF" and bool(st.get("pause_reset"))
        # a run shorter than the minimum gets the up steps in any mode but "off"
        short = mode != "off" and run_min is not None and run_min < s["min_run_min"]
        if short and mode != "up":
            live["note"] = f"{why} · run {run_min} of min. {s['min_run_min']} min: shift up allowed"
        # could a +1 step still be made? (checked at the next stop: stopped before max. shift)
        step_possible = st.pop("step_possible", False) if obs.compressor != "ON" else False
        if obs.compressor == "ON":
            st["step_possible"] = (mode == "up" or short) and obs.shift < normal + s["up_max"]
        cmd: Command | None = None
        if mode == "off":
            if obs.shift != normal:
                cmd = Command(normal, obs.shift, f"{why}: back to normal shift")
        elif st.get("reset_after_on"):
            cmd = Command(normal, obs.shift, "unit turned on after the shutoff: back to normal shift")
        elif prev == "ON" and obs.compressor == "OFF":
            if step_possible and obs.shift < normal + s["up_max"]:
                self._log(
                    entry,
                    now,
                    "early stop",
                    f"Compressor stopped at shift {obs.shift} before max. {normal + s['up_max']} "
                    f"(water {_n(obs.water)} °C, target {_n(obs.target)} °C)",
                    shift=obs.shift,
                    water=obs.water,
                    target=obs.target,
                )
            value = normal - s["down_max"] if mode == "down" else normal
            cmd = Command(value, obs.shift, f"compressor stopped ({why})")
        elif prev == "OFF" and obs.compressor == "ON":
            cmd = Command(normal, obs.shift, "compressor started")
        elif obs.compressor == "ON" and obs.shift < normal:  # also retries a skipped/failed start reset
            cmd = Command(normal, obs.shift, "compressor running with the shift below normal")
        elif (
            obs.compressor == "OFF"
            and obs.shift < normal
            and s["max_pause_min"]
            and pause_min is not None
            and pause_min >= s["max_pause_min"]
            and obs.indoor is not None
            and obs.indoor <= s["indoor_target"]
            and early_off is None
        ):
            st["pause_reset"] = pause_key
            cmd = Command(
                normal,
                obs.shift,
                f"compressor off for {pause_min} min ≥ max. pause {s['max_pause_min']} min "
                f"(indoor {_n(obs.indoor)} °C ≤ {_n(s['indoor_target'])} °C)",
            )
        elif (
            obs.compressor == "OFF"
            and mode == "down"
            and obs.shift > normal - s["down_max"]
            and not pause_reset
        ):
            cmd = Command(normal - s["down_max"], obs.shift, f"compressor off, {why}: shift down")
        elif obs.compressor == "ON" and mode != "up" and not short and obs.shift > normal:
            reason = f"no more heat needed ({why})"
            if run_min is not None and s["min_run_min"]:
                reason = f"min. run of {s['min_run_min']} min reached, no more heat needed ({why})"
            cmd = Command(normal, obs.shift, reason)
        elif (
            obs.compressor == "ON"
            and (mode == "up" or short)
            and obs.heating
            and obs.water is not None
            and obs.target is not None
            and obs.water > obs.target + NEAR_K
            and obs.shift < normal + s["up_max"]
        ):
            since = obs.thermo_since
            off_at = since + THERMO_OFF_AFTER if since is not None else None
            step_at = off_at - dt.timedelta(seconds=s["step_lead_s"]) if off_at is not None else None
            threshold = f"target {_n(obs.target)} + {THERMO_OFF_K} K"
            if not st.get("confirmed"):
                live["note"] = f"{why} · near thermo off, waiting for the unit to report shift {commanded}"
            elif since is None:
                live["note"] = (
                    f"{why} · near thermo off: water {_n(obs.water)} °C, thermo off timer starts above {threshold}"
                )
            elif st.get("step_over_since") == _iso(since):
                live["note"] = f"{why} · step sent for this thermo off timer, waiting for the new target"
            elif now < step_at:
                live["note"] = (
                    f"{why} · thermo off timer running since {since:%H:%M:%S} (water above {threshold}): "
                    f"unit stops about {off_at:%H:%M:%S}, step at {step_at:%H:%M:%S}"
                )
            else:
                reason = (
                    f"thermo off timer: water {_n(obs.water)} °C above {threshold} since {since:%H:%M:%S}, "
                    f"unit stops about {off_at:%H:%M:%S}"
                )
                if mode != "up":
                    reason += f" (run {run_min} of min. {s['min_run_min']} min; {why})"
                cmd = Command(
                    min(max(obs.shift + 1, normal), normal + s["up_max"]),
                    obs.shift,
                    reason,
                    step=True,
                    over_since=_iso(since),
                )
        if cmd is None:
            return None
        cmd.value = max(lo, min(hi, cmd.value))
        if cmd.value == obs.shift or (cmd.value == commanded and not st.get("confirmed")):
            return None
        return None if self._limited(entry, live, now, cmd, why) else cmd

    def _limited(self, entry: dict[str, Any], live: dict[str, Any], now: dt.datetime, cmd: Command, why: str) -> bool:
        """True (with a note and log entry) if the hourly or daily command limit is reached."""
        st = entry["state"]
        recent = _sent_within(st, now, dt.timedelta(hours=1))
        daily = _sent_within(st, now, dt.timedelta(days=1))
        limit = None
        if len(recent) >= MAX_COMMANDS_PER_HOUR:
            limit = f"{MAX_COMMANDS_PER_HOUR}/h"
        elif len(daily) >= MAX_COMMANDS_PER_DAY:
            limit = f"{MAX_COMMANDS_PER_DAY}/24 h, next at {_parse(daily[0]) + dt.timedelta(days=1):%H:%M}"
        if limit:
            live["note"] = f"{why} · command limit reached ({limit}), skipped: {cmd.reason}"
            text = f"Limit {limit.split(',')[0]}: {cmd.reason}"
            if not entry["log"] or entry["log"][-1].get("text") != text:
                self._log(entry, now, "skipped", text, to=cmd.value)
            return True
        return False

    def resend_due(self, device_id: str, now: dt.datetime) -> bool:
        """An unconfirmed command waits for its single resend."""
        st = self.entry(device_id)["state"]
        sent = _parse(st.get("commanded_at"))
        return (
            self.entry(device_id)["settings"]["enabled"]
            and st.get("commanded") is not None
            and not st.get("confirmed")
            and not st.get("resent")
            and sent is not None
            and RESEND_AFTER <= now - sent <= CONFIRM_TIMEOUT
        )

    def _night_down(self, entry: dict[str, Any], obs: Observation) -> bool:
        """Night hours with a shutoff planned or running ("down" mode); otherwise the room decides."""
        s = entry["settings"]
        window = night_window(s, obs.now)
        if not s["night_off"] or window is None or not window[0] <= obs.now < window[1]:
            return False
        no = entry["state"].get("night_off") or {}
        if no.get("night") == _iso(window[0]) and (no.get("done") or no.get("gate")):
            return False  # back on: 6am gate, turned on elsewhere, unit did not turn off
        if s["night_off_dynamic"] and not no.get("active") and obs.now >= dynamic_window(s, window)[1]:
            return False  # dynamic start not reached: no shutoff this night
        # the forecast of the last night-off evaluation (one location for all devices)
        fc = next((v.get("forecast") for v in self._night_live.values()), None)
        return bool(no.get("active")) or night_off_conditions(s, fc)[0]

    @staticmethod
    def _mode(
        s: dict[str, Any], obs: Observation, early_off: str | None = None, night_down: bool = False
    ) -> tuple[str, str]:
        if obs.outdoor is not None and obs.outdoor < s["min_outdoor"]:
            return "off", f"outdoor {_n(obs.outdoor)} °C < {_n(s['min_outdoor'])} °C"
        if early_off is not None:
            return "down", early_off
        if night_down:
            return "down", f"night {s['night_start']}–{s['night_end']}"
        if obs.indoor is None:
            return "hold", "indoor temperature unavailable"
        if obs.indoor > s["indoor_target"]:
            return "down", f"indoor {_n(obs.indoor)} °C > {_n(s['indoor_target'])} °C"
        return "up", f"indoor {_n(obs.indoor)} °C ≤ {_n(s['indoor_target'])} °C"

    def record(self, device_id: str, cmd: Command, now: dt.datetime, error: str | None = None) -> None:
        """Result of sending a command from evaluate()."""
        entry = self.entry(device_id)
        st = entry["state"]
        st["commands"] = _sent_within(st, now, dt.timedelta(days=1)) + [_iso(now)]
        if error:
            self._log(entry, now, "error", f"Sending shift {cmd.value} failed: {error}", to=cmd.value)
        else:
            self._log(entry, now, "command", cmd.reason, **{"from": cmd.previous, "to": cmd.value})
            if entry["settings"]["enabled"] and cmd.resend:
                st.update(commanded_at=_iso(now), resent=True)  # keeps "previous" of the first send
            elif entry["settings"]["enabled"]:
                st.update(
                    commanded=cmd.value,
                    previous=cmd.previous,
                    commanded_at=_iso(now),
                    confirmed=False,
                    sent=True,
                    resent=False,
                )
                if cmd.step:
                    st["step_over_since"] = cmd.over_since
            elif not cmd.restore:
                st["restore"] = entry["settings"]["normal_shift"]  # disabled while sending
        self.save()

    # ------------------------------------------------------------------ quiet auto
    def public_quiet(self, device_id: str) -> dict[str, Any]:
        q = self.entry(device_id)["quiet"]
        st = q["state"]
        return {
            "settings": q["settings"],
            "state": {k: st.get(k) for k in ("paused", "commanded", "confirmed", "defrost_lock", "adv_level")},
            "live": self._quiet_live.get(device_id, {}),
            "commands": {
                "hour": len(quiet.sent_within(st, dt.datetime.now().astimezone(), dt.timedelta(hours=1))),
                "day": len(quiet.sent_within(st, dt.datetime.now().astimezone(), dt.timedelta(days=1))),
                "max_hour": quiet.MAX_COMMANDS_PER_HOUR,
                "max_day": quiet.MAX_COMMANDS_PER_DAY,
            },
        }

    def update_quiet(self, device_id: str, settings: dict[str, Any], now: dt.datetime) -> None:
        entry = self.entry(device_id)
        q = entry["quiet"]
        old, st = q["settings"], q["state"]
        if settings == old:
            return
        if settings["enabled"] != old["enabled"]:
            st.update(paused=None, commanded=None, confirmed=False)
            self._log(entry, now, "quiet", "Quiet auto " + ("enabled" if settings["enabled"] else "disabled"))
        changed = [f"quiet {k} {old[k]} → {settings[k]}" for k in settings if k != "enabled" and settings[k] != old[k]]
        if changed:
            self._log(entry, now, "settings", "; ".join(changed))
        q["settings"] = settings
        self._quiet_live.pop(device_id, None)  # the note refers to the old settings
        self.save()

    def pause_quiet(self, device_id: str, now: dt.datetime, reason: str) -> None:
        entry = self.entry(device_id)
        q = entry["quiet"]
        if q["settings"]["enabled"]:
            quiet._pause(q["settings"], q["state"], reason, lambda ev, text: self._log(entry, now, ev, text))
            self._quiet_live.pop(device_id, None)
            self.save()

    def evaluate_quiet(self, device_id: str, obs: Observation) -> quiet.QuietCommand | None:
        entry = self.entry(device_id)
        before = json.dumps(entry, sort_keys=True)
        live = self._quiet_live[device_id] = {"checked_at": _iso(obs.now)}
        try:
            return quiet.decide(entry["quiet"], live, obs, lambda ev, text: self._log(entry, obs.now, ev, text))
        finally:
            if json.dumps(entry, sort_keys=True) != before:
                self.save()

    def record_quiet(self, device_id: str, cmd: quiet.QuietCommand, now: dt.datetime, error: str | None = None) -> None:
        entry = self.entry(device_id)
        quiet.record(entry["quiet"], cmd, now, error, lambda ev, text: self._log(entry, now, ev, text))
        self.save()

    # ------------------------------------------------------------------ night-time shutoff
    def evaluate_night_off(self, device_id: str, obs: Observation, fc: dict[str, Any] | None) -> PowerCommand | None:
        """Decide on turning the unit off/on; the caller sends it and calls record_power()."""
        entry = self.entry(device_id)
        before = json.dumps(entry, sort_keys=True)
        live = self._night_live[device_id] = {"forecast": fc, "checked_at": _iso(obs.now)}
        try:
            cmd = self._evaluate_night_off(entry, live, obs, fc)
            if cmd is not None and obs.hold_until is not None:
                live["note"] = f"defrost: waiting until {obs.hold_until:%H:%M} ({cmd.reason})"
                return None
            return cmd
        finally:
            if json.dumps(entry, sort_keys=True) != before:
                self.save()

    def _evaluate_night_off(
        self, entry: dict[str, Any], live: dict[str, Any], obs: Observation, fc: dict[str, Any] | None
    ) -> PowerCommand | None:
        s, now = entry["settings"], obs.now
        no = entry["state"].setdefault("night_off", {})
        window = night_window(s, now)
        in_night = window is not None and window[0] <= now < window[1]
        # dynamic start: (earliest, latest) start, decided with the cooling model below
        dyn = dynamic_window(s, window) if s["night_off_dynamic"] and window is not None else None
        # shortly before the night (window is then the coming night)
        early = window is not None and not in_night and now >= window[0] - dt.timedelta(
            minutes=s["night_off_early_min"]
        )
        # "prevent unnecessary last run": even earlier if the room stays warm enough until the night
        trend_early = (
            s["night_off_trend"]
            and window is not None
            and not in_night
            and not early
            and now >= window[0] - dt.timedelta(minutes=s["night_off_trend_min"])
        )
        early = early or trend_early
        if dyn is not None:
            early, trend_early = not in_night and now >= dyn[0], False
        threshold = round(s["night_off_gate_temp"] + s["night_off_dynamic_margin"], 1)
        key = _iso(window[0]) if window else None
        ok, why = night_off_conditions(s, fc)
        enabled = s["night_off"]
        regular_key, regular_in_night = key, in_night
        self._auto_early_off(entry, obs)
        eo = _early_off(entry["state"], now)
        if eo is None and "early_off" in entry["state"]:
            entry["state"].pop("early_off")
            self._log(entry, now, "early off", "Early off time over")
        if eo is not None:  # treated like a shutoff night from its start until its end
            window, in_night, early, trend_early, dyn = eo, True, False, False, None
            key, ok, why, enabled = f"early off {_iso(eo[0])}", True, _early_off_text(entry["state"]), True
        if not obs.status_ok:
            live["note"] = "unit status unavailable"
            return None
        last = _parse(no.get("last_power_at"))
        wait = last is not None and now - last < POWER_RETRY

        if no.get("active") and no.get("night") != key and (in_night or early) and enabled and ok:
            proj = _projection(obs, window[0]) if window else None
            dyn_proj = _projection(obs, window[1]) if dyn is not None else None
            if no.get("extended") and trend_early and (proj is None or proj[0] < s["indoor_target"]):
                pass  # still off after the last night; the room may need heat before the next one
            elif (
                no.get("extended")
                and dyn is not None
                and (
                    not in_night  # no cooling model: fixed start
                    if dyn_proj is None
                    else now >= dyn[1] or dyn_proj[0] < threshold
                )
            ):
                pass  # dynamic start: the room must stay warm enough until the end of the next night
            else:
                # overlapping early off / shutoff night: the unit stays off
                self._log(entry, now, "power", f"Unit stays off: {why}")
                no["night"] = key
                no.pop("extended", None)
        # 6am gate: from the first gate time after the start of the shutoff night / early off on,
        # a room below the threshold ends it
        gate = None
        if s["night_off_gate"] and in_night and obs.indoor is not None:
            gt = s["night_off_gate_time"]
            gate_time = dt.time(int(gt[:2]), int(gt[3:]))
            start_day = window[0].astimezone().date()
            gate_at = dt.datetime.combine(start_day, gate_time).astimezone()
            if gate_at <= window[0]:
                gate_at = dt.datetime.combine(start_day + dt.timedelta(days=1), gate_time).astimezone()
            if now >= gate_at and obs.indoor < s["night_off_gate_temp"]:
                gate = f"6am gate ({gt}): indoor {_n(obs.indoor)} °C < {_n(s['night_off_gate_temp'])} °C"
        if no.get("active"):
            extend_note = None
            if not (in_night or early) or no.get("night") != key:
                end_reason = "early off over" if str(no.get("night")).startswith("early off") else "night over"
                if (
                    end_reason == "night over"
                    and s["night_off_extend"]
                    and enabled
                    and not no.get("gate")
                    and not obs.unit_on
                ):
                    target = s["indoor_target"]
                    if obs.indoor is not None and obs.indoor >= target:
                        end_reason = None
                        extend_note = _extend_note(obs, target, window[0] if window else None)
                        if not no.get("extended"):
                            no["extended"] = True
                            self._log(
                                entry, now, "power",
                                f"Unit stays off after the night: indoor {_n(obs.indoor)} °C ≥ target {_n(target)} °C",
                            )
                    elif no.get("extended"):
                        end_reason = (
                            "no indoor reading after the night"
                            if obs.indoor is None
                            else f"indoor {_n(obs.indoor)} °C < target {_n(target)} °C after the night"
                        )
            elif not enabled:
                end_reason = "night-time shutoff disabled"
            elif (
                eo is None
                and fc
                and fc.get("night_min") is not None
                and fc["night_min"] < s["night_off_min_low"]
            ):
                end_reason = f"forecast night minimum {_n(fc['night_min'])} °C < {_n(s['night_off_min_low'])} °C"
            elif gate is not None or no.get("gate"):
                end_reason = no["gate"] = no.get("gate") or gate  # kept even if the room warms meanwhile
                if eo is not None:  # end the early off; within a shutoff night it ends that as well
                    entry["state"].pop("early_off", None)
                    self._log(entry, now, "early off", f"Early off ended: {end_reason}")
                    if regular_in_night:
                        no["night"] = regular_key
            else:
                end_reason = None
            if obs.unit_on:
                if no.get("gave_up") and not no.get("confirmed"):
                    # the unit never reported off; still turn it on in the morning if it does later
                    if end_reason is not None:
                        no.update(active=False, done=True)
                    else:
                        live["note"] = "unit did not turn off: no shutoff for the rest of this night"
                    return None
                if no.get("confirmed") or no.get("on_sent_at"):
                    text = (
                        "Unit reports on"
                        if no.get("on_sent_at")
                        else "Unit turned on elsewhere: no shutoff for the rest of this night"
                    )
                    no.update(active=False, done=True, on_sent_at=None)
                    if text == "Unit reports on":  # the shift is still lowered from the night
                        entry["state"]["reset_after_on"] = True
                    self._log(entry, now, "power", text)
                    live["note"] = text
                    return None
                if end_reason is not None:  # off command never taken over, no longer needed
                    no.update(active=False, done=True)
                    return None
                if wait:
                    live["note"] = "waiting for the unit to report off"
                    return None
                if no.get("attempts", 0) >= MAX_OFF_ATTEMPTS:
                    # stays active: should the unit report off after all, it is turned on at night end
                    no.update(gave_up=True, done=True)
                    live["note"] = "Unit does not turn off: no shutoff for the rest of this night"
                    self._log(entry, now, "power", live["note"])
                    return None
                if not obs.control:
                    return None
                return PowerCommand(False, "unit still reports on: off again")
            if not no.get("confirmed"):
                no["confirmed"] = True
                self._log(entry, now, "power", "Unit reports off")
            if end_reason is None:
                if extend_note is not None:
                    live["note"] = extend_note
                else:
                    live["note"] = f"unit off: {why}" if eo else f"unit off until {window[1]:%H:%M} ({why})"
                return None
            if not obs.control:
                live["note"] = f"{end_reason}, but control is disabled (--allow-control)"
                return None
            if wait:
                live["note"] = f"{end_reason}: waiting for the unit to report on"
                return None
            return PowerCommand(True, end_reason)

        if no.get("night") != key:
            no.clear()
            no["night"] = key
        if not enabled:
            live["note"] = "disabled" + (f" (would turn off: {why})" if ok else "")
            return None
        if not in_night and not early:
            when = f"next night {window[0]:%H:%M}–{window[1]:%H:%M}" if window else "no night hours set"
            if dyn is not None:
                when = f"next night until {window[1]:%H:%M}, dynamic start {dyn[0]:%H:%M}–{dyn[1]:%H:%M}"
            live["note"] = f"{when}: " + (f"unit will be turned off ({why})" if ok else f"stays on ({why})")
            return None
        if no.get("done"):
            live["note"] = "no more shutoff during this early off" if eo else "no more shutoff this night"
            return None
        if gate is not None:
            if eo is not None:
                entry["state"].pop("early_off", None)
                self._log(entry, now, "early off", f"Early off ended: {gate}")
            live["note"] = f"stays on ({gate})"
            return None
        if not ok:
            live["note"] = f"stays on ({why})"
            return None
        if not obs.unit_on:
            live["note"] = "unit already off (not turned off by the dashboard)"
            return None
        prefix = why if eo else "night" if in_night else f"night from {window[0]:%H:%M}"
        trend_text = ""
        if trend_early:
            plain_at = window[0] - dt.timedelta(minutes=s["night_off_early_min"])
            later = f"shutoff from {plain_at:%H:%M}" if s["night_off_early_min"] else "shutoff at the start of the night"
            if obs.compressor != "OFF":
                state = "running" if obs.compressor == "ON" else "state unknown"
                live["note"] = f"{prefix}: compressor {state}, no indoor projection; {later}"
                return None
            if obs.indoor is None or obs.cooling is None or obs.outdoor_path is None:
                missing = (
                    "no indoor reading"
                    if obs.indoor is None
                    else "no outdoor reading"
                    if obs.cooling is not None
                    else "no cooling model yet"
                )
                live["note"] = f"{prefix}: {missing}, {later}"
                return None
            projected, trend_text, _ = _projection(obs, window[0])
            if projected < s["indoor_target"]:
                live["note"] = f"{prefix}: {trend_text} < target {_n(s['indoor_target'])} °C, {later}"
                return None
            trend_text += f" ≥ target {_n(s['indoor_target'])} °C"
            prefix = f"{prefix} ({trend_text})"
        dyn_text = ""
        if dyn is not None:
            prefix = f"dynamic start {dyn[0]:%H:%M}–{dyn[1]:%H:%M}"
            proj = _projection(obs, window[1])
            if proj is None:
                missing = (
                    "no indoor reading"
                    if obs.indoor is None
                    else "no cooling model yet"
                    if obs.cooling is None
                    else "no outdoor reading"
                )
                if not in_night:
                    live["note"] = f"{prefix}: {missing}, shutoff from {window[0]:%H:%M}"
                    return None
                prefix = f"night ({missing}, fixed start)"
            else:
                projected, text, _ = proj
                reached = projected >= threshold
                dyn_text = f"{text} {'≥' if reached else '<'} {_n(threshold)} °C"
                if now >= dyn[1]:
                    live["note"] = f"no shutoff this night: latest start {dyn[1]:%H:%M} passed ({dyn_text})"
                    return None
                if not reached:
                    live["note"] = f"{prefix}: {dyn_text}; stays on"
                    return None
                prefix = f"{prefix} ({dyn_text})"
        wait_min = s["night_off_early_wait_min"]
        # never cut a run short: off only once the compressor has been off for the wait time
        if obs.compressor == "ON":
            run = _run_minutes(obs)
            live["note"] = (
                f"{prefix}: compressor running{f' for {run} min' if run is not None else ''}; "
                f"shutoff {wait_min} min after it stops"
            )
            return None
        if obs.compressor is None and (eo or dyn_text):
            live["note"] = f"{prefix}: compressor state unknown, waiting"
            return None
        if obs.compressor is None and early:
            live["note"] = f"{prefix}: compressor state unknown, shutoff at the start of the night"
            return None
        stop = obs.compressor_since if obs.compressor == "OFF" else None
        settle_until = stop + dt.timedelta(minutes=wait_min) if stop else None
        if settle_until is not None and now < settle_until:
            live["note"] = f"{prefix}: compressor stopped at {stop:%H:%M}, shutoff at {settle_until:%H:%M}"
            return None
        reason = f"night-time shutoff: {why}"
        if eo:
            reason = f"{why}, compressor off"
        elif dyn_text:
            reason = (
                f"dynamic night-time shutoff (night until {window[1]:%H:%M}), {dyn_text}, "
                f"compressor off: {why}"
            )
        elif trend_early:
            reason = (
                f"early night-time shutoff (no unnecessary last run), night from {window[0]:%H:%M}, "
                f"{trend_text}, compressor off: {why}"
            )
        elif early:
            reason = f"early night-time shutoff, night from {window[0]:%H:%M} and compressor off: {why}"
        if not obs.control:
            live["note"] = f"would turn off ({reason}), but control is disabled (--allow-control)"
            return None
        if wait:
            live["note"] = "waiting before the next power command"
            return None
        return PowerCommand(False, reason)

    def record_power(self, device_id: str, cmd: PowerCommand, now: dt.datetime, error: str | None = None) -> None:
        entry = self.entry(device_id)
        no = entry["state"].setdefault("night_off", {})
        no["last_power_at"] = _iso(now)
        what = "on" if cmd.on else "off"
        if not cmd.on:
            no["attempts"] = no.get("attempts", 0) + 1
        if error:
            self._log(entry, now, "error", f"Turning the unit {what} failed: {error}")
            if not cmd.on and not no.get("active") and no["attempts"] >= MAX_OFF_ATTEMPTS:
                no["done"] = True
                self._log(entry, now, "power", "Unit does not turn off: no shutoff for the rest of this night")
        else:
            self._log(entry, now, "power", f"Unit turned {what}: {cmd.reason}")
            if cmd.on:
                no["on_sent_at"] = _iso(now)
            else:
                no.update(active=True, confirmed=False, on_sent_at=None)
        self.save()
