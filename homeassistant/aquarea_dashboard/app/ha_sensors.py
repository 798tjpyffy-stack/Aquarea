"""Read temperature sensors from Home Assistant via its REST API.

Inside the Home Assistant app the Supervisor provides SUPERVISOR_TOKEN and the
proxy URL http://supervisor/core. Locally, set HA_URL (e.g. your Nabu Casa
remote URL) and HA_TOKEN (a long-lived access token) in the environment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp

_LOGGER = logging.getLogger("aquarea_web.ha")

SUPERVISOR_URL = "http://supervisor/core"
ENTITY_ID = re.compile(r"^sensor\.[a-z0-9_]+$")
MAX_AUTO_SENSORS = 12
TIMEOUT = aiohttp.ClientTimeout(total=15)
# entity_id -> area name (None if unassigned/unknown); areas rarely change, so
# they are looked up once per entity until restart.
_AREAS: dict[str, str | None] = {}


@dataclass(frozen=True)
class HAConfig:
    url: str
    token: str = field(repr=False)
    entity_ids: tuple[str, ...] = ()

    @classmethod
    def from_env(
        cls, url: str | None, entity_ids: list[str], token_file: Path | None = None
    ) -> HAConfig | None:
        """HA_URL + token (file or HA_TOKEN, local) take precedence over the Supervisor token."""
        for eid in entity_ids:
            if not ENTITY_ID.match(eid):
                raise ValueError(f"invalid sensor entity id {eid!r} (expected sensor.xyz)")
        url = url or os.environ.get("HA_URL")
        token = os.environ.get("HA_TOKEN")
        if token_file:
            try:
                token = token_file.read_text(encoding="utf-8").strip()
            except OSError as ex:
                raise ValueError(f"cannot read token file {token_file}: {ex.strerror}") from None
            if not token:
                raise ValueError(f"token file {token_file} is empty")
            if token_file.stat().st_mode & 0o077:
                _LOGGER.warning("Token file %s is readable by other users; run: chmod 600 %s", token_file, token_file)
        if url and token:
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError("HA_URL must be an http(s) URL")
            if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "homeassistant.local"):
                _LOGGER.warning("HA_URL uses plain HTTP: the access token is sent unencrypted")
            return cls(url.rstrip("/"), token, tuple(entity_ids))
        if url or token:
            raise ValueError("set both the Home Assistant URL and a token (HA_TOKEN or --ha-token-file)")
        supervisor = os.environ.get("SUPERVISOR_TOKEN")
        if supervisor:
            return cls(SUPERVISOR_URL, supervisor, tuple(entity_ids))
        return None


def _is_temperature(state: dict[str, Any]) -> bool:
    attrs = state.get("attributes") or {}
    return attrs.get("device_class") == "temperature" or attrs.get("unit_of_measurement") in ("°C", "°F")


def _sensor(state: dict[str, Any]) -> dict[str, Any]:
    attrs = state.get("attributes") or {}
    try:
        value = float(state.get("state"))
    except (TypeError, ValueError):
        value = None  # "unavailable", "unknown", ...
    area = _AREAS.get(state.get("entity_id"))
    return {
        "entity_id": state.get("entity_id"),
        "name": area or attrs.get("friendly_name") or state.get("entity_id"),
        "area": area,
        "friendly_name": attrs.get("friendly_name"),
        "value": value,
        "state": state.get("state"),
        "unit": attrs.get("unit_of_measurement"),
        "last_changed": state.get("last_changed"),
    }


async def _load_areas(http: aiohttp.ClientSession, config: HAConfig, entity_ids: list[str]) -> None:
    """Look up area names for entities not cached yet (one template request)."""
    missing = [eid for eid in entity_ids if eid not in _AREAS and ENTITY_ID.match(eid)]
    if not missing:
        return
    # Entity IDs are validated by ENTITY_ID, so they are safe inside the template.
    template = "{{ " + json.dumps(missing) + " | map('area_name') | list | tojson }}"
    areas: list[Any] = []
    try:
        async with http.post(
            f"{config.url}/api/template",
            headers={"Authorization": f"Bearer {config.token}"},
            json={"template": template},
            timeout=TIMEOUT,
        ) as response:
            if response.status == 200:
                areas = json.loads(await response.text())
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as ex:
        _LOGGER.info("Area lookup failed: %s", type(ex).__name__)
        return  # retry on the next poll
    for eid, area in zip(missing, areas if isinstance(areas, list) else []):
        _AREAS[eid] = area if isinstance(area, str) and area else None
    for eid in missing:
        _AREAS.setdefault(eid, None)


async def fetch_sensors(http: aiohttp.ClientSession, config: HAConfig) -> dict[str, Any]:
    """Current temperature sensors: the configured ones, or all (capped) if none are configured."""
    try:
        async with http.get(
            f"{config.url}/api/states",
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=TIMEOUT,
        ) as response:
            if response.status in (401, 403):
                return {"error": f"Home Assistant rejected the access token (HTTP {response.status})"}
            response.raise_for_status()
            states = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as ex:
        _LOGGER.warning("Home Assistant sensors unavailable: %s", type(ex).__name__)
        return {"error": f"Home Assistant not reachable ({type(ex).__name__})"}
    if not isinstance(states, list):
        return {"error": "Unexpected response from Home Assistant"}

    by_id = {s.get("entity_id"): s for s in states if isinstance(s, dict)}
    if config.entity_ids:
        await _load_areas(http, config, [eid for eid in config.entity_ids if eid in by_id])
        sensors = [
            _sensor(by_id[eid]) if eid in by_id else {"entity_id": eid, "name": eid, "value": None, "state": "not found"}
            for eid in config.entity_ids
        ]
    else:
        auto = sorted(eid for eid, s in by_id.items() if isinstance(eid, str) and ENTITY_ID.match(eid) and _is_temperature(s))
        await _load_areas(http, config, auto[:MAX_AUTO_SENSORS])
        sensors = [_sensor(by_id[eid]) for eid in auto[:MAX_AUTO_SENSORS]]
    return {"sensors": sensors, "auto": not config.entity_ids}
