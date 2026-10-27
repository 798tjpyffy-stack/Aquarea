"""HeishaMon over MQTT: live values of the heat pump and its commands.

HeishaMon publishes every value under <base>/main/<Name> (and <base>/extra/<Name> on
K/L-series units with the extra data block) when it changes, all of them every few
minutes, and its availability as <base>/LWT (Online/Offline). Commands go to
<base>/commands/<Command>. A command may be dropped on the serial link, so the
caller checks the reported value and repeats it if needed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import aiomqtt

_LOGGER = logging.getLogger("aquarea_heishamon")

# HeishaMon republishes all values every 300 s by default; without any message
# for longer the data counts as stale (no history rows, no commands).
STALE_S = 330
RECONNECT_MIN_S = 2
RECONNECT_MAX_S = 60
BASE_TOPIC_RE = re.compile(r"^[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-]+)*$")

OUTLET = "main/Main_Outlet_Temp"
INLET = "main/Main_Inlet_Temp"
TARGET = "main/Main_Target_Temp"
FREQ = "main/Compressor_Freq"
STATE = "main/Heatpump_State"
SHIFT = "main/Z1_Heat_Request_Temp"
DEFROST = "main/Defrosting_State"
OUTDOOR = "main/Outside_Temp"
FLOW = "main/Pump_Flow"
QUIET = "main/Quiet_Mode_Level"
MODE = "main/Operating_Mode_State"
HEATING_MODE = "main/Heating_Mode"  # 0 = compensation curve, 1 = direct
VALVE = "main/ThreeWay_Valve_State"  # 0 = room, 1 = DHW
ERROR = "main/Error"
MODEL = "main/Heat_Pump_Model"
PRESSURE = "main/Water_Pressure"
# electrical consumption / thermal production per mode (W); with the extra data block
# the "main" values are invalid (e.g. -200)
POWER_IN = ("main/Heat_Power_Consumption", "main/Cool_Power_Consumption", "main/DHW_Power_Consumption")
POWER_OUT = ("main/Heat_Power_Production", "main/Cool_Power_Production", "main/DHW_Power_Production")
POWER_IN_EXTRA = (
    "extra/Heat_Power_Consumption_Extra",
    "extra/Cool_Power_Consumption_Extra",
    "extra/DHW_Power_Consumption_Extra",
)
POWER_OUT_EXTRA = (
    "extra/Heat_Power_Production_Extra",
    "extra/Cool_Power_Production_Extra",
    "extra/DHW_Power_Production_Extra",
)
POWER_TOPICS = frozenset(POWER_IN + POWER_OUT + POWER_IN_EXTRA + POWER_OUT_EXTRA)
THERMO_TOPICS = frozenset({OUTLET, INLET, TARGET, FREQ, STATE, SHIFT, DEFROST})

OPERATING_MODES = {
    0: "HEAT",
    1: "COOL",
    2: "AUTO_HEAT",
    3: "DHW",
    4: "HEAT_DHW",
    5: "COOL_DHW",
    6: "AUTO_HEAT_DHW",
    7: "AUTO_COOL",
    8: "AUTO_COOL_DHW",
}
# Z1 shift range in compensation curve mode
SHIFT_MIN, SHIFT_MAX = -5, 5


class CommandError(Exception):
    """A command could not be published."""


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    base_topic: str = "panasonic_heat_pump"


def _parse(payload: bytes) -> Any:
    text = payload.decode("utf-8", errors="replace").strip()
    try:
        value = float(text)
    except ValueError:
        return text
    return int(value) if value.is_integer() and "." not in text else value


def _num(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    v = _num(value)
    return int(v) if v is not None and float(v).is_integer() else None


def _iso(t: float | None) -> str | None:
    return None if t is None else dt.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


class HeishaMon:
    """MQTT client with a value cache; listeners get (topic, value, time) per message."""

    def __init__(self, config: MqttConfig):
        self.config = config
        self.base = config.base_topic
        self.values: dict[str, Any] = {}
        self.updated: dict[str, float] = {}
        self.last_message: float | None = None
        self.connected = False
        self.error: str | None = None
        self.listeners: list[Callable[[str, Any, float], None]] = []
        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ connection
    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
                pass

    async def _run(self) -> None:
        delay = RECONNECT_MIN_S
        while True:
            try:
                async with aiomqtt.Client(
                    self.config.host,
                    port=self.config.port,
                    username=self.config.username,
                    password=self.config.password,
                    identifier=f"aquarea-dashboard-{int(time.time())}",
                ) as client:
                    self._client = client
                    self.connected, self.error = True, None
                    delay = RECONNECT_MIN_S
                    for sub in ("main/#", "extra/#", "LWT"):
                        await client.subscribe(f"{self.base}/{sub}")
                    _LOGGER.warning("Connected to MQTT %s:%s, base topic %s", self.config.host, self.config.port, self.base)
                    async for message in client.messages:
                        self._handle(str(message.topic), message.payload)
            except asyncio.CancelledError:
                raise
            except aiomqtt.MqttError as ex:
                self.error = str(ex) or type(ex).__name__
                _LOGGER.warning("MQTT connection lost/failed (%s); retrying in %s s", self.error, delay)
            except Exception as ex:  # noqa: BLE001 - keep the client alive
                self.error = str(ex) or type(ex).__name__
                _LOGGER.exception("MQTT client error")
            finally:
                self._client = None
                self.connected = False
            await asyncio.sleep(delay)
            delay = min(RECONNECT_MAX_S, delay * 2)

    def _handle(self, topic: str, payload: bytes | bytearray | Any) -> None:
        prefix = f"{self.base}/"
        if not topic.startswith(prefix) or not isinstance(payload, (bytes, bytearray)):
            return
        name = topic[len(prefix):]
        value = _parse(bytes(payload))
        now = time.time()
        self.values[name] = value
        self.updated[name] = now
        if name != "LWT":
            self.last_message = now
        for listener in self.listeners:
            try:
                listener(name, value, now)
            except Exception:  # noqa: BLE001 - a listener must not break the client
                _LOGGER.exception("MQTT listener failed for %s", name)

    # ------------------------------------------------------------------ state
    @property
    def lwt(self) -> str | None:
        v = self.values.get("LWT")
        return v if isinstance(v, str) else None

    def age_s(self, now: float | None = None) -> float | None:
        if self.last_message is None:
            return None
        return max(0.0, (now or time.time()) - self.last_message)

    def fresh(self, now: float | None = None) -> bool:
        """Data usable: connected, HeishaMon not reported offline, recent values."""
        age = self.age_s(now)
        return self.connected and self.lwt != "Offline" and age is not None and age <= STALE_S

    def problem(self) -> str | None:
        """Why the data is not fresh, None if it is."""
        if not self.connected:
            return f"not connected to the MQTT broker ({self.error or 'connecting'})"
        if self.lwt == "Offline":
            return "HeishaMon reports Offline"
        age = self.age_s()
        if age is None:
            return "no data from HeishaMon yet (check the base topic)"
        if age > STALE_S:
            return f"no data from HeishaMon for {int(age // 60)} min"
        return None

    def num(self, name: str) -> float | None:
        return _num(self.values.get(name))

    def power(self) -> tuple[float | None, float | None]:
        """Electrical input and thermal output (W), all modes together."""
        use_extra = any(t in self.values for t in POWER_IN_EXTRA)

        def total(topics: tuple[str, ...]) -> float | None:
            vals = [v for t in topics if (v := self.num(t)) is not None and v >= 0]
            return float(sum(vals)) if vals else None

        if use_extra:
            return total(POWER_IN_EXTRA), total(POWER_OUT_EXTRA)
        return total(POWER_IN), total(POWER_OUT)

    def thermo_values(self) -> dict[str, Any]:
        state = self.num(STATE)
        return {
            "outlet": self.num(OUTLET),
            "inlet": self.num(INLET),
            "target": self.num(TARGET),
            "freq": self.num(FREQ),
            "defrost": self.num(DEFROST) == 1,
            "unit_on": None if state is None else state == 1,
            "shift": self.num(SHIFT),
        }

    # ------------------------------------------------------------------ commands
    async def send(self, command: str, value: int) -> None:
        if not re.fullmatch(r"Set[A-Za-z0-9]+", command):
            raise CommandError(f"invalid command {command!r}")
        problem = self.problem()
        if problem is not None:
            raise CommandError(problem)
        client = self._client
        if client is None:
            raise CommandError("not connected to the MQTT broker")
        try:
            await client.publish(f"{self.base}/commands/{command}", str(int(value)), qos=1)
        except aiomqtt.MqttError as ex:
            raise CommandError(f"MQTT publish failed: {ex}") from ex

    # ------------------------------------------------------------------ report
    def device_report(self, device_id: str, name: str) -> dict[str, Any]:
        """The device in the report shape of the dashboard (info / status / status_extra)."""
        v = self.num
        p_in, p_out = self.power()
        problem = self.problem()
        live = {
            "online": problem is None,
            "problem": problem,
            "connected": self.connected,
            "lwt": self.lwt,
            "last_message": _iso(self.last_message),
            "age_s": None if (age := self.age_s()) is None else round(age),
            "base_topic": self.base,
            "model": self.values.get(MODEL),
            "compressor_freq": v(FREQ),
            "inlet": v(INLET),
            "outlet": v(OUTLET),
            "target": v(TARGET),
            "flow": v(FLOW),
            "power_in_w": p_in,
            "power_out_w": p_out,
            "operations_hours": v("main/Operations_Hours"),
            "operations_counter": v("main/Operations_Counter"),
            "values": dict(sorted(self.values.items())),
        }
        dev: dict[str, Any] = {"info": {"device_id": device_id, "name": name, "model": self.values.get(MODEL)}, "heishamon": live}
        if self.last_message is None:
            dev["status"] = {"error": problem or "no data"}
            return dev
        on = v(STATE) == 1
        curve = v(HEATING_MODE) == 0
        shift = _int(self.values.get(SHIFT))
        mode = _int(self.values.get(MODE))
        quiet = _int(self.values.get(QUIET))
        flow = v(FLOW)
        error = self.values.get(ERROR)
        faults = [] if error in (None, "", "No error") else [{"error_code": str(error), "error_message": None}]
        dev["status"] = {
            "operation_mode": {"name": OPERATING_MODES.get(mode, str(mode)) if mode is not None else None, "value": mode},
            "operation_status": {"name": "ON" if on else "OFF"},
            "direction": {"name": "WATER" if v(VALVE) == 1 else "PUMP"},
            "pump_duty": {"name": None if flow is None else "ON" if flow > 0 else "OFF"},
            "quiet_mode": {"name": "OFF" if quiet == 0 else f"LEVEL{quiet}", "value": quiet},
            "temperature_outdoor": v(OUTDOOR),
            "zones": [
                {
                    "zone_id": 1,
                    "name": "Zone 1",
                    "operation_status": {"name": "ON" if on else "OFF"},
                    "temperature": v(OUTLET),
                    "heat_set": shift,
                    # only the curve shift is supported for control
                    "heat_min": SHIFT_MIN if curve else None,
                    "heat_max": SHIFT_MAX if curve else None,
                }
            ],
            "fault_status": faults,
        }
        dev["status_extra"] = {
            "water_pressure": v(PRESSURE),
            "defrost": _int(self.values.get(DEFROST)),
            "internal_heater": _int(self.values.get("main/Internal_Heater_State")),
            "external_heater": _int(self.values.get("main/External_Heater_State")),
            "force_heater": _int(self.values.get("main/Force_Heater_State")),
        }
        target = v(TARGET)
        dev["water_target"] = {
            "outdoor": v(OUTDOOR),
            "curve": round(target - shift, 1) if curve and target is not None and shift is not None else None,
            "shift": shift if curve else None,
            "target": target,
        }
        return dev
