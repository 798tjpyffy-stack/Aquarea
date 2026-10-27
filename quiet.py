"""Automatic quiet mode level from the outdoor temperature.

Basic: outdoor below the threshold -> quiet 0, above -> quiet 3 (at the
threshold the level is kept, as the unit reports whole degrees).
Advanced (above the threshold): quiet 3 after each compressor stop, back to 0
once the compressor runs and the water reaches target - release_k.
Defrost: after a defrost quiet stays 0 until the outdoor temperature reaches
defrost_until.
A level changed elsewhere (Panasonic app, unit, manual control) pauses the
automatic mode.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable

DEFAULT_QUIET: dict[str, Any] = {
    "enabled": False,
    "threshold": 2,
    "advanced": False,
    "release_k": 2,
    "defrost": True,
    "defrost_until": 5,
}
QUIET_LEVELS = (0, 1, 2, 3)
CONFIRM_TIMEOUT = dt.timedelta(minutes=20)
MAX_COMMANDS_PER_HOUR = 10
MAX_COMMANDS_PER_DAY = 24  # the unit likely stores settings in EEPROM


@dataclass
class QuietCommand:
    level: int
    previous: int
    reason: str


def _float(value: Any, name: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        raise ValueError(f"{name} must be a number between {lo} and {hi}.")
    return round(float(value), 1)


def validate_quiet(body: Any) -> dict[str, Any]:
    """Complete, type-checked quiet auto settings; raises ValueError with a user message."""
    if not isinstance(body, dict):
        raise ValueError("settings must be an object.")
    unknown, missing = set(body) - set(DEFAULT_QUIET), set(DEFAULT_QUIET) - set(body)
    if unknown or missing:
        raise ValueError(f"settings: unknown {sorted(unknown)}, missing {sorted(missing)}.")
    for key in ("enabled", "advanced", "defrost"):
        if not isinstance(body[key], bool):
            raise ValueError(f"{key} must be true or false.")
    return {
        "enabled": body["enabled"],
        "threshold": _float(body["threshold"], "threshold", -20, 20),
        "advanced": body["advanced"],
        "release_k": _float(body["release_k"], "release_k", 0, 10),
        "defrost": body["defrost"],
        "defrost_until": _float(body["defrost_until"], "defrost_until", -20, 20),
    }


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def _parse(value: Any) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def sent_within(st: dict[str, Any], now: dt.datetime, span: dt.timedelta) -> list[str]:
    """Send times in st["commands"] within span before now (oldest first)."""
    return [t for t in st.get("commands") or [] if (p := _parse(t)) and now - p < span]


def _n(v: float | None) -> str:
    return "–" if v is None else (str(int(v)) if float(v).is_integer() else str(v))


Log = Callable[[str, str], None]  # (event, text)


def _pause(s: dict[str, Any], st: dict[str, Any], reason: str, log: Log) -> None:
    s["enabled"] = False
    st.update(paused=reason, commanded=None, confirmed=False)
    log("quiet", f"Quiet auto paused: {reason}")


def decide(q: dict[str, Any], live: dict[str, Any], obs: Any, log: Log) -> QuietCommand | None:
    """Next quiet level command for the observation (tuning.Observation), or None."""
    s, st, now = q["settings"], q["state"], obs.now
    prev_comp = st.get("compressor")
    if obs.compressor is not None:
        st["compressor"] = obs.compressor
    if not s["enabled"]:
        live["note"] = "off"
        return None
    if not obs.status_ok or obs.quiet is None:
        live["note"] = "unit status unavailable"
        return None
    if not obs.unit_on:
        live["note"] = "unit off"
        if st.get("commanded") is not None and not st.get("confirmed"):
            st["commanded_at"] = _iso(now)
        return None

    # Has the unit taken over the last command? Anything else means a manual change.
    commanded = st.get("commanded")
    if commanded is None:
        st.update(commanded=obs.quiet, confirmed=True)
    elif obs.quiet == commanded:
        if not st.get("confirmed"):
            st["confirmed"] = True
            log("quiet", f"Unit reports quiet mode {obs.quiet}")
    elif st.get("confirmed") or obs.quiet != st.get("previous"):
        _pause(s, st, f"quiet mode changed elsewhere ({commanded} → {obs.quiet})", log)
        live["note"] = "paused"
        return None
    else:
        sent = _parse(st.get("commanded_at"))
        if sent is None or now - sent > CONFIRM_TIMEOUT:
            _pause(s, st, f"unit still reports quiet mode {obs.quiet}, not {commanded}", log)
            live["note"] = "paused"
            return None

    # Defrost lock
    if st.get("defrost_lock") and (
        not s["defrost"] or (not obs.defrost and obs.outdoor is not None and obs.outdoor >= s["defrost_until"])
    ):
        st["defrost_lock"] = False
        log("quiet", "Defrost lock released" + (f": outdoor {_n(obs.outdoor)} °C" if s["defrost"] else ""))
    if s["defrost"] and obs.defrost and not st.get("defrost_lock"):
        st["defrost_lock"] = True
        log("quiet", f"Defrost detected: quiet mode 0 until outdoor ≥ {_n(s['defrost_until'])} °C")

    # Advanced: 3 after each compressor stop, 0 when the water is close to the target
    adv = st.get("adv_level", 0)
    if prev_comp == "ON" and obs.compressor == "OFF":
        adv, adv_why = 3, "compressor stopped"
    elif (
        adv == 3
        and obs.compressor == "ON"
        and obs.water is not None
        and obs.target is not None
        and obs.water >= obs.target - s["release_k"]
    ):
        adv, adv_why = 0, f"water {_n(obs.water)} °C ≥ target {_n(obs.target)} − {_n(s['release_k'])} K"
    else:
        adv_why = "after compressor stop until the water nears the target" if adv == 3 else "compressor running"
    st["adv_level"] = adv

    out, thr = obs.outdoor, s["threshold"]
    if st.get("defrost_lock"):
        want, why = 0, f"after defrost, until outdoor ≥ {_n(s['defrost_until'])} °C"
    elif out is None:
        want, why = None, "outdoor temperature unavailable"
    elif out < thr:
        want, why = 0, f"outdoor {_n(out)} °C < {_n(thr)} °C"
    elif out > thr:
        if s["advanced"]:
            want, why = adv, f"outdoor {_n(out)} °C > {_n(thr)} °C, {adv_why}"
        else:
            want, why = 3, f"outdoor {_n(out)} °C > {_n(thr)} °C"
    else:
        want, why = None, f"outdoor at threshold {_n(thr)} °C: level kept"
    live["note"] = why if want is None else f"level {want}: {why}"
    live["want"] = want
    if want is None or want == obs.quiet:
        return None
    if not st.get("confirmed"):
        live["note"] += f" · waiting for the unit to report {commanded}"
        return None
    if not obs.control:
        live["note"] += " · control disabled (--allow-control)"
        return None
    recent = sent_within(st, now, dt.timedelta(hours=1))
    daily = sent_within(st, now, dt.timedelta(days=1))
    if len(recent) >= MAX_COMMANDS_PER_HOUR:
        live["note"] += f" · command limit reached ({MAX_COMMANDS_PER_HOUR}/h)"
        return None
    if len(daily) >= MAX_COMMANDS_PER_DAY:
        nxt = _parse(daily[0]) + dt.timedelta(days=1)
        live["note"] += f" · command limit reached ({MAX_COMMANDS_PER_DAY}/24 h, next at {nxt:%H:%M})"
        return None
    return QuietCommand(want, obs.quiet, why)


def record(q: dict[str, Any], cmd: QuietCommand, now: dt.datetime, error: str | None, log: Log) -> None:
    st = q["state"]
    st["commands"] = sent_within(st, now, dt.timedelta(days=1)) + [_iso(now)]
    if error:
        log("error", f"Setting quiet mode {cmd.level} failed: {error}")
        return
    log("quiet", f"Quiet mode {cmd.previous} → {cmd.level}: {cmd.reason}")
    st.update(commanded=cmd.level, previous=cmd.previous, commanded_at=_iso(now), confirmed=False)
