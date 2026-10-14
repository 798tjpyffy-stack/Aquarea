"""Read-only reporter for Panasonic Aquarea Smart Cloud, built on aioaquarea.

Logs in with your Aquarea Smart Cloud credentials, enumerates every device on
the account and prints / saves all information the library can retrieve.
No control commands are ever sent.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import getpass
import json
import logging
import math
import os
import re
import sys
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import aiohttp
from dotenv import load_dotenv

from aioaquarea import (
    ApiError,
    AquareaEnvironment,
    AuthenticationError,
    Client,
    ClientError,
    Consumption,
    DateType,
    Device,
    DeviceInfo,
)

_LOGGER = logging.getLogger("aquarea_report")

TEMPERATURE_KEY = re.compile(
    r"temp|^(heat|cool)_(max|min|set)$|^(eco|comfort)_(heat|cool)$|^(heat|cool)$"
)
ENERGY_KEY = re.compile(r"_consumption$")
STATUS_API = re.compile(r"^/remote/v1/api/devices\?gwid=([^&]+)&deviceDirect=")
EXTRA_STATUS_FIELDS = {
    "waterPressure": "water_pressure",
    "deiceStatus": "defrost",
    "bivalentActual": "bivalent_active",
    "externalHeater": "external_heater",
    "forceHeater": "force_heater",
}


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def to_jsonable(obj: Any) -> Any:
    """Recursively convert aioaquarea objects into JSON-serializable data."""
    if isinstance(obj, Enum):
        return {"name": obj.name, "value": obj.value}
    if isinstance(obj, Consumption):
        return consumption_to_dict(obj)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {
            (k.name if isinstance(k, Enum) else str(k)): to_jsonable(v)
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


def consumption_to_dict(item: Consumption) -> dict[str, Any]:
    return {
        "data_time": item.data_time,
        "heat_consumption": item.heat_consumption,
        "cool_consumption": item.cool_consumption,
        "tank_consumption": item.tank_consumption,
        "total_consumption": item.total_consumption,
        "heat_cost": item.heat_cost,
        "cool_cost": item.cool_cost,
        "tank_cost": item.tank_cost,
        "outdoor_temp": item.outdoor_temp,
        "raw": to_jsonable(item.raw_data),
    }


def safe(fn: Callable[[], Any]) -> Any:
    """Evaluate a property lazily; capture errors instead of aborting."""
    try:
        return to_jsonable(fn())
    except Exception as ex:  # noqa: BLE001 - we want to report anything
        return {"error": f"{type(ex).__name__}: {ex}"}


# --------------------------------------------------------------------------- #
# Data gathering
# --------------------------------------------------------------------------- #
def derived_device_data(device: Device) -> dict[str, Any]:
    """Collect computed/derived properties exposed by the Device object."""
    zones: dict[str, Any] = {}
    for zone_id, zone in device.zones.items():
        zone_data = {
            "zone_id": zone.zone_id,
            "name": safe(lambda: zone.name),
            "type": safe(lambda: zone.type),
            "operation_status": safe(lambda: zone.operation_status),
            "temperature": safe(lambda: zone.temperature),
            "cool_mode": safe(lambda: zone.cool_mode),
            "sensor_mode": safe(lambda: zone.sensor_mode),
            "heat_sensor_mode": safe(lambda: zone.heat_sensor_mode),
            "cool_sensor_mode": safe(lambda: zone.cool_sensor_mode),
            "heat_target_temperature": safe(lambda: zone.heat_target_temperature),
            "heat_min": safe(lambda: zone.heat_min),
            "heat_max": safe(lambda: zone.heat_max),
            "cool_target_temperature": safe(lambda: zone.cool_target_temperature),
            "cool_min": safe(lambda: zone.cool_min),
            "cool_max": safe(lambda: zone.cool_max),
            "supports_set_temperature": safe(lambda: zone.supports_set_temperature),
            "supports_special_status": safe(lambda: zone.supports_special_status),
            "device_supports_cooling": safe(lambda: device.support_cooling(zone_id)),
        }
        if zone.supports_special_status:
            zone_data["eco_modifiers"] = safe(lambda: zone.eco)
            zone_data["comfort_modifiers"] = safe(lambda: zone.comfort)
        zones[str(zone_id)] = zone_data

    tank = None
    if device.has_tank and device.tank is not None:
        t = device.tank
        tank = {
            "operation_status": safe(lambda: t.operation_status),
            "temperature": safe(lambda: t.temperature),
            "target_temperature": safe(lambda: t.target_temperature),
            "heat_min": safe(lambda: t.heat_min),
            "heat_max": safe(lambda: t.heat_max),
        }

    return {
        "manufacturer": safe(lambda: device.manufacturer),
        "current_action": safe(lambda: device.current_action),
        "current_direction": safe(lambda: device.current_direction),
        "mode": safe(lambda: device.mode),
        "operation_status": safe(lambda: device.operation_status),
        "device_mode_status": safe(lambda: device.device_mode_status),
        "temperature_outdoor": safe(lambda: device.temperature_outdoor),
        "is_on_error": safe(lambda: device.is_on_error),
        "current_error": safe(lambda: device.current_error),
        "support_special_status": safe(lambda: device.support_special_status),
        "special_status": safe(lambda: device.special_status),
        "heat_max": safe(lambda: getattr(device, "heat_max", None)),
        "cool_max": safe(lambda: getattr(device, "cool_max", None)),
        "zones": zones,
        "tank": tank,
    }


def raw_device_list_entry(client: Client, info: DeviceInfo) -> Any:
    """Raw entry for this device from the /device/group response.

    aioaquarea does not expose this publicly, but it already contains live-ish
    values (zone temperatureNow, tankStatus, ...) and is a useful fallback
    when the status endpoint fails.
    """
    groups = client._device_manager._groups or {}  # noqa: SLF001
    for group in groups.get("groupList", []):
        for raw in group.get("deviceList") or group.get("deviceIdList") or []:
            if raw and raw.get("deviceGuid") == info.device_id:
                return raw
    return None


def capture_raw_status(client: Client) -> None:
    """Keep the raw status JSON per device; aioaquarea drops fields such as
    waterPressure and bivalentActual when parsing it. Idempotent."""
    if getattr(client, "_raw_status", None) is not None:
        return
    client._raw_status = {}  # noqa: SLF001
    original = client._api_client.request  # noqa: SLF001

    async def request(*args: Any, **kwargs: Any) -> Any:
        response = await original(*args, **kwargs)
        payload = kwargs.get("json")
        match = STATUS_API.match(payload.get("apiName", "")) if isinstance(payload, dict) else None
        if match:
            try:
                body = await response.json()  # aiohttp caches the body for the library's own read
            except (aiohttp.ContentTypeError, ValueError):
                body = None
            if isinstance(body, dict) and isinstance(body.get("status"), dict):
                client._raw_status[match.group(1)] = body["status"]  # noqa: SLF001
        return response

    client._api_client.request = request  # noqa: SLF001


def extra_status(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None
    return {name: raw.get(key) for key, name in EXTRA_STATUS_FIELDS.items()}


HEAT_CURVE_POINT = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*:\s*(-?\d+(?:\.\d+)?)\s*$")


def parse_heat_curve(text: str) -> list[tuple[float, float]]:
    """'-7:34,5:30' -> [(-7, 34), (5, 30)]: outdoor °C -> water target °C."""
    points = []
    for part in text.split(","):
        match = HEAT_CURVE_POINT.match(part)
        if not match:
            raise ValueError(f"invalid heating curve point {part.strip()!r} (expected OUTDOOR:WATER)")
        points.append((float(match.group(1)), float(match.group(2))))
    points.sort()
    if len(points) < 2 or len({p[0] for p in points}) != len(points):
        raise ValueError("heating curve needs at least two points with different outdoor temperatures")
    return points


def curve_value(curve: list[tuple[float, float]], outdoor: float) -> float:
    """Linear between the points, flat outside them (like the unit's curve)."""
    if outdoor <= curve[0][0]:
        return curve[0][1]
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if outdoor <= x1:
            return y0 + (y1 - y0) * (outdoor - x0) / (x1 - x0)
    return curve[-1][1]


def water_target(status: Any, curve: list[tuple[float, float]] | None) -> dict[str, Any] | None:
    """Zone 1 water target: heating curve at the current outdoor temperature plus the shift."""
    if not isinstance(status, dict) or "error" in status:
        return None
    zone1 = next((z for z in status.get("zones") or [] if z.get("zone_id") == 1), None)
    outdoor = status.get("temperature_outdoor")
    if not zone1 or not isinstance(outdoor, (int, float)):
        return None
    heat_set, heat_min = zone1.get("heat_set"), zone1.get("heat_min")
    if not (isinstance(heat_min, (int, float)) and heat_min < 0):
        # Direct mode: heat_set is the water target itself
        return {"outdoor": outdoor, "curve": None, "shift": None, "target": heat_set}
    if not curve:
        return None
    base = curve_value(curve, outdoor)
    # The unit works in whole degrees (outdoor, shift and water target are
    # integer bytes in its protocol): curve value rounded half up, then + shift.
    curve_int = math.floor(base + 0.5)
    shift = heat_set if isinstance(heat_set, (int, float)) else 0
    return {
        "outdoor": outdoor,
        "curve": curve_int,
        "curve_exact": round(base, 2),
        "shift": shift,
        "target": curve_int + shift,
    }


CONSUMPTION_PERIODS = ("day", "month", "year")


async def gather_consumption(
    client: Client, long_id: str, periods: tuple[str, ...] = CONSUMPTION_PERIODS
) -> dict[str, Any]:
    """Fetch consumption for today (hourly), this month (daily), this year (monthly)."""
    today = dt.date.today()
    requests = {
        "day": (DateType.DAY, today.strftime("%Y%m%d")),
        "month": (DateType.MONTH, today.strftime("%Y%m01")),
        "year": (DateType.YEAR, today.strftime("%Y0101")),
    }
    result: dict[str, Any] = {}
    for key, (date_type, date_str) in requests.items():
        if key not in periods:
            continue
        try:
            items = await client.get_device_consumption(long_id, date_type, date_str)
        except (ApiError, ClientError) as ex:
            _LOGGER.warning("Consumption (%s) failed: %s", key, ex)
            result[key] = {"date": date_str, "error": str(ex), "items": []}
            continue
        result[key] = {
            "date": date_str,
            "items": [consumption_to_dict(i) for i in items] if items else [],
        }
    return result


async def gather_device(
    client: Client,
    info: DeviceInfo,
    include_consumption: bool,
    consumption_periods: tuple[str, ...] = CONSUMPTION_PERIODS,
) -> dict[str, Any]:
    data: dict[str, Any] = {"info": to_jsonable(info)}
    data["raw_device_list_entry"] = safe(lambda: raw_device_list_entry(client, info))
    client._raw_status.pop(info.device_id, None)  # noqa: SLF001

    try:
        # get_device() fetches the status itself; one request instead of two.
        device = await client.get_device(device_info=info)
    except (ApiError, ClientError) as ex:
        _LOGGER.error("Status for %s failed: %s (cause: %r)", info.name, ex, ex.__cause__)
        data["status"] = {"error": str(ex), "cause": repr(ex.__cause__)}
        data["derived"] = {"error": "skipped, status unavailable"}
        if include_consumption:
            data["consumption"] = await gather_consumption(client, info.long_id, consumption_periods)
        return data

    data["status"] = to_jsonable(device._status)  # noqa: SLF001
    data["status_extra"] = extra_status(client._raw_status.get(info.device_id))  # noqa: SLF001
    # Device zones are stored on a class-level dict in aioaquarea, so read
    # derived data immediately for this device before fetching the next.
    data["derived"] = safe(lambda: derived_device_data(device))

    if include_consumption:
        data["consumption"] = await gather_consumption(client, info.long_id, consumption_periods)

    return data


async def build_report(
    client: Client, include_consumption: bool, consumption_periods: tuple[str, ...] = CONSUMPTION_PERIODS
) -> dict[str, Any]:
    """Collect the full report using an already logged-in client."""
    # aioaquarea caches the device list (and its raw data) after the first
    # call; reset it so every report reflects the current cloud state.
    client._device_manager._devices = None  # noqa: SLF001
    capture_raw_status(client)
    devices = await client.get_devices()
    report: dict[str, Any] = {
        "generated_at": dt.datetime.now().astimezone().isoformat(),
        "account": {
            "username": client.username,
            "token_expiration": to_jsonable(client.token_expiration),
            "device_count": len(devices),
        },
        "devices": [],
    }
    for info in devices:
        report["devices"].append(await gather_device(client, info, include_consumption, consumption_periods))
    return report


async def gather_all(
    username: str, password: str, include_consumption: bool
) -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        client = Client(
            session=session,
            username=username,
            password=password,
            refresh_login=True,
            environment=AquareaEnvironment.PRODUCTION,
        )
        await client.login()
        return await build_report(client, include_consumption)


# --------------------------------------------------------------------------- #
# Console output
# --------------------------------------------------------------------------- #
def fmt_value(key: str, value: Any) -> str:
    if isinstance(value, dict) and set(value) == {"name", "value"}:
        return str(value["name"])
    if isinstance(value, bool) or value is None:
        return str(value)
    if isinstance(value, (int, float)):
        if ENERGY_KEY.search(key):
            return f"{value} kWh"
        if TEMPERATURE_KEY.search(key):
            return f"{value} °C"
    return str(value)


def is_leaf(value: Any) -> bool:
    return not isinstance(value, (dict, list)) or (
        isinstance(value, dict) and set(value) == {"name", "value"}
    )


def print_tree(data: Any, indent: int = 2) -> None:
    pad = " " * indent
    if isinstance(data, dict):
        for key, value in data.items():
            if is_leaf(value):
                print(f"{pad}{key:<28} {fmt_value(key, value)}")
            elif not value:
                print(f"{pad}{key:<28} -")
            else:
                print(f"{pad}{key}:")
                print_tree(value, indent + 2)
    elif isinstance(data, list):
        for i, value in enumerate(data):
            if is_leaf(value):
                print(f"{pad}- {fmt_value('', value)}")
            else:
                print(f"{pad}[{i}]")
                print_tree(value, indent + 2)


def print_consumption_table(title: str, block: dict[str, Any]) -> None:
    print(f"  {title} (date param {block.get('date')})")
    if block.get("error"):
        print(f"    error: {block['error']}")
        return
    items = block.get("items") or []
    if not items:
        print("    no data")
        return
    header = f"    {'time':<12}{'heat kWh':>10}{'cool kWh':>10}{'tank kWh':>10}{'total kWh':>11}{'outdoor °C':>12}"
    print(header)
    print("    " + "-" * (len(header) - 4))

    def cell(v: Any, width: int) -> str:
        return f"{'-' if v is None else v:>{width}}"

    for it in items:
        print(
            f"    {str(it['data_time']):<12}"
            f"{cell(it['heat_consumption'], 10)}"
            f"{cell(it['cool_consumption'], 10)}"
            f"{cell(it['tank_consumption'], 10)}"
            f"{cell(it['total_consumption'], 11)}"
            f"{cell(it['outdoor_temp'], 12)}"
        )


def section(title: str) -> None:
    print()
    print(f"== {title} ".ljust(78, "="))


def print_report(report: dict[str, Any]) -> None:
    section("Account")
    print_tree(report["account"])

    for device in report["devices"]:
        name = device["info"].get("name", "?")
        model = device["info"].get("model", "?")
        print()
        print("#" * 78)
        print(f"# Device: {name} ({model})")
        print("#" * 78)

        section("Configuration (DeviceInfo)")
        print_tree(device["info"])

        section("Raw device list entry (from login)")
        print_tree(device["raw_device_list_entry"])

        section("Live status (DeviceStatus)")
        print_tree(device["status"])

        section("Derived / computed")
        print_tree(device["derived"])

        if "consumption" in device:
            section("Energy consumption")
            labels = {"day": "Today (hourly)", "month": "This month (daily)", "year": "This year (monthly)"}
            for key, label in labels.items():
                print_consumption_table(label, device["consumption"][key])
                print()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="JSON output path (default: output/aquarea_<timestamp>.json)",
    )
    parser.add_argument("--no-consumption", action="store_true", help="skip energy consumption data")
    parser.add_argument("--debug", action="store_true", help="enable debug logging")
    return parser.parse_args()


def get_credentials() -> tuple[str, str]:
    load_dotenv()
    username = os.getenv("AQUAREA_USERNAME") or input("Aquarea username (email): ").strip()
    password = os.getenv("AQUAREA_PASSWORD") or getpass.getpass("Aquarea password: ")
    if not username or not password:
        sys.exit("Username and password are required.")
    return username, password


def write_json(report: dict[str, Any], path: Path | None) -> Path:
    if path is None:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = Path("output") / f"aquarea_{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)  # contains account / device identifiers
    return path


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    username, password = get_credentials()

    try:
        report = asyncio.run(
            gather_all(username, password, not args.no_consumption)
        )
    except AuthenticationError as ex:
        print(f"Login failed: {ex}", file=sys.stderr)
        return 1
    except (ApiError, ClientError, aiohttp.ClientError) as ex:
        print(f"Aquarea API error: {ex}", file=sys.stderr)
        return 1

    print_report(report)
    out = write_json(report, args.json)
    print(f"\nJSON report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
