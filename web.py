"""Local web UI for the Aquarea read-only reporter.

Run:  python web.py            -> http://127.0.0.1:8080
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import ipaddress
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from aioaquarea import (
    ApiError,
    AuthenticationError,
    Client,
    ClientError,
)
from aioaquarea.data import OperationStatus, QuietMode, UpdateOperationMode, ZoneTemperatureSetUpdate
from aioaquarea.errors import AuthenticationErrorCodes

import compressor
import quiet
import tuning
from aquarea_report import CONSUMPTION_PERIODS, build_report, parse_heat_curve, raw_device_list_entry, water_target
from forecast import Forecast, parse_location
from ha_sensors import HAConfig, fetch_sensors
from history import append_rows, count_rising_edges, extract_rows, read_rows
from token_store import PersistentClient, TokenStore

_LOGGER = logging.getLogger("aquarea_web")

STATIC_DIR = Path(__file__).parent / "static"
COOKIE_NAME = "aq_session"
COOKIE_MAX_AGE = 30 * 24 * 3600  # seconds
MAX_FIELD_LENGTH = 256
ALLOW_CONTROL = web.AppKey("allow_control", bool)
POLL_MINUTES = web.AppKey("poll_minutes", int)
HISTORY_FILE = web.AppKey("history_file", Path)
TOKEN_STORE = web.AppKey("token_store", TokenStore)
ALLOWED_IPS = web.AppKey("allowed_ips", frozenset)
SHARED_SESSION = web.AppKey("shared_session", bool)
HA_CONFIG = web.AppKey("ha_config", object)  # HAConfig | None
HA_HTTP = web.AppKey("ha_http", aiohttp.ClientSession)
HEAT_CURVE = web.AppKey("heat_curve", object)  # list[(outdoor, water)] | None
TUNER = web.AppKey("tuner", tuning.Tuner)
FORECAST = web.AppKey("forecast", Forecast)
FORECAST_HTTP = web.AppKey("forecast_http", aiohttp.ClientSession)

# aioaquarea keeps device zones in a class-level dict, so reports must not be
# built concurrently (even across different user sessions).
REPORT_LOCK = asyncio.Lock()
# Today's energy data is fetched on every poll (the cloud updates it about every
# 15 min); month/year totals are refreshed less often.
CONSUMPTION_MAX_AGE = dt.timedelta(minutes=10)


@dataclass
class UserSession:
    client: PersistentClient
    http: aiohttp.ClientSession
    session_hash: str = ""
    last_report: dict[str, Any] | None = None
    poll_task: asyncio.Task | None = None
    consumption_at: dt.datetime | None = None

    async def close(self) -> None:
        if self.poll_task:
            self.poll_task.cancel()
        await self.http.close()


# Sessions live until logout and are restored from the token store on restart,
# so background polling keeps logging history while no browser is open.
# Keyed by SHA-256 of the cookie value (the raw cookie is never stored).
SESSIONS: dict[str, UserSession] = {}


def _hash(cookie: str) -> str:
    return hashlib.sha256(cookie.encode()).hexdigest()


def _get_session(request: web.Request) -> UserSession | None:
    if request.app[SHARED_SESSION]:
        # Access is already authenticated upstream (e.g. HA ingress), so every
        # client uses the one Aquarea login regardless of its cookie.
        return next(iter(SESSIONS.values()), None)
    token = request.cookies.get(COOKIE_NAME)
    return SESSIONS.get(_hash(token)) if token else None


def _persist(app: web.Application, session: UserSession) -> None:
    try:
        app[TOKEN_STORE].save(
            session.client.username, {**session.client.export_token(), "session_hash": session.session_hash}
        )
    except OSError as ex:
        _LOGGER.error("Could not store token: %s", ex)


def _start_session(app: web.Application, session: UserSession, poll_now: bool = False) -> None:
    session.client.on_token = lambda: _persist(app, session)
    session.poll_task = asyncio.create_task(poll_loop(app, session, poll_now))
    SESSIONS[session.session_hash] = session


def _require_json(request: web.Request) -> None:
    # Blocks cross-site form posts (CSRF); browsers can't send JSON cross-origin
    # without a CORS preflight, which this server never approves.
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="Expected application/json")


async def refresh_report(
    app: web.Application, session: UserSession, include_consumption: bool = True, reuse_consumption: bool = False
) -> dict[str, Any]:
    """Fetch a fresh report, cache it on the session and append to the CSV history.

    With reuse_consumption, today's energy data is always fetched, but month/year
    totals are only fetched when the cached copy is older than CONSUMPTION_MAX_AGE;
    otherwise they are carried over from the last report.
    """
    now = dt.datetime.now(dt.timezone.utc)
    reuse = (
        include_consumption
        and reuse_consumption
        and session.last_report is not None
        and session.consumption_at is not None
        and now - session.consumption_at < CONSUMPTION_MAX_AGE
    )
    periods = ("day",) if reuse else CONSUMPTION_PERIODS
    async with REPORT_LOCK:
        await session.client.ensure_token()
        try:
            report = await build_report(session.client, include_consumption, periods)
        except AuthenticationError as ex:
            if ex.error_code == AuthenticationErrorCodes.INVALID_USERNAME_OR_PASSWORD:
                raise
            # Token rejected early (e.g. another login on this account): renew once.
            _LOGGER.info("Token rejected (%s), renewing", ex)
            await session.client.login()
            report = await build_report(session.client, include_consumption, periods)
    if reuse:
        previous = {d.get("info", {}).get("device_id"): d for d in session.last_report.get("devices", [])}
        for dev in report["devices"]:
            old = (previous.get(dev.get("info", {}).get("device_id"), {}).get("consumption")) or {}
            for key in CONSUMPTION_PERIODS:
                if key in old and key not in dev.get("consumption", {}):
                    dev.setdefault("consumption", {})[key] = old[key]
    elif include_consumption:
        session.consumption_at = now
    else:
        session.consumption_at = None  # report without energy data: fetch it on the next poll
    if app[HA_CONFIG] is not None:
        report["ha_sensors"] = await fetch_sensors(app[HA_HTTP], app[HA_CONFIG])
    await app[FORECAST].refresh(app[FORECAST_HTTP], app[HA_CONFIG])
    for dev in report["devices"]:
        dev["water_target"] = water_target(dev.get("status"), app[HEAT_CURVE])
    session.last_report = report
    try:
        append_rows(app[HISTORY_FILE], extract_rows(report))
        midnight = dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        # at least 12 h back, so that "off since" also covers the night
        read_from = min(midnight, dt.datetime.now().astimezone() - dt.timedelta(hours=12)) - compressor.CONTEXT
        for dev in report["devices"]:
            device_id = dev.get("info", {}).get("device_id")
            rows = read_rows(app[HISTORY_FILE], read_from, device_id)
            today = [r for r in rows if dt.datetime.fromisoformat(r["timestamp"]) >= midnight]
            dev["defrost_cycles_today"] = count_rising_edges(today, "defrost")
            dev["compressor_today"] = compressor.daily_stats(
                compressor.estimate(rows), midnight, dt.timedelta(minutes=3 * app[POLL_MINUTES])
            )
    except OSError as ex:
        _LOGGER.error("Could not write history: %s", ex)
    for dev in report["devices"]:
        device_id = dev.get("info", {}).get("device_id")
        if device_id:
            try:
                await run_tuning(app, session, report, dev)
            except Exception:  # noqa: BLE001 - tuning must never break the report
                _LOGGER.exception("Tuning failed for %s", device_id)
            dev["tuning"] = app[TUNER].public(device_id)
            dev["quiet_auto"] = app[TUNER].public_quiet(device_id)
    return report


def _whole(value: Any) -> int | None:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return _strict_int(value)


def _observe(app: web.Application, report: dict[str, Any], dev: dict[str, Any]) -> tuning.Observation:
    """What the tuner needs from a fresh report and the history."""
    device_id = dev["info"]["device_id"]
    settings = app[TUNER].entry(device_id)["settings"]
    status = dev.get("status") if isinstance(dev.get("status"), dict) and "error" not in dev["status"] else {}
    zone1 = next((z for z in status.get("zones") or [] if z.get("zone_id") == 1), {})
    lo, hi = _whole(zone1.get("heat_min")), _whole(zone1.get("heat_max"))
    wt = dev.get("water_target") or {}
    now = dt.datetime.now().astimezone()
    rows = compressor.estimate(read_rows(app[HISTORY_FILE], now - tuning.HISTORY_WINDOW, device_id))
    latest = rows[-1] if rows else {}
    sensor = next(
        (
            s
            for s in (report.get("ha_sensors") or {}).get("sensors") or []
            if isinstance(s, dict) and s.get("entity_id") == settings["indoor_sensor"]
        ),
        {},
    )
    indoor = sensor.get("value")
    extra = dev.get("status_extra") or {}
    direction = (status.get("direction") or {}).get("name") if isinstance(status.get("direction"), dict) else None
    # The operation mode stays e.g. HEAT when the unit is switched off; the zones go OFF
    # (same source as the "Operation" tile and the history).
    zones_on = any((z.get("operation_status") or {}).get("name") == "ON" for z in status.get("zones") or [])
    current = latest.get("timestamp") == report["generated_at"]  # history write may have failed
    comp_state = compressor.state_of(rows) if current else {}
    return tuning.Observation(
        now=now,
        control=app[ALLOW_CONTROL],
        unit_on=bool(status) and (status.get("operation_mode") or {}).get("name") not in (None, "OFF") and zones_on,
        operation_on=(zone1.get("operation_status") or {}).get("name") == "ON",
        curve_mode=wt.get("curve") is not None,
        shift=_whole(wt.get("shift")) if wt.get("curve") is not None else None,
        limits=(lo, hi) if lo is not None and hi is not None else None,
        water=zone1.get("temperature") if isinstance(zone1.get("temperature"), (int, float)) else None,
        target=wt.get("target") if isinstance(wt.get("target"), (int, float)) else None,
        indoor=indoor if isinstance(indoor, (int, float)) and not isinstance(indoor, bool) else None,
        outdoor=status.get("temperature_outdoor") if _is_number(status.get("temperature_outdoor")) else None,
        # only the row of this report counts (history write may have failed)
        compressor=latest.get("compressor") if current else None,
        compressor_since=dt.datetime.fromisoformat(comp_state["since"]) if comp_state.get("since") else None,
        heating=direction != "WATER" and extra.get("defrost") != 1,
        status_ok=bool(status),
        defrost=extra.get("defrost") == 1,
        quiet=_strict_int((status.get("quiet_mode") or {}).get("value")) if isinstance(status.get("quiet_mode"), dict) else None,
        hold_until=compressor.defrost_hold_until(rows, now, extra.get("defrost") == 1),
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class ControlError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


async def _send_power(client: Client, device_id: str, on: bool, why: str) -> None:
    """Heating on (HEAT, all zones on) or the whole unit off; call with REPORT_LOCK held."""
    client._device_manager._devices = None  # noqa: SLF001 - force fresh device list
    info = next((d for d in await client.get_devices() if d.device_id == device_id), None)
    if info is None:
        raise ControlError("Unknown device", 404)
    status = await client.get_device_status(info)
    zones = [z for z in status.zones if z.zone_id is not None and z.operation_status is not None]
    if not zones:
        raise ControlError("No zones in the live status; refusing to send.", 409)
    zone_state = OperationStatus.ON if on else OperationStatus.OFF
    tank = status.tank_status[0].operation_status if status.tank_status else OperationStatus.OFF
    _LOGGER.warning("CONTROL %s: device %s heating -> %s (%s)", client.username, device_id, zone_state.name, why)
    await client.post_device_operation_update(
        info.long_id,
        UpdateOperationMode.HEAT if on else UpdateOperationMode.OFF,
        {z.zone_id: zone_state for z in zones},
        OperationStatus.ON if on or tank == OperationStatus.ON else OperationStatus.OFF,
        tank,
        # Resend the current setpoints, as the official app does
        [ZoneTemperatureSetUpdate(zone_id=z.zone_id, heat_set=z.heat_set, cool_set=z.cool_set) for z in zones],
    )


async def run_night_off(app: web.Application, session: UserSession, dev: dict[str, Any], obs: tuning.Observation) -> bool:
    """Night-time shutoff; True if a power command was sent."""
    device_id = dev["info"]["device_id"]
    tuner = app[TUNER]
    window = tuning.night_window(tuner.entry(device_id)["settings"], obs.now)
    cmd = tuner.evaluate_night_off(device_id, obs, app[FORECAST].summary(window))
    if cmd is None:
        return False
    error = None
    try:
        async with REPORT_LOCK:
            await session.client.ensure_token()
            await _send_power(session.client, device_id, cmd.on, f"night-time shutoff: {cmd.reason}")
    except (ControlError, AuthenticationError, ApiError, ClientError, aiohttp.ClientError) as ex:
        error = str(ex) or type(ex).__name__
        _LOGGER.warning("Night-time shutoff command failed: %s", error)
    tuner.record_power(device_id, cmd, obs.now, error)
    return True


async def _send_quiet(client: Client, device_id: str, level: int, why: str) -> None:
    """Set the quiet mode level; call with REPORT_LOCK held."""
    client._device_manager._devices = None  # noqa: SLF001 - force fresh device list
    info = next((d for d in await client.get_devices() if d.device_id == device_id), None)
    if info is None:
        raise ControlError("Unknown device", 404)
    mode = QuietMode(level)
    _LOGGER.warning("CONTROL %s: device %s quiet mode -> %s (%s)", client.username, device_id, mode.name, why)
    await client.post_device_set_quiet_mode(info.long_id, mode)


async def run_quiet(app: web.Application, session: UserSession, dev: dict[str, Any], obs: tuning.Observation) -> None:
    device_id = dev["info"]["device_id"]
    tuner = app[TUNER]
    cmd = tuner.evaluate_quiet(device_id, obs)
    if cmd is None:
        return
    error = None
    try:
        async with REPORT_LOCK:
            await session.client.ensure_token()
            await _send_quiet(session.client, device_id, cmd.level, f"quiet auto: {cmd.reason}")
    except (ControlError, AuthenticationError, ApiError, ClientError, aiohttp.ClientError) as ex:
        error = str(ex) or type(ex).__name__
        _LOGGER.warning("Quiet auto command failed: %s", error)
    tuner.record_quiet(device_id, cmd, obs.now, error)


async def run_tuning(app: web.Application, session: UserSession, report: dict[str, Any], dev: dict[str, Any]) -> None:
    device_id = dev["info"]["device_id"]
    tuner = app[TUNER]
    obs = _observe(app, report, dev)
    if await run_night_off(app, session, dev, obs):
        return  # the unit state changes; tune on the next poll
    await run_quiet(app, session, dev, obs)
    cmd = tuner.evaluate(device_id, obs)
    if cmd is None:
        return
    client = session.client
    error = None
    try:
        async with REPORT_LOCK:
            await client.ensure_token()
            limits = await _zone_heat_limits(client, device_id, 1)
            if limits is None or not limits[0] <= cmd.value <= limits[1]:
                error = f"allowed range {limits} does not include {cmd.value}"
            else:
                _LOGGER.warning(
                    "CONTROL %s: tuning device %s zone 1 heatSet %s -> %s (%s)",
                    client.username, device_id, cmd.previous, cmd.value, cmd.reason,
                )
                await client.post_device_zone_heat_temperature(device_id, 1, cmd.value)
    except (AuthenticationError, ApiError, ClientError, aiohttp.ClientError, web.HTTPException) as ex:
        error = str(ex) or type(ex).__name__
        _LOGGER.warning("Tuning command failed: %s", error)
    tuner.record(device_id, cmd, obs.now, error)


async def poll_loop(app: web.Application, session: UserSession, poll_now: bool = False) -> None:
    interval = app[POLL_MINUTES] * 60
    while True:
        if not poll_now:
            await asyncio.sleep(interval)
        poll_now = False
        try:
            await refresh_report(app, session, reuse_consumption=True)
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001 - keep polling on any failure
            _LOGGER.warning("Scheduled update failed: %s", ex)


def _session_info(request: web.Request, session: UserSession | None) -> dict[str, Any]:
    expiry = session.client.token_expiration if session else None
    return {
        "logged_in": session is not None,
        "username": session.client.username if session else None,
        "token_expiration": expiry.isoformat() if expiry else None,
        "allow_control": request.app[ALLOW_CONTROL],
        "poll_minutes": request.app[POLL_MINUTES],
    }


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
    return web.json_response(_session_info(request, _get_session(request)))


async def api_login(request: web.Request) -> web.Response:
    _require_json(request)
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid JSON")
    username = str(body.get("username", "")).strip()[:MAX_FIELD_LENGTH]
    password = str(body.get("password", ""))[:MAX_FIELD_LENGTH]
    if not username or not password:
        return web.json_response({"error": "Username and password are required."}, status=400)

    http = aiohttp.ClientSession()
    client = PersistentClient(http, username, password)
    try:
        await client.login()
    except AuthenticationError:
        await http.close()
        return web.json_response({"error": "Login failed. Check username and password."}, status=401)
    except (ApiError, ClientError, aiohttp.ClientError) as ex:
        await http.close()
        _LOGGER.warning("Login error: %s", ex)
        return web.json_response({"error": "Aquarea cloud not reachable. Try again later."}, status=502)

    # Replace existing sessions of this browser or this account: Panasonic
    # allows only one active login per account anyway.
    old = request.cookies.get(COOKIE_NAME)
    old_hash = _hash(old) if old else None
    shared = request.app[SHARED_SESSION]
    for key in [k for k, s in SESSIONS.items() if shared or k == old_hash or s.client.username == username]:
        await SESSIONS.pop(key).close()

    token = secrets.token_urlsafe(32)
    session = UserSession(client=client, http=http, session_hash=_hash(token))
    _start_session(request.app, session)
    _persist(request.app, session)
    response = web.json_response(_session_info(request, session))
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="Strict",
        secure=request.secure,
        max_age=COOKIE_MAX_AGE,
        path="/",
    )
    return response


async def api_logout(request: web.Request) -> web.Response:
    _require_json(request)
    session = _get_session(request)
    if session is not None:
        await SESSIONS.pop(session.session_hash).close()
        try:
            request.app[TOKEN_STORE].remove(session.client.username)
        except OSError as ex:
            _LOGGER.error("Could not remove stored token: %s", ex)
    response = web.json_response({"logged_in": False})
    response.del_cookie(COOKIE_NAME, path="/")
    return response


async def api_report(request: web.Request) -> web.Response:
    session = _get_session(request)
    if session is None:
        return web.json_response({"error": "Not logged in."}, status=401)
    include_consumption = request.query.get("consumption", "1") != "0"
    try:
        report = await refresh_report(request.app, session, include_consumption)
    except AuthenticationError:
        return web.json_response({"error": "Aquarea session expired. Please log in again."}, status=401)
    except (ApiError, ClientError, aiohttp.ClientError) as ex:
        _LOGGER.warning("Report error: %s", ex)
        return web.json_response({"error": f"Aquarea API error: {ex}"}, status=502)
    return web.json_response(report)


async def api_report_latest(request: web.Request) -> web.Response:
    """Last report fetched by the background poller (no cloud request)."""
    session = _get_session(request)
    if session is None:
        return web.json_response({"error": "Not logged in."}, status=401)
    return web.json_response({"report": session.last_report})


async def api_history(request: web.Request) -> web.Response:
    if _get_session(request) is None:
        return web.json_response({"error": "Not logged in."}, status=401)
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


def _strict_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


async def _zone_heat_limits(client: Client, device_id: str, zone_id: int) -> tuple[int, int] | None:
    """Current heat min/max for a zone, from live status or the device list."""
    client._device_manager._devices = None  # noqa: SLF001 - force fresh device list
    info = next((d for d in await client.get_devices() if d.device_id == device_id), None)
    if info is None:
        raise web.HTTPNotFound(text="Unknown device")

    try:
        status = await client.get_device_status(info)
        zone = next((z for z in status.zones if z.zone_id == zone_id), None)
        if zone and zone.heat_min is not None and zone.heat_max is not None:
            return zone.heat_min, zone.heat_max
    except (ApiError, ClientError) as ex:
        _LOGGER.info("Status unavailable, using device list limits: %s", ex)

    raw = raw_device_list_entry(client, info) or {}
    for zone in raw.get("zoneStatus") or []:
        if isinstance(zone, dict) and zone.get("zoneId") == zone_id:
            lo, hi = _strict_int(zone.get("heatMin")), _strict_int(zone.get("heatMax"))
            if lo is not None and hi is not None:
                return lo, hi
    return None


async def api_zone_heat_target(request: web.Request) -> web.Response:
    if not request.app[ALLOW_CONTROL]:
        return web.json_response(
            {"error": "Control is disabled. Start the server with --allow-control."}, status=403
        )
    _require_json(request)
    session = _get_session(request)
    if session is None:
        return web.json_response({"error": "Not logged in."}, status=401)

    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid JSON")
    device_id = body.get("device_id")
    zone_id = _strict_int(body.get("zone_id"))
    value = _strict_int(body.get("value"))
    if not isinstance(device_id, str) or not 0 < len(device_id) <= 64 or zone_id is None or value is None:
        return web.json_response({"error": "device_id (str), zone_id (int) and value (int) required."}, status=400)

    client = session.client
    try:
        async with REPORT_LOCK:
            await client.ensure_token()
            limits = await _zone_heat_limits(client, device_id, zone_id)
            if limits is None:
                return web.json_response(
                    {"error": f"Allowed range for zone {zone_id} is unknown; refusing to send."}, status=409
                )
            lo, hi = limits
            if not lo <= value <= hi:
                return web.json_response({"error": f"Value must be between {lo} and {hi}."}, status=400)

            _LOGGER.warning(
                "CONTROL %s: device %s zone %s heatSet -> %s", client.username, device_id, zone_id, value
            )
            await client.post_device_zone_heat_temperature(device_id, zone_id, value)
    except AuthenticationError:
        return web.json_response({"error": "Aquarea session expired. Please log in again."}, status=401)
    except (ApiError, ClientError, aiohttp.ClientError) as ex:
        _LOGGER.warning("Control error: %s", ex)
        return web.json_response({"error": f"Aquarea API error: {ex}"}, status=502)

    return web.json_response({"ok": True, "device_id": device_id, "zone_id": zone_id, "value": value, "range": [lo, hi]})


async def api_device_power(request: web.Request) -> web.Response:
    """Turn heating on (HEAT, all zones on) or off (all zones off)."""
    if not request.app[ALLOW_CONTROL]:
        return web.json_response(
            {"error": "Control is disabled. Start the server with --allow-control."}, status=403
        )
    _require_json(request)
    session = _get_session(request)
    if session is None:
        return web.json_response({"error": "Not logged in."}, status=401)
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid JSON")
    device_id, on = body.get("device_id"), body.get("on")
    if not isinstance(device_id, str) or not 0 < len(device_id) <= 64 or not isinstance(on, bool):
        return web.json_response({"error": "device_id (str) and on (bool) required."}, status=400)

    client = session.client
    try:
        async with REPORT_LOCK:
            await client.ensure_token()
            await _send_power(client, device_id, on, "dashboard")
    except ControlError as ex:
        return web.json_response({"error": str(ex)}, status=ex.status)
    except AuthenticationError:
        return web.json_response({"error": "Aquarea session expired. Please log in again."}, status=401)
    except (ApiError, ClientError, aiohttp.ClientError) as ex:
        _LOGGER.warning("Control error: %s", ex)
        return web.json_response({"error": f"Aquarea API error: {ex}"}, status=502)
    return web.json_response({"ok": True, "device_id": device_id, "on": on})


async def api_device_quiet_mode(request: web.Request) -> web.Response:
    """Set the quiet mode level (0 = off, 1–3)."""
    if not request.app[ALLOW_CONTROL]:
        return web.json_response(
            {"error": "Control is disabled. Start the server with --allow-control."}, status=403
        )
    _require_json(request)
    session = _get_session(request)
    if session is None:
        return web.json_response({"error": "Not logged in."}, status=401)
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid JSON")
    device_id, level = body.get("device_id"), _strict_int(body.get("level"))
    if not isinstance(device_id, str) or not 0 < len(device_id) <= 64 or level not in {m.value for m in QuietMode}:
        return web.json_response({"error": "device_id (str) and level (0–3) required."}, status=400)

    client = session.client
    try:
        async with REPORT_LOCK:
            await client.ensure_token()
            await _send_quiet(client, device_id, level, "dashboard")
    except ControlError as ex:
        return web.json_response({"error": str(ex)}, status=ex.status)
    except AuthenticationError:
        return web.json_response({"error": "Aquarea session expired. Please log in again."}, status=401)
    except (ApiError, ClientError, aiohttp.ClientError) as ex:
        _LOGGER.warning("Control error: %s", ex)
        return web.json_response({"error": f"Aquarea API error: {ex}"}, status=502)
    request.app[TUNER].pause_quiet(device_id, dt.datetime.now().astimezone(), f"set manually to {level}")
    return web.json_response({"ok": True, "device_id": device_id, "level": level})


async def api_quiet_auto(request: web.Request) -> web.Response:
    """Save quiet auto settings for a device."""
    if not request.app[ALLOW_CONTROL]:
        return web.json_response(
            {"error": "Control is disabled. Start the server with --allow-control."}, status=403
        )
    _require_json(request)
    session = _get_session(request)
    if session is None:
        return web.json_response({"error": "Not logged in."}, status=401)
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid JSON")
    if not isinstance(body, dict):
        return web.json_response({"error": "JSON object required."}, status=400)
    device_id = body.get("device_id")
    known = {d.get("info", {}).get("device_id") for d in (session.last_report or {}).get("devices", [])}
    if not isinstance(device_id, str) or device_id not in known:
        return web.json_response({"error": "Unknown device."}, status=404)
    try:
        settings = quiet.validate_quiet(body.get("settings"))
    except ValueError as ex:
        return web.json_response({"error": str(ex)}, status=400)
    _LOGGER.warning("CONTROL %s: quiet auto device %s settings %s", session.client.username, device_id, settings)
    tuner = request.app[TUNER]
    tuner.update_quiet(device_id, settings, dt.datetime.now().astimezone())
    return web.json_response({"ok": True, "quiet_auto": tuner.public_quiet(device_id)})


async def api_tuning(request: web.Request) -> web.Response:
    """Save hysteresis tuning settings for a device."""
    if not request.app[ALLOW_CONTROL]:
        return web.json_response(
            {"error": "Control is disabled. Start the server with --allow-control."}, status=403
        )
    _require_json(request)
    session = _get_session(request)
    if session is None:
        return web.json_response({"error": "Not logged in."}, status=401)
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid JSON")
    if not isinstance(body, dict):
        return web.json_response({"error": "JSON object required."}, status=400)
    device_id = body.get("device_id")
    known = {d.get("info", {}).get("device_id") for d in (session.last_report or {}).get("devices", [])}
    if not isinstance(device_id, str) or device_id not in known:
        return web.json_response({"error": "Unknown device."}, status=404)
    try:
        settings = tuning.validate_settings(body.get("settings"))
    except ValueError as ex:
        return web.json_response({"error": str(ex)}, status=400)
    _LOGGER.warning("CONTROL %s: tuning device %s settings %s", session.client.username, device_id, settings)
    tuner = request.app[TUNER]
    tuner.update_settings(device_id, settings, dt.datetime.now().astimezone())
    return web.json_response({"ok": True, "tuning": tuner.public(device_id)})


async def api_early_off(request: web.Request) -> web.Response:
    """Start (until the end of the night or for some hours) or end an early off."""
    if not request.app[ALLOW_CONTROL]:
        return web.json_response(
            {"error": "Control is disabled. Start the server with --allow-control."}, status=403
        )
    _require_json(request)
    session = _get_session(request)
    if session is None:
        return web.json_response({"error": "Not logged in."}, status=401)
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid JSON")
    if not isinstance(body, dict):
        return web.json_response({"error": "JSON object required."}, status=400)
    device_id = body.get("device_id")
    known = {d.get("info", {}).get("device_id") for d in (session.last_report or {}).get("devices", [])}
    if not isinstance(device_id, str) or device_id not in known:
        return web.json_response({"error": "Unknown device."}, status=404)
    tuner = request.app[TUNER]
    now = dt.datetime.now().astimezone()
    action = body.get("action")
    if action == "cancel":
        _LOGGER.warning("CONTROL %s: early off device %s ended", session.client.username, device_id)
        tuner.cancel_early_off(device_id, now)
    elif action == "morning":
        window = tuning.night_window(tuner.entry(device_id)["settings"], now)
        if window is None:
            return web.json_response({"error": "No night hours set: choose a duration."}, status=400)
        _LOGGER.warning("CONTROL %s: early off device %s until %s", session.client.username, device_id, window[1])
        tuner.start_early_off(device_id, window[1], now)
    elif action == "hours":
        hours = body.get("hours")
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0.5 <= hours <= 48:
            return web.json_response({"error": "hours must be a number between 0.5 and 48."}, status=400)
        until = now + dt.timedelta(hours=hours)
        _LOGGER.warning("CONTROL %s: early off device %s until %s", session.client.username, device_id, until)
        tuner.start_early_off(device_id, until, now)
    else:
        return web.json_response({"error": "action must be morning, hours or cancel."}, status=400)
    return web.json_response({"ok": True, "tuning": tuner.public(device_id)})


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


async def on_startup(app: web.Application) -> None:
    """Restore sessions from stored tokens; the first poll renews them if needed."""
    if app[HA_CONFIG] is not None:
        app[HA_HTTP] = aiohttp.ClientSession()
    app[FORECAST_HTTP] = aiohttp.ClientSession()
    for username, entry in app[TOKEN_STORE].load().items():
        session_hash = entry.get("session_hash")
        if not isinstance(session_hash, str) or not entry.get("refresh_token"):
            continue
        http = aiohttp.ClientSession()
        client = PersistentClient(http, username)
        client.restore_token(entry)
        _start_session(app, UserSession(client=client, http=http, session_hash=session_hash), poll_now=True)
        _LOGGER.warning("Restored session for %s from %s", username, app[TOKEN_STORE].path)


async def on_shutdown(app: web.Application) -> None:
    for session in SESSIONS.values():
        await session.close()
    SESSIONS.clear()
    if HA_HTTP in app:
        await app[HA_HTTP].close()
    if FORECAST_HTTP in app:
        await app[FORECAST_HTTP].close()


def create_app(
    allow_control: bool = False,
    poll_minutes: int = 1,
    history_file: Path = Path("data/history.csv"),
    token_file: Path = Path("data/tokens.json"),
    allowed_ips: frozenset[str] = frozenset(),
    ha_config: HAConfig | None = None,
    heat_curve: list[tuple[float, float]] | None = None,
    tuning_file: Path = Path("data/tuning.json"),
    location: tuple[float, float] | None = None,
    shared_session: bool = False,
) -> web.Application:
    app = web.Application(middlewares=[client_allowlist, security_headers], client_max_size=16 * 1024)
    app[ALLOW_CONTROL] = allow_control
    app[POLL_MINUTES] = poll_minutes
    app[HISTORY_FILE] = history_file
    app[TOKEN_STORE] = TokenStore(token_file)
    app[ALLOWED_IPS] = allowed_ips
    app[SHARED_SESSION] = shared_session
    app[HA_CONFIG] = ha_config
    app[HEAT_CURVE] = heat_curve
    app[TUNER] = tuning.Tuner(tuning_file)
    app[FORECAST] = Forecast(location)
    app.router.add_get("/", index)
    app.router.add_get("/api/session", api_session)
    app.router.add_post("/api/login", api_login)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_get("/api/report", api_report)
    app.router.add_get("/api/report/latest", api_report_latest)
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
    parser = argparse.ArgumentParser(description="Aquarea web UI")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--allow-control",
        action="store_true",
        help="enable changing settings (zone heat target); read-only otherwise",
    )
    parser.add_argument("--poll-minutes", type=int, default=1, help="auto-update interval (default: 1)")
    parser.add_argument(
        "--history-file", type=Path, default=Path("data/history.csv"), help="CSV history file"
    )
    parser.add_argument(
        "--token-file", type=Path, default=Path("data/tokens.json"), help="stored login tokens (no passwords)"
    )
    parser.add_argument(
        "--allow-ip",
        action="append",
        default=[],
        metavar="IP",
        help="only accept requests from this client IP (repeatable; e.g. 172.30.32.2 for HA ingress)",
    )
    parser.add_argument(
        "--shared-session",
        action="store_true",
        help="share one Aquarea login between all clients instead of per browser; "
        "only use behind an authenticating proxy such as HA ingress",
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
        "--heat-curve",
        metavar="OUT:WATER,...",
        help="zone 1 heating curve, outdoor °C : water target °C, e.g. '5:30,-7:34'",
    )
    parser.add_argument(
        "--tuning-file",
        type=Path,
        default=Path("data/tuning.json"),
        help="hysteresis tuning settings, state and log (default: data/tuning.json)",
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
    try:
        location = parse_location(args.location) if args.location else None
    except ValueError as ex:
        parser.error(f"--location: {ex}")
    try:
        heat_curve = parse_heat_curve(args.heat_curve) if args.heat_curve else None
    except ValueError as ex:
        parser.error(f"--heat-curve: {ex}")
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
            "Binding to %s over plain HTTP: credentials are sent unencrypted. "
            "Use a TLS reverse proxy for anything beyond localhost.",
            args.host,
        )

    mode = "control ENABLED" if args.allow_control else "read-only"
    print(
        f"Aquarea web UI on http://{args.host}:{args.port} ({mode}, "
        f"auto-update every {args.poll_minutes} min -> {args.history_file})"
    )
    if ha_config:
        which = ", ".join(ha_config.entity_ids) or "all temperature sensors"
        print(f"Home Assistant sensors from {ha_config.url}: {which}")
    web.run_app(
        create_app(
            args.allow_control,
            args.poll_minutes,
            args.history_file,
            args.token_file,
            frozenset(args.allow_ip),
            ha_config,
            heat_curve,
            args.tuning_file,
            location,
            args.shared_session,
        ),
        host=args.host,
        port=args.port,
        print=None,
    )


if __name__ == "__main__":
    main()
