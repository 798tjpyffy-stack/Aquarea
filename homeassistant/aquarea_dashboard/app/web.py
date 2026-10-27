"""Local web UI for the Aquarea heat pump, live data and commands through HeishaMon (MQTT).

Run:  python web.py --mqtt-host <broker>   -> http://127.0.0.1:8080
MQTT user and password are read from $MQTT_USER / $MQTT_PASSWORD (or a .env file).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import hashlib
import ipaddress
import logging
import os
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

import compressor
import cooling
import forecast
import heishamon
import quiet
import tuning
from energy import EnergyMeter
from forecast import Forecast, parse_location
from ha_sensors import HAConfig, fetch_sensors
from heishamon import CommandError, HeishaMon, MqttConfig
from history import append_rows, count_rising_edges, extract_rows, read_rows
from thermo import THERMO_S, ThermoWatch

_LOGGER = logging.getLogger("aquarea_web")

STATIC_DIR = Path(__file__).parent / "static"
ALLOW_CONTROL = web.AppKey("allow_control", bool)
POLL_MINUTES = web.AppKey("poll_minutes", int)
HISTORY_FILE = web.AppKey("history_file", Path)
ALLOWED_IPS = web.AppKey("allowed_ips", frozenset)
HA_CONFIG = web.AppKey("ha_config", object)  # HAConfig | None
HA_HTTP = web.AppKey("ha_http", aiohttp.ClientSession)
TUNER = web.AppKey("tuner", tuning.Tuner)
COOLING = web.AppKey("cooling", cooling.CoolingStore)
FORECAST = web.AppKey("forecast", Forecast)
FORECAST_HTTP = web.AppKey("forecast_http", aiohttp.ClientSession)
HEISHAMON = web.AppKey("heishamon", HeishaMon)
ENERGY = web.AppKey("energy", EnergyMeter)
THERMO = web.AppKey("thermo", ThermoWatch)
DEVICE = web.AppKey("device", tuple)  # (device_id, name)
STATE = web.AppKey("state", dict)  # last_report, tasks
CONTROL_LOCK = web.AppKey("control_lock", asyncio.Lock)

FIRST_POLL_S = 10  # after start: time for HeishaMon to publish its values
TICK_S = 10  # energy integration / save
FAST_S = 2  # thermo off timer and resend checks
FAST_RETRY_S = 10  # repeat a due step check at most this often
RESEND_CHECK_S = 15
DEFAULT_DEVICE_ID = "heishamon"


def history_gap(poll_minutes: int) -> dt.timedelta:
    """Longest distance between history rows that still counts as continuous."""
    return dt.timedelta(minutes=max(3 * poll_minutes, 10))


def _require_json(request: web.Request) -> None:
    # Blocks cross-site form posts (CSRF); browsers can't send JSON cross-origin
    # without a CORS preflight, which this server never approves.
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="Expected application/json")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _strict_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _whole(value: Any) -> int | None:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return _strict_int(value)


def _running(app: web.Application) -> bool:
    freq = app[HEISHAMON].num(heishamon.FREQ)
    return freq is not None and freq > 0


def _step_lead(app: web.Application) -> int:
    return app[TUNER].entry(app[DEVICE][0])["settings"]["step_lead_s"]


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def build_report(app: web.Application) -> dict[str, Any]:
    """The current HeishaMon values in the dashboard's report shape (no network request)."""
    now = time.time()
    dev = app[HEISHAMON].device_report(*app[DEVICE])
    dev["energy"] = app[ENERGY].public(now, _running(app))
    dev["thermo"] = app[THERMO].public(now, _step_lead(app))
    return {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "devices": [dev],
    }


async def refresh_report(app: web.Application) -> dict[str, Any]:
    """Build a report, append it to the CSV history (while HeishaMon is online) and run
    the automatic control."""
    report = build_report(app)
    if app[HA_CONFIG] is not None:
        report["ha_sensors"] = await fetch_sensors(app[HA_HTTP], app[HA_CONFIG])
    await app[FORECAST].refresh(app[FORECAST_HTTP], app[HA_CONFIG])
    dev = report["devices"][0]
    device_id = dev["info"]["device_id"]
    try:
        if dev["heishamon"]["online"]:
            append_rows(app[HISTORY_FILE], extract_rows(report))
        now = dt.datetime.now().astimezone()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # at least 12 h back, so that "off since" also covers the night
        read_from = min(midnight, now - dt.timedelta(hours=12)) - compressor.CONTEXT
        rows = read_rows(app[HISTORY_FILE], read_from, device_id)
        today = [r for r in rows if dt.datetime.fromisoformat(r["timestamp"]) >= midnight]
        dev["defrost_cycles_today"] = count_rising_edges(today, "defrost")
        estimated = compressor.estimate(rows)
        dev["compressor_today"] = compressor.daily_stats(estimated, midnight, history_gap(app[POLL_MINUTES]))
    except OSError as ex:
        _LOGGER.error("Could not read/write history: %s", ex)
    store = app[COOLING]
    local_now = dt.datetime.now().astimezone()
    if store.due(device_id, local_now):
        sensor = app[TUNER].entry(device_id)["settings"]["indoor_sensor"]
        try:
            await asyncio.to_thread(store.update, device_id, app[HISTORY_FILE], sensor, local_now)
        except Exception:  # noqa: BLE001 - learning must never break the report
            _LOGGER.exception("Cooling update failed for %s", device_id)
    try:
        async with app[CONTROL_LOCK]:
            await run_tuning(app, report, dev)
    except Exception:  # noqa: BLE001 - tuning must never break the report
        _LOGGER.exception("Tuning failed for %s", device_id)
    dev["tuning"] = _tuning_public(app, device_id)
    dev["quiet_auto"] = app[TUNER].public_quiet(device_id)
    app[STATE]["last_report"] = report
    return report


def _tuning_public(app: web.Application, device_id: str) -> dict[str, Any]:
    return {**app[TUNER].public(device_id), "cooling": app[COOLING].public(device_id)}


def _zones_on(status: dict[str, Any]) -> bool:
    return any((z.get("operation_status") or {}).get("name") == "ON" for z in status.get("zones") or [])


def _observe(app: web.Application, report: dict[str, Any], dev: dict[str, Any]) -> tuning.Observation:
    """What the tuner needs from a report and the history."""
    device_id = dev["info"]["device_id"]
    settings = app[TUNER].entry(device_id)["settings"]
    online = bool((dev.get("heishamon") or {}).get("online"))
    status = dev.get("status") if online and isinstance(dev.get("status"), dict) and "error" not in dev["status"] else {}
    zone1 = next((z for z in status.get("zones") or [] if z.get("zone_id") == 1), {})
    lo, hi = _whole(zone1.get("heat_min")), _whole(zone1.get("heat_max"))
    wt = (dev.get("water_target") or {}) if status else {}
    now = dt.datetime.now().astimezone()
    rows = compressor.estimate(read_rows(app[HISTORY_FILE], now - tuning.HISTORY_WINDOW, device_id))
    sensor = next(
        (
            s
            for s in (report.get("ha_sensors") or {}).get("sensors") or []
            if isinstance(s, dict) and s.get("entity_id") == settings["indoor_sensor"]
        ),
        {},
    )
    indoor = sensor.get("value")
    outdoor = status.get("temperature_outdoor") if _is_number(status.get("temperature_outdoor")) else None
    fc = app[FORECAST]
    fresh = fc.fetched_at is not None and now - fc.fetched_at <= forecast.MAX_AGE
    outdoor_path = cooling.outdoor_path(now, outdoor, fc.hourly if fresh else []) if outdoor is not None else None
    extra = dev.get("status_extra") or {}
    direction = (status.get("direction") or {}).get("name") if isinstance(status.get("direction"), dict) else None
    # compressor state from the measured frequency; since: the live edge, else the history
    freq = dev["heishamon"].get("compressor_freq") if status else None
    comp_now = None if not _is_number(freq) else "ON" if freq > 0 else "OFF"
    comp_since = None
    if comp_now is not None:
        watch = app[THERMO]
        if watch.running_since is not None and watch.running == (comp_now == "ON"):
            comp_since = dt.datetime.fromtimestamp(watch.running_since).astimezone()
        else:
            state = compressor.state_of(rows)
            comp_since = (
                dt.datetime.fromisoformat(state["since"]) if state.get("state") == comp_now and state.get("since") else now
            )
    over_since = app[THERMO].over_since
    defrost = extra.get("defrost") == 1
    water = zone1.get("temperature")
    return tuning.Observation(
        now=now,
        control=app[ALLOW_CONTROL],
        unit_on=bool(status) and _zones_on(status),
        operation_on=(zone1.get("operation_status") or {}).get("name") == "ON",
        curve_mode=wt.get("curve") is not None,
        shift=_whole(wt.get("shift")) if wt.get("curve") is not None else None,
        limits=(lo, hi) if lo is not None and hi is not None else None,
        water=water if _is_number(water) else None,
        target=wt.get("target") if _is_number(wt.get("target")) else None,
        indoor=indoor if _is_number(indoor) else None,
        outdoor=outdoor,
        compressor=comp_now,
        compressor_since=comp_since,
        heating=direction != "WATER" and not defrost,
        status_ok=bool(status),
        defrost=defrost,
        quiet=_strict_int((status.get("quiet_mode") or {}).get("value")) if isinstance(status.get("quiet_mode"), dict) else None,
        hold_until=compressor.defrost_hold_until(rows, now, defrost),
        cooling=app[COOLING].model(device_id),
        outdoor_path=outdoor_path,
        thermo_since=dt.datetime.fromtimestamp(over_since).astimezone() if over_since is not None else None,
    )


# --------------------------------------------------------------------------- #
# Automatic control (call with CONTROL_LOCK held)
# --------------------------------------------------------------------------- #
async def _send(app: web.Application, command: str, value: int, why: str) -> str | None:
    """Publish a command; the error text if it failed."""
    _LOGGER.warning("CONTROL: %s -> %s (%s)", command, value, why)
    try:
        await app[HEISHAMON].send(command, value)
    except CommandError as ex:
        _LOGGER.warning("Command %s %s failed: %s", command, value, ex)
        return str(ex) or type(ex).__name__
    return None


async def run_night_off(app: web.Application, device_id: str, obs: tuning.Observation) -> bool:
    """Night-time shutoff; True if a power command was sent."""
    tuner = app[TUNER]
    window = tuning.night_window(tuner.entry(device_id)["settings"], obs.now)
    cmd = tuner.evaluate_night_off(device_id, obs, app[FORECAST].summary(window))
    if cmd is None:
        return False
    error = await _send(app, "SetHeatpump", 1 if cmd.on else 0, f"night-time shutoff: {cmd.reason}")
    tuner.record_power(device_id, cmd, obs.now, error)
    return True


async def run_quiet(app: web.Application, device_id: str, obs: tuning.Observation) -> None:
    tuner = app[TUNER]
    cmd = tuner.evaluate_quiet(device_id, obs)
    if cmd is None:
        return
    error = await _send(app, "SetQuietMode", cmd.level, f"quiet auto: {cmd.reason}")
    tuner.record_quiet(device_id, cmd, obs.now, error)


async def run_shift(app: web.Application, device_id: str, obs: tuning.Observation, force: bool = False) -> None:
    tuner = app[TUNER]
    cmd = tuner.evaluate(device_id, obs, force=force)
    if cmd is None:
        return
    if obs.limits is None or not obs.limits[0] <= cmd.value <= obs.limits[1]:
        error = f"allowed range {obs.limits} does not include {cmd.value}"
    else:
        error = await _send(app, "SetZ1HeatRequestTemperature", cmd.value, f"tuning from {cmd.previous}: {cmd.reason}")
    tuner.record(device_id, cmd, obs.now, error)


async def run_tuning(app: web.Application, report: dict[str, Any], dev: dict[str, Any]) -> None:
    device_id = dev["info"]["device_id"]
    obs = _observe(app, report, dev)
    if await run_night_off(app, device_id, obs):
        return  # the unit state changes; tune on the next poll
    await run_quiet(app, device_id, obs)
    await run_shift(app, device_id, obs)


async def fast_evaluate(app: web.Application) -> None:
    """Shift tuning only, from the live values (thermo off timer / resend)."""
    last = app[STATE].get("last_report") or {}
    report = build_report(app)
    if "ha_sensors" in last:
        report["ha_sensors"] = last["ha_sensors"]  # indoor temperature of the last poll
    dev = report["devices"][0]
    async with app[CONTROL_LOCK]:
        await run_shift(app, dev["info"]["device_id"], _observe(app, report, dev), force=True)


# --------------------------------------------------------------------------- #
# Background tasks
# --------------------------------------------------------------------------- #
async def poll_loop(app: web.Application) -> None:
    await asyncio.sleep(FIRST_POLL_S)
    while True:
        try:
            await refresh_report(app)
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001 - keep polling on any failure
            _LOGGER.warning("Scheduled update failed: %s", ex)
        await asyncio.sleep(app[POLL_MINUTES] * 60)


async def tick_loop(app: web.Application) -> None:
    """Energy integration also between messages (and stopping when they stay away)."""
    hm, meter = app[HEISHAMON], app[ENERGY]
    while True:
        await asyncio.sleep(TICK_S)
        now = time.time()
        p_in, p_out = hm.power()
        meter.update(now, p_in, p_out, hm.fresh(now))
        meter.maybe_save(now)


async def fast_loop(app: web.Application) -> None:
    """Evaluate the shift at once when a step is due on the thermo off timer, or a
    command waits for its resend (the regular poll may be a minute away)."""
    watch, tuner = app[THERMO], app[TUNER]
    last_try = last_resend = 0.0
    while True:
        await asyncio.sleep(FAST_S)
        try:
            if app[STATE].get("last_report") is None or not app[ALLOW_CONTROL]:
                continue
            device_id = app[DEVICE][0]
            settings = tuner.entry(device_id)["settings"]
            now, mono = time.time(), time.monotonic()
            due = False
            since = watch.over_since
            if settings["enabled"] and since is not None:
                off_at = since + THERMO_S
                if off_at - settings["step_lead_s"] <= now < off_at + 30 and mono - last_try >= FAST_RETRY_S:
                    due, last_try = True, mono
            if not due and mono - last_resend >= RESEND_CHECK_S and tuner.resend_due(
                device_id, dt.datetime.now().astimezone()
            ):
                due, last_resend = True, mono
            if due:
                await fast_evaluate(app)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the loop alive
            _LOGGER.exception("Fast evaluation failed")


def _on_message(app: web.Application):
    hm, meter, watch = app[HEISHAMON], app[ENERGY], app[THERMO]

    def handle(name: str, _value: Any, t: float) -> None:
        if name in heishamon.POWER_TOPICS:
            p_in, p_out = hm.power()
            meter.update(t, p_in, p_out, hm.fresh(t))
        if name in heishamon.THERMO_TOPICS:
            watch.update(t, **hm.thermo_values())

    return handle


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
async def index(_: web.Request) -> web.Response:
    # Version the asset URLs by content, so browsers never run a stale app.js
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "style.css"):
        digest = hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]
        html = html.replace(f'"static/{name}"', f'"static/{name}?v={digest}"')
    return web.Response(text=html, content_type="text/html")


async def api_session(request: web.Request) -> web.Response:
    app = request.app
    hm = app[HEISHAMON]
    return web.json_response(
        {
            "allow_control": app[ALLOW_CONTROL],
            "poll_minutes": app[POLL_MINUTES],
            "history_gap_minutes": history_gap(app[POLL_MINUTES]).total_seconds() / 60,
            "device_id": app[DEVICE][0],
            "mqtt": {"host": hm.config.host, "port": hm.config.port, "base_topic": hm.base},
        }
    )


async def api_report(request: web.Request) -> web.Response:
    try:
        report = await refresh_report(request.app)
    except Exception as ex:  # noqa: BLE001
        _LOGGER.exception("Report failed")
        return web.json_response({"error": f"Report failed: {ex}"}, status=500)
    return web.json_response(report)


async def api_report_latest(request: web.Request) -> web.Response:
    """Last report of the background poller."""
    return web.json_response({"report": request.app[STATE].get("last_report")})


async def api_live(request: web.Request) -> web.Response:
    """Live values for the overview (no history write, no control)."""
    app = request.app
    hm = app[HEISHAMON]
    now = time.time()
    p_in, p_out = hm.power()
    age = hm.age_s(now)
    problem = hm.problem()
    return web.json_response(
        {
            "time": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "online": problem is None,
            "problem": problem,
            "age_s": None if age is None else round(age),
            "compressor_freq": hm.num(heishamon.FREQ),
            "outlet": hm.num(heishamon.OUTLET),
            "inlet": hm.num(heishamon.INLET),
            "target": hm.num(heishamon.TARGET),
            "flow": hm.num(heishamon.FLOW),
            "power_in_w": p_in,
            "power_out_w": p_out,
            "energy": {"live": app[ENERGY].live(now, _running(app)), "today": app[ENERGY].today(now)},
            "thermo": app[THERMO].public(now, _step_lead(app)),
        }
    )


async def api_history(request: web.Request) -> web.Response:
    try:
        hours = int(request.query.get("hours", "24"))
    except ValueError:
        return web.json_response({"error": "hours must be an integer"}, status=400)
    if not 0 <= hours <= 24 * 366 * 5:
        return web.json_response({"error": "hours out of range"}, status=400)
    since = dt.datetime.now().astimezone() - dt.timedelta(hours=hours) if hours else None
    device_id = request.query.get("device_id") or None
    # extra context before the range so the compressor estimate has settled
    rows = read_rows(request.app[HISTORY_FILE], since - compressor.CONTEXT if since else None, device_id)
    by_device: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        by_device.setdefault(row.get("device_id"), []).append(row)
    rows = [r for group in by_device.values() for r in compressor.estimate(group)]
    if since:
        rows = [r for r in rows if dt.datetime.fromisoformat(r["timestamp"]) >= since]
    return web.json_response({"rows": rows})


async def _control_body(request: web.Request) -> tuple[dict[str, Any] | None, web.Response | None]:
    """The JSON body of a control request for this device, or an error response."""
    if not request.app[ALLOW_CONTROL]:
        return None, web.json_response(
            {"error": "Control is disabled. Start the server with --allow-control."}, status=403
        )
    _require_json(request)
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid JSON")
    if not isinstance(body, dict):
        return None, web.json_response({"error": "JSON object required."}, status=400)
    if body.get("device_id") != request.app[DEVICE][0]:
        return None, web.json_response({"error": "Unknown device."}, status=404)
    return body, None


def _live_status(app: web.Application) -> tuple[dict[str, Any] | None, web.Response | None]:
    hm = app[HEISHAMON]
    problem = hm.problem()
    if problem is not None:
        return None, web.json_response({"error": f"HeishaMon: {problem}; refusing to send."}, status=409)
    return hm.device_report(*app[DEVICE]), None


async def _manual_send(app: web.Application, command: str, value: int) -> web.Response | None:
    async with app[CONTROL_LOCK]:
        error = await _send(app, command, value, "dashboard")
    if error is not None:
        return web.json_response({"error": f"Command not sent: {error}"}, status=502)
    return None


async def api_zone_heat_target(request: web.Request) -> web.Response:
    body, error = await _control_body(request)
    if error:
        return error
    zone_id, value = _strict_int(body.get("zone_id")), _strict_int(body.get("value"))
    if zone_id != 1 or value is None:
        return web.json_response({"error": "zone_id 1 and value (int) required."}, status=400)
    dev, error = _live_status(request.app)
    if error:
        return error
    zone = dev["status"]["zones"][0]
    lo, hi = zone.get("heat_min"), zone.get("heat_max")
    if lo is None or hi is None:
        return web.json_response(
            {"error": "Zone 1 is not in heating curve mode: only the curve shift can be set."}, status=409
        )
    if not lo <= value <= hi:
        return web.json_response({"error": f"Value must be between {lo} and {hi}."}, status=400)
    result = {"ok": True, "device_id": body["device_id"], "zone_id": 1, "value": value, "range": [lo, hi]}
    if zone.get("heat_set") == value:
        return web.json_response({**result, "unchanged": True})
    if error := await _manual_send(request.app, "SetZ1HeatRequestTemperature", value):
        return error
    return web.json_response(result)


async def api_device_power(request: web.Request) -> web.Response:
    """Turn the heat pump on or off."""
    body, error = await _control_body(request)
    if error:
        return error
    on = body.get("on")
    if not isinstance(on, bool):
        return web.json_response({"error": "on (bool) required."}, status=400)
    dev, error = _live_status(request.app)
    if error:
        return error
    result = {"ok": True, "device_id": body["device_id"], "on": on}
    if (dev["status"]["operation_status"]["name"] == "ON") == on:
        return web.json_response({**result, "unchanged": True})
    if error := await _manual_send(request.app, "SetHeatpump", 1 if on else 0):
        return error
    return web.json_response(result)


async def api_device_quiet_mode(request: web.Request) -> web.Response:
    """Set the quiet mode level (0 = off, 1–3)."""
    body, error = await _control_body(request)
    if error:
        return error
    level = _strict_int(body.get("level"))
    if level not in (0, 1, 2, 3):
        return web.json_response({"error": "level (0–3) required."}, status=400)
    dev, error = _live_status(request.app)
    if error:
        return error
    result = {"ok": True, "device_id": body["device_id"], "level": level}
    if dev["status"]["quiet_mode"]["value"] == level:
        return web.json_response({**result, "unchanged": True})
    if error := await _manual_send(request.app, "SetQuietMode", level):
        return error
    request.app[TUNER].pause_quiet(body["device_id"], dt.datetime.now().astimezone(), f"set manually to {level}")
    return web.json_response(result)


async def api_quiet_auto(request: web.Request) -> web.Response:
    """Save quiet auto settings."""
    body, error = await _control_body(request)
    if error:
        return error
    try:
        settings = quiet.validate_quiet(body.get("settings"))
    except ValueError as ex:
        return web.json_response({"error": str(ex)}, status=400)
    device_id = body["device_id"]
    _LOGGER.warning("CONTROL: quiet auto device %s settings %s", device_id, settings)
    tuner = request.app[TUNER]
    tuner.update_quiet(device_id, settings, dt.datetime.now().astimezone())
    return web.json_response({"ok": True, "quiet_auto": tuner.public_quiet(device_id)})


async def api_tuning(request: web.Request) -> web.Response:
    """Save hysteresis tuning settings."""
    body, error = await _control_body(request)
    if error:
        return error
    try:
        settings = tuning.validate_settings(body.get("settings"))
    except ValueError as ex:
        return web.json_response({"error": str(ex)}, status=400)
    device_id = body["device_id"]
    _LOGGER.warning("CONTROL: tuning device %s settings %s", device_id, settings)
    request.app[TUNER].update_settings(device_id, settings, dt.datetime.now().astimezone())
    return web.json_response({"ok": True, "tuning": _tuning_public(request.app, device_id)})


async def api_early_off(request: web.Request) -> web.Response:
    """Start (until the end of the night or for some hours) or end an early off."""
    body, error = await _control_body(request)
    if error:
        return error
    device_id = body["device_id"]
    tuner = request.app[TUNER]
    now = dt.datetime.now().astimezone()
    action = body.get("action")
    if action == "cancel":
        _LOGGER.warning("CONTROL: early off device %s ended", device_id)
        tuner.cancel_early_off(device_id, now)
    elif action == "morning":
        window = tuning.night_window(tuner.entry(device_id)["settings"], now)
        if window is None:
            return web.json_response({"error": "No night hours set: choose a duration."}, status=400)
        _LOGGER.warning("CONTROL: early off device %s until %s", device_id, window[1])
        tuner.start_early_off(device_id, window[1], now)
    elif action == "hours":
        hours = body.get("hours")
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0.5 <= hours <= 48:
            return web.json_response({"error": "hours must be a number between 0.5 and 48."}, status=400)
        until = now + dt.timedelta(hours=hours)
        _LOGGER.warning("CONTROL: early off device %s until %s", device_id, until)
        tuner.start_early_off(device_id, until, now)
    else:
        return web.json_response({"error": "action must be morning, hours or cancel."}, status=400)
    return web.json_response({"ok": True, "tuning": _tuning_public(request.app, device_id)})


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
@web.middleware
async def client_allowlist(request: web.Request, handler):
    # Home Assistant ingress: only the Supervisor proxy may connect.
    allowed = request.app[ALLOWED_IPS]
    if allowed and request.remote not in allowed:
        _LOGGER.warning("Rejected request from %s", request.remote)
        raise web.HTTPForbidden()
    return await handler(request)


@web.middleware
async def security_headers(request: web.Request, handler):
    response = await handler(request)
    # 'self' framing is required for the Home Assistant ingress iframe.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    else:  # revalidate page and static files, so an update is picked up at once
        response.headers["Cache-Control"] = "no-cache"
    return response


def cloud_daily_energy(path: Path, device_id: str) -> dict[str, float]:
    """Daily consumption (kWh) recorded from the Panasonic cloud before HeishaMon:
    the highest "today" total per local day."""
    daily: dict[str, float] = {}
    for row in read_rows(path, None, device_id):
        kwh = row.get("energy_today_kwh")
        if row.get("source") == "heishamon" or not _is_number(kwh):
            continue
        day = dt.datetime.fromisoformat(row["timestamp"]).astimezone().date().isoformat()
        daily[day] = max(daily.get(day, 0.0), kwh)
    return daily


async def on_startup(app: web.Application) -> None:
    if app[HA_CONFIG] is not None:
        app[HA_HTTP] = aiohttp.ClientSession()
    app[FORECAST_HTTP] = aiohttp.ClientSession()
    hm = app[HEISHAMON]
    hm.listeners.append(_on_message(app))
    hm.start()
    app[STATE]["tasks"] = [
        asyncio.create_task(poll_loop(app)),
        asyncio.create_task(tick_loop(app)),
        asyncio.create_task(fast_loop(app)),
    ]
    meter = app[ENERGY]
    if not meter.seeded:
        try:
            daily = await asyncio.to_thread(cloud_daily_energy, app[HISTORY_FILE], app[DEVICE][0])
            meter.seed(daily, dt.datetime.now().astimezone().date().isoformat())
            _LOGGER.warning("Energy history: %d days of cloud consumption taken over", len(daily))
        except Exception:  # noqa: BLE001 - the history is optional
            _LOGGER.exception("Could not take over the cloud energy history")


async def on_shutdown(app: web.Application) -> None:
    for task in app[STATE].get("tasks") or []:
        task.cancel()
    await app[HEISHAMON].stop()
    app[ENERGY].save()
    if HA_HTTP in app:
        await app[HA_HTTP].close()
    if FORECAST_HTTP in app:
        await app[FORECAST_HTTP].close()


def last_device(path: Path) -> tuple[str | None, str | None]:
    """Device ID and name of the last history row (keeps the history continuous)."""
    device_id = name = None
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("device_id"):
                    device_id, name = row["device_id"], row.get("device_name") or None
    except OSError:
        pass
    return device_id, name


def create_app(
    mqtt: MqttConfig,
    allow_control: bool = False,
    poll_minutes: int = 1,
    history_file: Path = Path("data/history.csv"),
    allowed_ips: frozenset[str] = frozenset(),
    ha_config: HAConfig | None = None,
    tuning_file: Path = Path("data/tuning.json"),
    location: tuple[float, float] | None = None,
    device_id: str | None = None,
    device_name: str | None = None,
) -> web.Application:
    app = web.Application(middlewares=[client_allowlist, security_headers], client_max_size=16 * 1024)
    app[ALLOW_CONTROL] = allow_control
    app[POLL_MINUTES] = poll_minutes
    app[HISTORY_FILE] = history_file
    app[ALLOWED_IPS] = allowed_ips
    app[HA_CONFIG] = ha_config
    app[TUNER] = tuning.Tuner(tuning_file)
    app[COOLING] = cooling.CoolingStore(tuning_file.with_name("cooling.json"))
    app[FORECAST] = Forecast(location)
    app[HEISHAMON] = HeishaMon(mqtt)
    app[ENERGY] = EnergyMeter(tuning_file.with_name("energy.json"))
    app[THERMO] = ThermoWatch(tuning_file.with_name("thermo.json"))
    hist_id, hist_name = last_device(history_file)
    app[DEVICE] = (device_id or hist_id or DEFAULT_DEVICE_ID, device_name or hist_name or "Heat pump")
    app[STATE] = {}
    app[CONTROL_LOCK] = asyncio.Lock()
    app.router.add_get("/", index)
    app.router.add_get("/api/session", api_session)
    app.router.add_get("/api/report", api_report)
    app.router.add_get("/api/report/latest", api_report_latest)
    app.router.add_get("/api/live", api_live)
    app.router.add_get("/api/history", api_history)
    app.router.add_post("/api/zone/heat-target", api_zone_heat_target)
    app.router.add_post("/api/device/power", api_device_power)
    app.router.add_post("/api/device/quiet-mode", api_device_quiet_mode)
    app.router.add_post("/api/quiet-auto", api_quiet_auto)
    app.router.add_post("/api/tuning", api_tuning)
    app.router.add_post("/api/early-off", api_early_off)
    app.router.add_static("/static/", STATIC_DIR)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Aquarea web UI (HeishaMon over MQTT)")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--allow-control",
        action="store_true",
        help="enable sending commands (shift, power, quiet mode); read-only otherwise",
    )
    parser.add_argument("--poll-minutes", type=int, default=1, help="history/control interval (default: 1)")
    parser.add_argument(
        "--history-file", type=Path, default=Path("data/history.csv"), help="CSV history file"
    )
    parser.add_argument(
        "--mqtt-host", default=os.environ.get("MQTT_HOST") or "localhost", help="MQTT broker (default: $MQTT_HOST)"
    )
    parser.add_argument(
        "--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT") or 1883), help="default: $MQTT_PORT or 1883"
    )
    parser.add_argument(
        "--mqtt-base-topic",
        default=os.environ.get("MQTT_BASE_TOPIC") or "panasonic_heat_pump",
        help="HeishaMon's MQTT base topic (default: $MQTT_BASE_TOPIC or panasonic_heat_pump); "
        "user and password from $MQTT_USER / $MQTT_PASSWORD",
    )
    parser.add_argument("--device-id", help="device ID in the history (default: that of the last history row)")
    parser.add_argument("--device-name", help="device name (default: that of the last history row)")
    parser.add_argument(
        "--allow-ip",
        action="append",
        default=[],
        metavar="IP",
        help="only accept requests from this client IP (repeatable; e.g. 172.30.32.2 for HA ingress)",
    )
    parser.add_argument(
        "--ha-url",
        help="Home Assistant URL, e.g. your Nabu Casa remote URL (default: $HA_URL); "
        "the access token is read from --ha-token-file or $HA_TOKEN",
    )
    parser.add_argument(
        "--ha-token-file",
        type=Path,
        default=Path(os.environ["HA_TOKEN_FILE"]) if os.environ.get("HA_TOKEN_FILE") else None,
        help="file containing the Home Assistant long-lived access token (default: $HA_TOKEN_FILE)",
    )
    parser.add_argument(
        "--ha-sensor",
        action="append",
        default=[],
        metavar="ENTITY_ID",
        help="Home Assistant temperature sensor to show and record (repeatable; "
        "default: $HA_SENSORS, comma-separated, or all temperature sensors)",
    )
    parser.add_argument(
        "--tuning-file",
        type=Path,
        default=Path("data/tuning.json"),
        help="hysteresis tuning settings, state and log (default: data/tuning.json); "
        "energy.json, thermo.json and cooling.json are kept next to it",
    )
    parser.add_argument(
        "--location",
        metavar="LAT,LON",
        help="location for the weather forecast (default: from Home Assistant); "
        "rounded to 0.01° and sent to Open-Meteo",
    )
    args = parser.parse_args()
    if args.poll_minutes < 1:
        parser.error("--poll-minutes must be >= 1")
    if not 0 < args.mqtt_port < 65536:
        parser.error("--mqtt-port out of range")
    base_topic = args.mqtt_base_topic.strip().strip("/")
    if not heishamon.BASE_TOPIC_RE.match(base_topic):
        parser.error("--mqtt-base-topic: letters, digits, _ and - separated by / (no wildcards)")
    if args.device_id is not None and not 0 < len(args.device_id) <= 64:
        parser.error("--device-id: 1 to 64 characters")
    try:
        location = parse_location(args.location) if args.location else None
    except ValueError as ex:
        parser.error(f"--location: {ex}")
    for ip in args.allow_ip:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            parser.error(f"--allow-ip: invalid IP address {ip!r}")
    ha_sensors = args.ha_sensor or [s.strip() for s in os.environ.get("HA_SENSORS", "").split(",") if s.strip()]
    try:
        ha_config = HAConfig.from_env(args.ha_url, ha_sensors, args.ha_token_file)
    except ValueError as ex:
        parser.error(str(ex))

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        loopback = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = args.host == "localhost"
    if not loopback and not args.allow_ip:
        _LOGGER.warning(
            "Binding to %s over plain HTTP with no authentication: anyone who can reach it can %s. "
            "Use an authenticating TLS reverse proxy for anything beyond localhost.",
            args.host,
            "control the heat pump" if args.allow_control else "read the data",
        )

    mqtt = MqttConfig(
        host=args.mqtt_host,
        port=args.mqtt_port,
        username=os.environ.get("MQTT_USER") or None,
        password=os.environ.get("MQTT_PASSWORD") or None,
        base_topic=base_topic,
    )
    mode = "control ENABLED" if args.allow_control else "read-only"
    print(
        f"Aquarea web UI on http://{args.host}:{args.port} ({mode}, HeishaMon via mqtt://{mqtt.host}:{mqtt.port}/"
        f"{mqtt.base_topic}, history every {args.poll_minutes} min -> {args.history_file})"
    )
    if ha_config:
        which = ", ".join(ha_config.entity_ids) or "all temperature sensors"
        print(f"Home Assistant sensors from {ha_config.url}: {which}")
    web.run_app(
        create_app(
            mqtt,
            args.allow_control,
            args.poll_minutes,
            args.history_file,
            frozenset(args.allow_ip),
            ha_config,
            args.tuning_file,
            location,
            args.device_id,
            args.device_name,
        ),
        host=args.host,
        port=args.port,
        print=None,
    )


if __name__ == "__main__":
    main()
