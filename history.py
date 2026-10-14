"""CSV history of key heat pump values."""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import Any

FIELDS = [
    "timestamp",
    "device_id",
    "device_name",
    "operation_status",
    "pump_duty",
    "direction",
    "defrost",
    "outdoor_temperature",
    "zone1_water_temperature",
    "zone1_target_temperature",
    "zone1_heat_shift",
    "energy_today_kwh",
    "source",
]
NUMERIC_FIELDS = {
    "outdoor_temperature",
    "zone1_water_temperature",
    "zone1_target_temperature",
    "zone1_heat_shift",
    "energy_today_kwh",
}
# Home Assistant sensors are stored in extra columns named "ha:<entity_id>".
HA_PREFIX = "ha:"
TEMPERATURE_UNITS = ("°C", "°F")


def _enum_name(value: Any) -> Any:
    if isinstance(value, dict) and "name" in value:
        return value["name"]
    return value


def _on_off(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, int):
        return value
    return {0: "OFF", 1: "ON"}.get(value, value)


def _usable(block: Any) -> dict | None:
    return block if isinstance(block, dict) and block and "error" not in block else None


def extract_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per device, from live status or (fallback) the raw device list."""
    rows = []
    ha_values = {
        f"{HA_PREFIX}{s['entity_id']}": s.get("value")
        for s in (report.get("ha_sensors") or {}).get("sensors") or []
        if isinstance(s, dict) and s.get("entity_id") and s.get("unit") in TEMPERATURE_UNITS
    }
    for dev in report.get("devices", []):
        info = dev.get("info", {})
        row = {
            "timestamp": report["generated_at"],
            "device_id": info.get("device_id"),
            "device_name": info.get("name"),
        }
        status = _usable(dev.get("status"))
        raw = _usable(dev.get("raw_device_list_entry"))
        if status:
            zones = status.get("zones") or []
            zone1 = next((z for z in zones if z.get("zone_id") == 1), {})
            zone0 = zones[0] if zones and isinstance(zones[0], dict) else {}
            extra = dev.get("status_extra") or {}
            defrost = _on_off(extra.get("defrost"))
            if defrost is None:
                mode_status = _enum_name(status.get("device_status"))
                defrost = {"DEFROST": "ON", "NORMAL": "OFF"}.get(mode_status)
            row |= {
                "operation_status": _enum_name(zone0.get("operation_status")),
                "pump_duty": _enum_name(status.get("pump_duty")),
                "direction": _enum_name(status.get("direction")),
                "defrost": defrost,
                "outdoor_temperature": status.get("temperature_outdoor"),
                "zone1_water_temperature": zone1.get("temperature"),
                "zone1_target_temperature": (dev.get("water_target") or {}).get("target"),
                "zone1_heat_shift": (dev.get("water_target") or {}).get("shift"),
                "source": "status",
            }
        elif raw:
            zones = raw.get("zoneStatus") or []
            zone1 = next((z for z in zones if isinstance(z, dict) and z.get("zoneId") == 1), {})
            zone0 = zones[0] if zones and isinstance(zones[0], dict) else {}
            row |= {
                "operation_status": _on_off(zone0.get("operationStatus")),
                "pump_duty": _on_off(raw.get("pumpDuty")),
                "outdoor_temperature": raw.get("outdoorNow"),
                "zone1_water_temperature": zone1.get("temperatureNow"),
                "source": "device_list",
            }
        else:
            continue
        if row["outdoor_temperature"] is None:
            # Fallback: latest hourly outdoor value from today's energy data
            items = (dev.get("consumption") or {}).get("day", {}).get("items") or []
            temps = [i.get("outdoor_temp") for i in items if isinstance(i.get("outdoor_temp"), (int, float))]
            if temps:
                row["outdoor_temperature"] = temps[-1]
        day = (dev.get("consumption") or {}).get("day") or {}
        items = day.get("items") or []
        if items and not day.get("error"):
            # Sum of today's hourly values; the cloud updates it in steps (~15 min)
            row["energy_today_kwh"] = round(
                sum(v for i in items if isinstance(v := i.get("total_consumption"), (int, float))), 3
            )
        row |= ha_values
        rows.append(row)
    return rows


def _migrate_header(path: Path, fields: list[str]) -> None:
    """Rewrite an existing CSV whose columns differ from fields (missing values stay empty)."""
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames == fields:
            return
        old_rows = list(reader)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(old_rows)
    tmp.replace(path)


def _existing_ha_fields(path: Path) -> set[str]:
    with path.open(newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh), [])
    return {f for f in header if f.startswith(HA_PREFIX)}


def append_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    ha_fields = {k for row in rows for k in row if k.startswith(HA_PREFIX)}
    if not new_file:
        ha_fields |= _existing_ha_fields(path)
    fields = FIELDS + sorted(ha_fields)
    if not new_file:
        _migrate_header(path, fields)
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerows({k: ("" if v is None else v) for k, v in row.items()} for row in rows)


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def count_rising_edges(rows: list[dict[str, Any]], key: str, on: str = "ON") -> int:
    """Count OFF->ON transitions of a column (rows in chronological order).
    Rows without a value keep the previous state."""
    count, previous = 0, False
    for row in rows:
        value = row.get(key)
        if value in (None, ""):
            continue
        current = str(value).upper() == on
        if current and not previous:
            count += 1
        previous = current
    return count


def read_rows(
    path: Path, since: dt.datetime | None = None, device_id: str | None = None
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                ts = dt.datetime.fromisoformat(row["timestamp"])
            except (KeyError, ValueError):
                continue
            if since and ts < since:
                continue
            if device_id and row.get("device_id") != device_id:
                continue
            for key in NUMERIC_FIELDS:
                row[key] = _to_float(row.get(key))
            for key in [k for k in row if k and k.startswith(HA_PREFIX)]:
                row[key] = _to_float(row[key])
            result.append(row)
    return result
