"use strict";

const $ = (id) => document.getElementById(id);
let lastReport = null;
let allowControl = false;
let pollMinutes = 10;
let autoTimer = null;
let historyHours = 24;
let currentUser = null;
let tokenExpiration = null;

// ------------------------------------------------------------------ helpers
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

const isEnum = (v) =>
  v && typeof v === "object" && !Array.isArray(v) &&
  Object.keys(v).length === 2 && "name" in v && "value" in v;

const isLeaf = (v) => v === null || typeof v !== "object" || isEnum(v);

const TEMP_KEY = /temp|^(heat|cool)_?(max|min|set)$|^(eco|comfort)_?(heat|cool)$|^(heat|cool)$/i;
const ENERGY_KEY = /consumption$/i;

function fmt(key, v) {
  if (v === null || v === undefined || v === "") return "–";
  if (isEnum(v)) return v.name;
  if (typeof v === "number") {
    if (ENERGY_KEY.test(key)) return `${v} kWh`;
    if (TEMP_KEY.test(key)) return `${v} °C`;
  }
  return String(v);
}

function kvTable(obj) {
  const table = el("table", { class: "kv" });
  const tbody = el("tbody");
  table.append(tbody);

  const entries = Array.isArray(obj) ? obj.map((v, i) => [`[${i}]`, v]) : Object.entries(obj);
  for (const [key, value] of entries) {
    let cell;
    if (isLeaf(value)) cell = fmt(key, value);
    else if (Array.isArray(value) && value.length === 0) cell = "–";
    else if (Array.isArray(value) && value.every(isLeaf)) cell = value.map((v) => fmt(key, v)).join(", ");
    else if (!Array.isArray(value) && Object.keys(value).length === 0) cell = "–";
    else cell = kvTable(value);
    tbody.append(el("tr", {}, el("td", { text: key }), el("td", {}, cell)));
  }
  return table;
}

function section(title, content, open = false) {
  const details = el("details", { class: "card" }, el("summary", { text: title }), content);
  if (open) details.open = true;
  return details;
}

function tile(label, value, sub, warn = false) {
  return el("div", { class: warn ? "tile warn" : "tile" },
    el("div", { class: "label", text: label }),
    el("div", { class: "value", text: value }),
    sub ? el("div", { class: "sub", text: sub }) : null);
}

const num = (v) => (typeof v === "number" ? v : null);
const round = (v, d = 2) => (v == null ? "–" : String(Math.round(v * 10 ** d) / 10 ** d));

// ------------------------------------------------------------------ overview
function overviewTiles(dev) {
  const status = dev.status && !dev.status.error ? dev.status : null;
  const raw = dev.raw_device_list_entry && !dev.raw_device_list_entry.error ? dev.raw_device_list_entry : null;
  const tiles = el("div", { class: "tiles" });

  const zone0Status = status?.zones?.[0]?.operation_status?.name;
  const zone0Raw = raw?.zoneStatus?.[0]?.operationStatus;
  const operation = zone0Status ?? (typeof zone0Raw === "number" ? (zone0Raw ? "ON" : "OFF") : "–");
  const mode = status?.operation_mode?.name ?? dev.info?.mode?.name ?? "–";
  const direction = status?.direction?.name ?? "–";
  tiles.append(tile("Operation", operation, `Mode: ${mode} · Direction: ${direction}`));
  if (allowControl && status) tiles.lastChild.append(powerControl(dev.info.device_id, operation === "ON"));

  let outdoor = num(status?.temperature_outdoor);
  let outdoorSrc = "live status";
  if (outdoor == null && raw) { outdoor = num(raw.outdoorNow); outdoorSrc = "device list"; }
  if (outdoor == null) {
    const items = dev.consumption?.day?.items ?? [];
    const last = [...items].reverse().find((i) => num(i.outdoor_temp) != null);
    if (last) { outdoor = last.outdoor_temp; outdoorSrc = `energy data ${last.data_time}`; }
  }
  tiles.append(tile("Outdoor", outdoor == null ? "–" : `${outdoor} °C`, outdoor == null ? null : outdoorSrc));

  const quiet = status?.quiet_mode?.value ?? raw?.quietMode;
  if (typeof quiet === "number") {
    tiles.append(tile("Quiet mode", quietLabel(quiet), "Level 3 = quietest"));
    if (allowControl) tiles.lastChild.append(quietModeControl(dev.info.device_id, quiet));
    if (dev.quiet_auto) tiles.lastChild.append(quietAutoView(dev));
  }

  const zones = status?.zones?.length
    ? status.zones.map((z) => ({ id: z.zone_id, t: z.temperature, set: z.heat_set, min: z.heat_min, max: z.heat_max, on: z.operation_status?.name, src: "live status" }))
    : (raw?.zoneStatus ?? []).map((z) => ({ id: z.zoneId, t: z.temperatureNow, set: z.heatSet, min: z.heatMin, max: z.heatMax, on: z.operationStatus, src: "device list" }));
  for (const z of zones) {
    const on = z.on === undefined ? "" : ` · ${typeof z.on === "number" ? (z.on ? "ON" : "OFF") : z.on}`;
    const t = tile(`Zone ${z.id} water`, num(z.t) == null ? "–" : `${z.t} °C`, `Set/shift: ${fmt("heat_set", z.set)}${on} · ${z.src}`);
    if (allowControl) t.append(heatTargetControl(dev.info.device_id, z));
    tiles.append(t);
  }

  const wt = dev.water_target;
  if (wt && num(wt.target) != null) {
    const sign = (v) => (v > 0 ? `+${v}` : String(v));
    tiles.append(tile("Zone 1 target water", `${wt.target} °C`, wt.curve == null
      ? "Direct setpoint"
      : `Curve ${wt.curve} °C (${wt.curve_exact}) · shift ${sign(wt.shift)} · at ${wt.outdoor} °C outdoor`));
  }

  const tank = status?.tank_status?.[0]
    ? { t: status.tank_status[0].temperature, set: status.tank_status[0].heat_set, src: "live status" }
    : raw?.tankStatus && Object.keys(raw.tankStatus).length
      ? { t: raw.tankStatus.temperatureNow, set: raw.tankStatus.heatSet, src: "device list" }
      : null;
  if (tank) tiles.append(tile("Hot water tank", num(tank.t) == null ? "–" : `${tank.t} °C`, `Target: ${fmt("heat_set", tank.set)} · ${tank.src}`));

  const faults = status?.fault_status ?? [];
  tiles.append(tile("Errors", faults.length ? faults.map((f) => f.error_code).join(", ") : status ? "None" : "–",
    faults[0]?.error_message ?? null, faults.length > 0));

  const extra = dev.status_extra;
  if (extra) {
    const onOff = (v) => (typeof v === "number" ? (v ? "ON" : "OFF") : "–");
    tiles.append(tile("Water pressure", num(extra.water_pressure) == null ? "–" : `${extra.water_pressure} bar`));
    tiles.append(tile("Defrost", onOff(extra.defrost),
      typeof dev.defrost_cycles_today === "number" ? `Cycles today: ${dev.defrost_cycles_today}` : null));
    tiles.append(tile("Backup heat", onOff(extra.bivalent_active),
      `Force heater: ${onOff(extra.force_heater)} · External heater: ${onOff(extra.external_heater)}`));
  }

  const comp = dev.compressor_today;
  if (comp) {
    const perStart = comp.hours_per_start == null ? "–"
      : `${comp.hours_per_start} h (${Math.round(comp.hours_per_start * 60)} min) per start`;
    const today = `Today: ${comp.starts} start${comp.starts === 1 ? "" : "s"} · running ${comp.hours} h · ${perStart}`;
    const running = comp.state === "ON";
    if (comp.state === "ON" || comp.state === "OFF") {
      const t = tile("Compressor (est.)", running ? "Running" : "Off", compressorRunText(comp));
      t.classList.add(running ? "comp-on" : "comp-off");
      t.append(el("div", { class: "sub", text: today }));
      tiles.append(t);
    } else {
      tiles.append(tile("Compressor today (est.)", `${comp.starts} start${comp.starts === 1 ? "" : "s"}`,
        `Running ${comp.hours} h · ${perStart}`));
    }
  }

  const dayItems = dev.consumption?.day?.items ?? [];
  if (dayItems.length) {
    const total = dayItems.reduce((s, i) => s + (num(i.total_consumption) ?? 0), 0);
    const cost = dayItems.reduce((s, i) => s + (num(i.heat_cost) ?? 0) + (num(i.cool_cost) ?? 0) + (num(i.tank_cost) ?? 0), 0);
    tiles.append(tile("Energy today", `${round(total)} kWh`, `Cost: ${round(cost)}`));
  }
  return tiles;
}

// ------------------------------------------------------------------ control
function powerControl(deviceId, isOn) {
  const btn = el("button", { type: "button", text: isOn ? "Turn heating off" : "Turn heating on" });
  btn.addEventListener("click", async () => {
    if (!confirm(`Turn the heating ${isOn ? "OFF" : "ON"}?`)) return;
    btn.disabled = true;
    btn.textContent = "Sending…";
    try {
      await postJSON("api/device/power", { device_id: deviceId, on: !isOn });
      await loadReport();
    } catch (e) {
      if (e.status === 401) return showLogin(e.message);
      showAppError(e.message);
      btn.disabled = false;
      btn.textContent = isOn ? "Turn heating off" : "Turn heating on";
    }
  });
  return el("div", { class: "control" }, el("div", { class: "control-row" }, btn));
}
// "Off for 1 h 5 min (since 12:30) · last run 42 min" / "Running for 20 min (started 12:30)";
// "more than" if the run reaches back to a data gap.
function compressorRunText(comp) {
  if (!comp.state || !comp.since) return null;
  const duration = (min) => (min >= 60 ? `${Math.floor(min / 60)} h ${min % 60} min` : `${min} min`);
  const since = new Date(comp.since);
  const dur = duration(Math.max(0, Math.round((Date.now() - since) / 60_000)));
  const clock = since.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (comp.state === "ON") {
    return comp.since_exact ? `Running for ${dur} (started ${clock})` : `Running for more than ${dur}`;
  }
  const off = comp.since_exact ? `Off for ${dur} (since ${clock})` : `Off for more than ${dur}`;
  if (comp.last_run_min == null) return off;
  return `${off} · last run ${comp.last_run_exact ? "" : "more than "}${duration(comp.last_run_min)}`;
}

function quietLabel(level) {
  return level === 0 ? "Off" : `Level ${level}`;
}
function quietModeControl(deviceId, current) {
  const select = el("select", { "aria-label": "Quiet mode" },
    [0, 1, 2, 3].map((l) => el("option", { value: String(l), text: quietLabel(l) })));
  select.value = String(current);
  const btn = el("button", { type: "button", text: "Set" });
  btn.addEventListener("click", async () => {
    const level = Number(select.value);
    if (level === current) return;
    if (!confirm(`Change quiet mode from ${quietLabel(current)} to ${quietLabel(level)}?`)) return;
    btn.disabled = true;
    btn.textContent = "Sending…";
    try {
      await postJSON("api/device/quiet-mode", { device_id: deviceId, level });
      await loadReport();
    } catch (e) {
      if (e.status === 401) return showLogin(e.message);
      showAppError(e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "Set";
    }
  });
  return el("div", { class: "control" }, el("div", { class: "control-row" }, select, btn));
}

// Cached per device so that open settings and unsaved input survive re-renders.
const quietAutoViews = new Map();
function quietAutoView(dev) {
  const deviceId = dev.info.device_id;
  let view = quietAutoViews.get(deviceId);
  if (!view) {
    view = buildQuietAutoView(deviceId);
    quietAutoViews.set(deviceId, view);
  }
  view.update(dev.quiet_auto);
  return view.node;
}

function buildQuietAutoView(deviceId) {
  const numberInput = (min, max, step) => el("input", { type: "number", min, max, step });
  const f = {
    enabled: el("input", { type: "checkbox" }),
    threshold: numberInput("-20", "20", "0.5"),
    advanced: el("input", { type: "checkbox" }),
    release_k: numberInput("0", "10", "0.5"),
    defrost: el("input", { type: "checkbox" }),
    defrost_until: numberInput("-20", "20", "0.5"),
  };
  const check = (input, text) => el("label", { class: "check" }, input, el("span", { text }));
  const field = (text, input) => el("label", {}, el("span", { text }), input);
  const save = el("button", { type: "button", text: "Save" });
  const status = el("div", { class: "sub" });
  const form = el("div", { class: "quiet-form" },
    check(f.enabled, "Auto"),
    field("Threshold outdoor °C (below: 0, above: 3)", f.threshold),
    check(f.advanced, "Advanced: 3 after each compressor stop"),
    field("Back to 0 at water ≥ target − K", f.release_k),
    check(f.defrost, "Quiet 0 after a defrost"),
    field("… until outdoor ≥ °C", f.defrost_until),
    allowControl ? save : null);
  const details = el("details", {}, el("summary", { text: "Auto settings" }), form);
  const node = el("div", { class: "control" }, status, details);
  let dirty = false, settings = null;
  for (const input of Object.values(f)) {
    input.disabled = !allowControl;
    input.addEventListener("input", () => { dirty = true; });
    input.addEventListener("change", () => { dirty = true; });
  }

  function setValues(s) {
    settings = s;
    for (const k of ["enabled", "advanced", "defrost"]) f[k].checked = s[k];
    for (const k of ["threshold", "release_k", "defrost_until"]) f[k].value = String(s[k]);
    dirty = false;
  }

  function render(q) {
    const st = q.state ?? {}, live = q.live ?? {};
    const text = q.settings.enabled
      ? `Auto: ${live.note ?? "on, applied at the next update (within a minute)"}`
      : st.paused ? `Auto paused: ${st.paused}` : "Auto: off";
    status.textContent = text;
  }

  save.addEventListener("click", async () => {
    const body = {
      enabled: f.enabled.checked,
      threshold: Number(f.threshold.value),
      advanced: f.advanced.checked,
      release_k: Number(f.release_k.value),
      defrost: f.defrost.checked,
      defrost_until: Number(f.defrost_until.value),
    };
    if (body.enabled && !settings?.enabled &&
        !confirm("Enable quiet mode auto? The dashboard will change the quiet mode on its own.")) return;
    save.disabled = true;
    save.textContent = "Saving…";
    try {
      const res = await postJSON("api/quiet-auto", { device_id: deviceId, settings: body });
      const dev = lastReport?.devices?.find((d) => d.info?.device_id === deviceId);
      if (dev) dev.quiet_auto = res.quiet_auto;
      setValues(res.quiet_auto.settings);
      render(res.quiet_auto);
    } catch (e) {
      if (e.status === 401) return showLogin(e.message);
      showAppError(e.message);
    } finally {
      save.disabled = false;
      save.textContent = "Save";
    }
  });

  return {
    node,
    update(q) {
      if (!dirty) setValues(q.settings);
      render(q);
    },
  };
}
function heatTargetControl(deviceId, zone) {
  const min = num(zone.min);
  const max = num(zone.max);
  if (min == null || max == null) {
    return el("div", { class: "sub", text: "Heating target: range unknown, control unavailable" });
  }
  // Small ranges around 0 mean the zone runs on a heating curve: the value is a shift.
  const isShift = min < 0 && max <= 10;
  const label = isShift ? `Curve shift (${min}…${max})` : `Heating target °C (${min}…${max})`;

  const input = el("input", { type: "number", min: String(min), max: String(max), step: "1", "aria-label": label });
  input.value = num(zone.set) ?? (isShift ? 0 : min);
  const btn = el("button", { type: "button", text: "Set" });

  btn.addEventListener("click", async () => {
    const value = Number(input.value);
    if (!Number.isInteger(value) || value < min || value > max) {
      showAppError(`Value must be a whole number between ${min} and ${max}.`);
      return;
    }
    const current = num(zone.set);
    const what = isShift ? "heating curve shift" : "heating target";
    if (!confirm(`Change zone ${zone.id} ${what} from ${current ?? "?"} to ${value}?`)) return;

    btn.disabled = true;
    btn.textContent = "Sending…";
    try {
      await postJSON("api/zone/heat-target", { device_id: deviceId, zone_id: zone.id, value });
      await loadReport();
    } catch (e) {
      if (e.status === 401) return showLogin(e.message);
      showAppError(e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "Set";
    }
  });

  return el("div", { class: "control" }, el("div", { class: "sub", text: label }), el("div", { class: "control-row" }, input, btn));
}

function showAppError(message) {
  $("app-error").textContent = message;
  $("app-error").hidden = false;
}

// ------------------------------------------------------------------ energy
function shortLabel(t) {
  if (!t) return "";
  const s = String(t).trim();
  return s.includes(" ") ? s.split(" ").pop() : s.slice(-2);
}

function energyView(consumption) {
  const wrap = el("div");
  const tabs = el("div", { class: "tabs" });
  const body = el("div");
  const views = [["day", "Today (hourly)"], ["month", "This month (daily)"], ["year", "This year (monthly)"]];

  function show(key) {
    for (const b of tabs.children) b.classList.toggle("active", b.dataset.key === key);
    body.replaceChildren(energyBlock(consumption[key]));
  }
  for (const [key, label] of views) {
    const b = el("button", { type: "button", text: label });
    b.dataset.key = key;
    b.addEventListener("click", () => show(key));
    tabs.append(b);
  }
  wrap.append(tabs, body);
  show("day");
  return wrap;
}

function energyBlock(block) {
  if (!block) return el("p", { class: "muted", text: "No data requested." });
  if (block.error) return el("p", { class: "error", text: block.error });
  const items = block.items ?? [];
  if (!items.length) return el("p", { class: "muted", text: `No data for ${block.date}.` });

  const max = Math.max(...items.map((i) => (num(i.heat_consumption) ?? 0) + (num(i.cool_consumption) ?? 0) + (num(i.tank_consumption) ?? 0)), 0.0001);
  const chart = el("div", { class: "chart" });
  const labels = el("div", { class: "chart-labels" });
  for (const i of items) {
    const bar = el("div", { class: "bar", title: `${i.data_time}: ${round(i.total_consumption, 3)} kWh` });
    for (const [cls, v] of [["heat", i.heat_consumption], ["cool", i.cool_consumption], ["tank", i.tank_consumption]]) {
      const seg = el("span", { class: cls });
      seg.style.height = `${((num(v) ?? 0) / max) * 100}%`;
      bar.append(seg);
    }
    chart.append(bar);
    labels.append(el("span", { text: shortLabel(i.data_time) }));
  }
  const legend = el("div", { class: "legend" },
    el("span", {}, el("i", { class: "heat" }), "Heating"),
    el("span", {}, el("i", { class: "cool" }), "Cooling"),
    el("span", {}, el("i", { class: "tank" }), "Hot water"));

  const sum = (k) => items.reduce((s, i) => s + (num(i[k]) ?? 0), 0);
  const head = el("tr", {}, ...["Time", "Heat kWh", "Cool kWh", "Tank kWh", "Total kWh", "Cost", "Outdoor °C"]
    .map((h, idx) => el("th", { class: idx ? "num" : "", text: h })));
  const rows = items.map((i) => el("tr", {},
    el("td", { text: i.data_time ?? "–" }),
    el("td", { class: "num", text: round(i.heat_consumption, 3) }),
    el("td", { class: "num", text: round(i.cool_consumption, 3) }),
    el("td", { class: "num", text: round(i.tank_consumption, 3) }),
    el("td", { class: "num", text: round(i.total_consumption, 3) }),
    el("td", { class: "num", text: round((num(i.heat_cost) ?? 0) + (num(i.cool_cost) ?? 0) + (num(i.tank_cost) ?? 0)) }),
    el("td", { class: "num", text: i.outdoor_temp ?? "–" })));
  const totals = el("tr", {},
    el("th", { text: "Sum" }),
    el("th", { class: "num", text: round(sum("heat_consumption"), 3) }),
    el("th", { class: "num", text: round(sum("cool_consumption"), 3) }),
    el("th", { class: "num", text: round(sum("tank_consumption"), 3) }),
    el("th", { class: "num", text: round(sum("total_consumption"), 3) }),
    el("th", { class: "num", text: round(sum("heat_cost") + sum("cool_cost") + sum("tank_cost")) }),
    el("th"));

  return el("div", {}, legend, chart, labels,
    el("table", {}, el("thead", {}, head), el("tbody", {}, rows), el("tfoot", {}, totals)));
}

// ------------------------------------------------------------------ history
const SVG_NS = "http://www.w3.org/2000/svg";

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") node.textContent = v;
    else node.setAttribute(k, String(v));
  }
  for (const c of children.flat()) if (c) node.append(c);
  return node;
}

function historyView(deviceId) {
  const ranges = [[6, "6 h"], [12, "12 h"], [24, "24 h"], [24 * 7, "7 days"], [24 * 30, "30 days"], [0, "All"]];
  const tabs = el("div", { class: "tabs" });
  const body = el("div", {}, el("p", { class: "muted", text: "Loading history…" }));

  async function load(hours) {
    historyHours = hours;
    for (const b of tabs.children) b.classList.toggle("active", Number(b.dataset.hours) === hours);
    try {
      const params = new URLSearchParams({ hours: String(hours), device_id: deviceId });
      const { rows } = await api(`api/history?${params}`);
      body.replaceChildren(historyChart(rows));
    } catch (e) {
      body.replaceChildren(el("p", { class: "error", text: e.message }));
    }
  }
  for (const [hours, label] of ranges) {
    const b = el("button", { type: "button", text: label });
    b.dataset.hours = String(hours);
    b.addEventListener("click", () => load(hours));
    tabs.append(b);
  }
  load(historyHours);
  return el("div", {}, tabs, body);
}

const POWER_COLOR = "#b7950b";
const HA_COLORS = ["#27ae60", "#c0392b", "#16a085", "#d35400", "#2c3e50", "#e84393", "#7f8c8d", "#6c5ce7"];
// The server (compressor.py) estimates the compressor state per row: fields
// compressor, compressor_why, near_thermo_off. These constants mirror it for the legend text.
const COMPRESSOR_ON_KW = 0.2;
const COMPRESSOR_OFF_KW = 0.08;
const WATER_STEP_K = 1;
const THERMO_OFF_K = 3;
const THERMO_MS = 3 * 60_000;
const TARGET_UPDATE_MS = 60 * 60_000;
const POWER_LAG_MIN = 15;
const SHIFT_UP_COLOR = "#e74c3c";
const SHIFT_DOWN_COLOR = "#3498db";

function historyChart(rows) {
  const pts = rows
    .map((r) => ({ ...r, t: Date.parse(r.timestamp) }))
    .filter((r) => !Number.isNaN(r.t))
    .sort((a, b) => a.t - b.t);
  if (!pts.length) {
    return el("p", { class: "muted", text: `No history yet. A data point is recorded on every update (every ${pollMinutes} min).` });
  }

  const narrow = window.matchMedia("(max-width: 600px)").matches;
  // Home Assistant sensors (columns "ha:<entity_id>", e.g. indoor) get their own scale on the right
  const haKeys = [...new Set(pts.flatMap((p) => Object.keys(p).filter((k) => k.startsWith("ha:") && typeof p[k] === "number")))].sort();
  const W = narrow ? 340 : 900, L = narrow ? 60 : 48, R = haKeys.length ? (narrow ? 40 : 48) : 16, T = 12, plotH = narrow ? 160 : 220, bandH = 14, bandGap = 6;
  const xTicks = narrow ? 3 : 6;
  // Average power between consecutive changes of today's kWh total (the cloud
  // updates it in steps); the first step after a start/reset has no known start.
  const power = [];
  let prevE = null, lastChange = null;
  for (const p of pts) {
    const e = p.energy_today_kwh;
    if (typeof e !== "number") continue;
    if (prevE !== null && e < prevE.energy_today_kwh) {
      lastChange = null; // new day
    } else if (prevE !== null && e !== prevE.energy_today_kwh) {
      if (lastChange) power.push({ from: lastChange.t, to: p.t, kw: (e - prevE.energy_today_kwh) / ((p.t - lastChange.t) / 3_600_000) });
      lastChange = p;
    }
    prevE = p;
  }
  const hasEnergy = prevE !== null;
  const powerAt = (t) => power.find((s) => t > s.from && t <= s.to);
  const powerH = hasEnergy ? (narrow ? 60 : 80) : 0;
  const powerTop = T + plotH + 30;
  const bandsTop = hasEnergy ? powerTop + powerH + 14 : T + plotH + 26;
  const series = [
    { key: "zone1_water_temperature", label: "Zone 1 water", color: "#e4572e" },
    { key: "outdoor_temperature", label: "Outdoor", color: "#2e86de" },
    { key: "zone1_target_temperature", label: "Zone 1 target", color: "#e4572e", dash: "6 4" },
  ];
  // named from the latest report
  const haNames = Object.fromEntries((lastReport?.ha_sensors?.sensors ?? []).map((s) => [s.entity_id, s.name]));
  haKeys.forEach((key, i) => {
    const id = key.slice(3);
    const label = haNames[id] ?? id;
    series.push({ key, label, legend: `${label} (right axis)`, color: HA_COLORS[i % HA_COLORS.length], right: true });
  });
  const bands = [
    { key: "compressor", label: "Comp.*", legend: "Compressor (estimated)", color: "#c0392b", colors: { ON: "#c0392b" } },
    { key: "near_thermo_off", label: "Th.off*", legend: `Near thermo off (water ≥ target + ${THERMO_OFF_K - 1} K / + ${THERMO_OFF_K} K)`, color: "#e67e22", colors: { NEAR: "#f5b041", ABOVE: "#d35400" } },
    { key: "zone1_heat_shift", label: "Shift", legend: "Curve shift (red > 0, blue < 0)", color: SHIFT_UP_COLOR,
      colorOf: (v) => (typeof v !== "number" || v === 0 ? null : v > 0 ? SHIFT_UP_COLOR : SHIFT_DOWN_COLOR) },
    { key: "operation_status", label: "Operation", color: "#17a589", colors: { ON: "#17a589" } },
    { key: "pump_duty", label: "Pump", color: "#8e44ad", colors: { ON: "#8e44ad" } },
    { key: "direction", label: "Direction", legend: "Direction (heating / water)", color: "#f39c12", colors: { PUMP: "#f39c12", WATER: "#2e86de" } },
    { key: "defrost", label: "Defrost", color: "#5dade2", colors: { ON: "#5dade2" } },
  ];
  const H = bandsTop + bands.length * (bandH + bandGap) + 4;

  const valuesOf = (right) => pts.flatMap((p) => series.filter((s) => !!s.right === right).map((s) => p[s.key]))
    .filter((v) => typeof v === "number");
  const values = valuesOf(false);
  const yMin = values.length ? Math.floor(Math.min(...values) - 1) : 0;
  const yMax = values.length ? Math.ceil(Math.max(...values) + 1) : 1;
  // right scale: smallest step that fits in at most 6 intervals, at least 2
  const rValues = valuesOf(true);
  const rLo = rValues.length ? Math.min(...rValues) : 0, rHi = rValues.length ? Math.max(...rValues) : 1;
  const rStep = [0.1, 0.2, 0.5, 1, 2, 5, 10].find((s) => Math.ceil(rHi / s - 1e-9) - Math.floor(rLo / s + 1e-9) <= 6) ?? 10;
  let rMin = Math.floor(rLo / rStep + 1e-9) * rStep, rMax = Math.ceil(rHi / rStep - 1e-9) * rStep;
  if (rMax - rMin < rStep / 2) [rMin, rMax] = [rMin - rStep, rMax + rStep];
  else if (rMax - rMin < 1.5 * rStep) rMax += rStep;
  const t0 = pts[0].t;
  const t1 = pts.length > 1 ? pts[pts.length - 1].t : t0 + 60_000;
  const x = (t) => L + ((t - t0) / (t1 - t0)) * (W - L - R);
  const y = (v) => T + (1 - (v - yMin) / (yMax - yMin)) * plotH;
  const yR = (v) => T + (1 - (v - rMin) / (rMax - rMin)) * plotH;
  const yOf = (s) => (s.right ? yR : y);

  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "history-chart", role: "img", "aria-label": "History chart" });

  // y grid + labels
  const yStep = Math.max(1, Math.ceil((yMax - yMin) / 6));
  for (let v = yMin; v <= yMax; v += yStep) {
    root.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: "#dde3ea" }));
    root.append(svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end", class: "axis", text: `${v}°` }));
  }
  if (rValues.length) {
    const decimals = rStep < 1 ? 1 : 0;
    const rColor = haKeys.length === 1 ? HA_COLORS[0] : null;
    for (let i = 0; rMin + i * rStep <= rMax + 1e-9; i++) {
      const v = rMin + i * rStep;
      root.append(svg("line", { x1: W - R, x2: W - R + 4, y1: yR(v), y2: yR(v), stroke: rColor ?? "#6b7785" }));
      root.append(svg("text", { x: W - R + 6, y: yR(v) + 4, "text-anchor": "start", class: "axis",
        ...(rColor ? { fill: rColor } : {}), text: `${v.toFixed(decimals)}°` }));
    }
    root.append(svg("line", { x1: W - R, x2: W - R, y1: T, y2: T + plotH, stroke: rColor ?? "#6b7785" }));
  }
  // x labels
  const spanH = (t1 - t0) / 3_600_000;
  const fmtTime = (t) => new Date(t).toLocaleString([], spanH > 36
    ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }
    : { hour: "2-digit", minute: "2-digit" });
  for (let i = 0; i <= xTicks; i++) {
    const t = t0 + ((t1 - t0) * i) / xTicks;
    root.append(svg("text", { x: x(t), y: T + plotH + 16, "text-anchor": i === xTicks ? "end" : "middle", class: "axis", text: fmtTime(t) }));
  }

  // temperature lines (connect all points; missing values are skipped)
  for (const s of series) {
    const sy = yOf(s);
    let d = "";
    for (const p of pts) {
      const v = p[s.key];
      if (typeof v !== "number") continue;
      d += `${d ? "L" : "M"}${x(p.t).toFixed(1)},${sy(v).toFixed(1)} `;
    }
    if (d) root.append(svg("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, ...(s.dash ? { "stroke-dasharray": s.dash } : {}) }));
    if (pts.length <= 150 && !s.dash) {
      for (const p of pts) {
        if (typeof p[s.key] === "number") root.append(svg("circle", { cx: x(p.t), cy: sy(p[s.key]), r: 2.5, fill: s.color }));
      }
    }
  }

  // average power (kW) panel
  if (hasEnergy) {
    const pMax = Math.max(1, Math.ceil(Math.max(0, ...power.map((s) => s.kw))));
    const py = (v) => powerTop + (1 - v / pMax) * powerH;
    root.append(svg("rect", { x: L, y: powerTop, width: W - L - R, height: powerH, fill: "#f6f8fa" }));
    for (const v of [0, pMax / 2, pMax]) {
      root.append(svg("line", { x1: L, x2: W - R, y1: py(v), y2: py(v), stroke: "#dde3ea" }));
      root.append(svg("text", { x: L - 6, y: py(v) + 4, "text-anchor": "end", class: "axis", text: `${v} kW` }));
    }
    for (const s of power) {
      const x1 = x(s.from);
      const top = py(Math.max(0, s.kw));
      root.append(svg("rect", { x: x1, y: top, width: Math.max(1, x(s.to) - x1), height: powerTop + powerH - top, fill: POWER_COLOR, opacity: 0.8 }));
    }
    if (!power.length) {
      root.append(svg("text", { x: L + 8, y: powerTop + powerH / 2 + 4, class: "axis", text: "Waiting for two consumption updates…" }));
    }
  }

  // ON/OFF bands
  bands.forEach((b, bi) => {
    const top = bandsTop + bi * (bandH + bandGap);
    root.append(svg("text", { x: L - 6, y: top + bandH - 3, "text-anchor": "end", class: "axis", text: b.label }));
    root.append(svg("rect", { x: L, y: top, width: W - L - R, height: bandH, fill: "#eef1f5" }));
    pts.forEach((p, i) => {
      const color = b.colorOf ? b.colorOf(p[b.key]) : b.colors[String(p[b.key]).toUpperCase()];
      if (!color) return;
      const next = pts[i + 1];
      const end = next ? next.t : p.t + pollMinutes * 60_000;
      const x1 = x(p.t);
      const w = Math.max(1, Math.min(x(end), W - R) - x1);
      root.append(svg("rect", { x: x1, y: top, width: w, height: bandH, fill: color }));
    });
  });

  // hover cursor + readout
  const cursor = svg("line", { x1: 0, x2: 0, y1: T, y2: H - 4, stroke: "#1d2733", "stroke-dasharray": "3 3", visibility: "hidden" });
  const overlay = svg("rect", { x: L, y: T, width: W - L - R, height: H - T, fill: "transparent" });
  root.append(cursor, overlay);
  const readout = el("div", { class: "history-readout muted", text: "Hover or tap the chart for details." });

  const showPoint = (ev) => {
    const pt = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(root.getScreenCTM().inverse());
    let best = pts[0];
    for (const p of pts) if (Math.abs(x(p.t) - pt.x) < Math.abs(x(best.t) - pt.x)) best = p;
    cursor.setAttribute("x1", x(best.t));
    cursor.setAttribute("x2", x(best.t));
    cursor.setAttribute("visibility", "visible");
    readout.textContent =
      `${new Date(best.t).toLocaleString()} · Zone 1 water ${fmt("temp", best.zone1_water_temperature)} · ` +
      `Outdoor ${fmt("temp", best.outdoor_temperature)} · Operation ${best.operation_status || "–"} · ` +
      `Pump ${best.pump_duty || "–"} · Direction ${best.direction || "–"} · Defrost ${best.defrost || "–"} · ` +
      `Energy today ${typeof best.energy_today_kwh === "number" ? `${best.energy_today_kwh} kWh` : "–"} · ` +
      `Avg power ${powerAt(best.t) ? `${powerAt(best.t).kw.toFixed(2)} kW` : "–"} · ` +
      `Compressor (est.) ${best.compressor ? `${best.compressor} (${best.compressor_why})` : "–"} · ` +
      `Shift ${typeof best.zone1_heat_shift === "number" ? best.zone1_heat_shift : "–"} · source ${best.source || "–"}` +
      series.slice(2).map((s) => ` · ${s.label} ${fmt("temp", best[s.key])}`).join("");
  };
  overlay.addEventListener("pointermove", showPoint);
  overlay.addEventListener("pointerdown", showPoint);
  overlay.addEventListener("pointerleave", (ev) => {
    if (ev.pointerType === "mouse") cursor.setAttribute("visibility", "hidden");
  });

  const legend = el("div", { class: "legend" },
    ...[...series, ...(hasEnergy ? [{ label: "Avg power (kW)", color: POWER_COLOR }] : []), ...bands].map((s) => {
      const swatch = el("i");
      swatch.style.background = s.dash
        ? `repeating-linear-gradient(90deg, ${s.color} 0 4px, transparent 4px 7px)`
        : s.color;
      return el("span", {}, swatch, s.legend ?? s.label);
    }));

  let onMs = 0, knownMs = 0;
  pts.forEach((p, i) => {
    const next = pts[i + 1];
    if (!next || !p.compressor || next.t - p.t > 3 * pollMinutes * 60_000) return;
    knownMs += next.t - p.t;
    if (p.compressor === "ON") onMs += next.t - p.t;
  });
  const runtime = knownMs ? ` · compressor (est.) running ${(onMs / 3_600_000).toFixed(1)} h of ${(knownMs / 3_600_000).toFixed(1)} h` : "";

  return el("div", {}, legend, root, readout,
    el("p", { class: "muted", text: `${pts.length} data point(s) · stored in the history CSV${runtime}` }),
    el("p", { class: "muted", text: `* Compressor estimated: OFF when operation is off, ON in defrost. Panasonic thermostat: OFF when Zone 1 water ≥ target + ${THERMO_OFF_K} K for ${THERMO_MS / 60_000} min (highest target of the last ${TARGET_UPDATE_MS / 60_000} min, since the unit's update time is unknown). Zone 1 water rising ${WATER_STEP_K} K above its low since the last change (e.g. the uptick after the pump-start dip) = ON, falling ${WATER_STEP_K} K below its high = OFF. Otherwise 15-min avg power ≥ ${COMPRESSOR_ON_KW * 1000} W = ON, ≤ ${COMPRESSOR_OFF_KW * 1000} W = OFF (ignored for ${POWER_LAG_MIN} min after a water-detected change, as the kWh total lags). Th.off: light = water within 1 K of target + ${THERMO_OFF_K} K (lowest possible target), dark = at/above it for the highest possible target (3-min timer running).` }));
}

// ------------------------------------------------------------------ tuning
// Cached per device so open state and unsaved edits survive the 1-min re-render.
const tuningViews = new Map();

function tuningSection(dev) {
  if (!dev.tuning) return null;
  const deviceId = dev.info.device_id;
  let view = tuningViews.get(deviceId);
  if (!view) {
    view = buildTuningView(deviceId);
    tuningViews.set(deviceId, view);
  }
  view.update(dev.tuning, dev);
  return view.details;
}

function buildTuningView(deviceId) {
  const forecastText = (fc) => {
    if (!fc) return "–";
    const time = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const day = fc.day ? new Date(`${fc.day}T12:00:00`).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }) : "?";
    const parts = [];
    if (typeof fc.mean === "number") parts.push(`${day}: mean ${fc.mean} °C`);
    if (typeof fc.night_min === "number") parts.push(`night ${time(fc.night_start)}–${time(fc.night_end)} min. ${fc.night_min} °C`);
    if (fc.error) parts.push(fc.error);
    if (fc.fetched_at) parts.push(`${fc.source}, ${time(fc.fetched_at)}`);
    return parts.join(" · ") || "–";
  };
  const numberInput = (min, max, step = "1") => el("input", { type: "number", min, max, step });
  const f = {
    enabled: el("input", { type: "checkbox" }),
    normal_shift: numberInput("-5", "5"),
    up_max: numberInput("0", "5"),
    down_max: numberInput("0", "5"),
    min_run_min: numberInput("0", "240", "10"),
    max_pause_min: numberInput("0", "300", "10"),
    indoor_target: numberInput("15", "28", "0.1"),
    min_outdoor: numberInput("-25", "20", "0.5"),
    night_start: el("input", { type: "time" }),
    night_end: el("input", { type: "time" }),
    indoor_sensor: el("select"),
    night_off: el("input", { type: "checkbox" }),
    night_off_min_mean: numberInput("-20", "25", "0.5"),
    night_off_min_low: numberInput("-30", "15", "0.5"),
    night_off_early_min: numberInput("0", "180", "5"),
    night_off_early_wait_min: numberInput("0", "120", "5"),
  };
  const field = (label, input, hint) =>
    el("label", {}, el("span", { text: label }), input, hint ? el("small", { class: "muted", text: hint }) : null);
  const save = el("button", { type: "button", text: "Save" });
  const discard = el("button", { type: "button", class: "secondary", text: "Discard changes" });
  const form = el("div", { class: "tuning-form" },
    el("label", { class: "check" }, f.enabled, el("span", { text: "Automatic tuning enabled" })),
    field("Normal shift", f.normal_shift, "−5 … 5"),
    field("Max. shift up", f.up_max, "steps above normal while heat is needed"),
    field("Shift down", f.down_max, "below normal after a stop (night / warm)"),
    field("Min. compressor run, min.", f.min_run_min, "shift up (within max.) until a run lasts this long; 0 = off"),
    field("Max. compressor pause, min.", f.max_pause_min, "then, if the room is at or below target, a shift below normal goes back to normal; 0 = off"),
    field("Indoor target °C", f.indoor_target, "warmer = down mode"),
    field("Min. outdoor °C", f.min_outdoor, "colder = no tuning, normal shift"),
    field("Night from", f.night_start),
    field("Night until", f.night_end),
    field("Indoor sensor", f.indoor_sensor),
    el("label", { class: "check" }, f.night_off, el("span", { text: "Night-time shutoff (whole unit off during the night hours)" })),
    field("Shutoff if tomorrow's mean above °C", f.night_off_min_mean, "forecast mean of the day after the night"),
    field("No shutoff if night min. below °C", f.night_off_min_low, "forecast lowest temperature of the night"),
    field("Early shutoff, min. before night", f.night_off_early_min, "only while the compressor is off; 0 = never"),
    field("Shutoff wait after compressor stop, min.", f.night_off_early_wait_min, "unit stays on this long after the last stop"));
  const buttons = el("div", { class: "control-row" }, save, discard);
  const status = el("div");
  const log = el("div");
  const details = section("Hysteresis tuning (curve shift)", el("div", {},
    el("p", { class: "muted", text:
      "Lengthens compressor runs: while it runs and the room needs heat, the shift goes up by 1 when the water is " +
      "within 1 K of the thermo off point (target + 3 K), up to normal + max. When the compressor stops, the shift " +
      "goes back to normal, or down by \u201cshift down\u201d at night or when the room is warmer than the indoor target; " +
      "when it starts again, back to normal. Until a run reaches the minimum run time the shift also goes up in " +
      "down mode (never above normal + max); after that it goes back to normal and the compressor stops on its own. " +
      "If the compressor stays off longer than the maximum pause with the shift lowered and the room is not " +
      "warmer than the indoor target, the shift goes back to normal so it restarts on its own. " +
      "Below the minimum outdoor temperature tuning is off and the shift " +
      "stays at normal. A shift changed elsewhere pauses tuning. Night-time shutoff turns the whole unit (including " +
      "hot water) off for the night hours when the forecast mean of the following day is above the threshold and " +
      "the night minimum is not below its limit, and back on at the end of the night. It turns the unit off only " +
      "when the compressor has been off for the wait time (a run is not cut short); when it is back on in the " +
      "morning the shift goes back to normal. " +
      "Within the early shutoff time before the night this can already happen, avoiding a last short run." }),
    form, allowControl ? buttons : el("p", { class: "muted", text: "Read-only: start the server with --allow-control to change these settings." }),
    status, log), true);
  let dirty = false, settings = null, sensorsKey = "";
  for (const input of Object.values(f)) {
    input.disabled = !allowControl;
    input.addEventListener("input", () => { dirty = true; });
    input.addEventListener("change", () => { dirty = true; });
  }

  function fillSensors(selected) {
    const sensors = (lastReport?.ha_sensors?.sensors ?? []).filter((s) => s.entity_id?.startsWith("sensor."));
    const ids = sensors.map((s) => s.entity_id);
    const key = [...ids, selected].join(",");
    if (key === sensorsKey) return;
    sensorsKey = key;
    const options = sensors.map((s) => el("option", { value: s.entity_id, text: `${s.name ?? s.entity_id} (${s.entity_id})` }));
    if (!ids.includes(selected)) options.unshift(el("option", { value: selected, text: `${selected} (not in Home Assistant data)` }));
    f.indoor_sensor.replaceChildren(...options);
  }

  function setValues(s) {
    settings = s;
    f.enabled.checked = s.enabled;
    f.night_off.checked = s.night_off;
    for (const k of ["normal_shift", "up_max", "down_max", "min_run_min", "max_pause_min", "indoor_target", "min_outdoor", "night_start", "night_end",
      "night_off_min_mean", "night_off_min_low", "night_off_early_min", "night_off_early_wait_min"]) f[k].value = String(s[k]);
    fillSensors(s.indoor_sensor);
    f.indoor_sensor.value = s.indoor_sensor;
    dirty = false;
  }

  function render(t, dev) {
    const sign = (v) => (typeof v === "number" ? (v > 0 ? `+${v}` : String(v)) : "–");
    const st = t.state ?? {}, live = t.live ?? {};
    const shift = dev?.water_target?.shift;
    const state = t.settings.enabled ? "Enabled" : st.paused ? `Paused: ${st.paused}` : "Disabled";
    const rows = [
      ["Status", state],
      ["Mode", live.mode ? `${live.mode}${live.note ? ` \u2013 ${live.note}` : ""}` : "–"],
      ["Shift", `${sign(shift)}${t.settings.enabled && typeof st.commanded === "number"
        ? ` \u00b7 commanded ${sign(st.commanded)}${st.confirmed ? "" : " (waiting for the unit)"}` : ""}`],
      ["Indoor", typeof live.indoor === "number" ? `${live.indoor} °C (target ${t.settings.indoor_target} °C)` : "–"],
      ["Outdoor", typeof live.outdoor === "number" ? `${live.outdoor} °C (min. ${t.settings.min_outdoor} °C)` : "–"],
      ["Compressor (est.)", st.compressor ?? "–"],
      ["Forecast", forecastText(t.night_off?.forecast)],
      ["Night-time shutoff", `${t.night_off?.active ? "Unit off – " : ""}${t.night_off?.note ?? "–"}`],
      ["Last check", live.checked_at ? new Date(live.checked_at).toLocaleString() : "–"],
    ];
    status.replaceChildren(el("table", { class: "kv" }, el("tbody", {},
      rows.map(([k, v]) => el("tr", {}, el("td", { text: k }), el("td", { text: v }))))));
    const entries = [...(t.log ?? [])].reverse();
    log.replaceChildren(entries.length
      ? el("table", {},
        el("thead", {}, el("tr", {}, ["Time", "Event", "Shift", "Details"].map((h) => el("th", { text: h })))),
        el("tbody", {}, entries.map((e) => el("tr", {},
          el("td", { text: new Date(e.time).toLocaleString() }),
          el("td", { text: e.event }),
          el("td", { text: typeof e.to === "number" ? `${typeof e.from === "number" ? `${sign(e.from)} \u2192 ` : ""}${sign(e.to)}` : "" }),
          el("td", { text: e.text ?? "" })))))
      : el("p", { class: "muted", text: "No tuning events yet." }));
  }

  let lastDev = null;
  save.addEventListener("click", async () => {
    const body = {
      enabled: f.enabled.checked,
      normal_shift: Number(f.normal_shift.value),
      up_max: Number(f.up_max.value),
      down_max: Number(f.down_max.value),
      min_run_min: Number(f.min_run_min.value),
      max_pause_min: Number(f.max_pause_min.value),
      indoor_target: Number(f.indoor_target.value),
      min_outdoor: Number(f.min_outdoor.value),
      night_start: f.night_start.value,
      night_end: f.night_end.value,
      indoor_sensor: f.indoor_sensor.value,
      night_off: f.night_off.checked,
      night_off_min_mean: Number(f.night_off_min_mean.value),
      night_off_min_low: Number(f.night_off_min_low.value),
      night_off_early_min: Number(f.night_off_early_min.value),
      night_off_early_wait_min: Number(f.night_off_early_wait_min.value),
    };
    if (body.night_off && !settings?.night_off &&
        !confirm("Enable night-time shutoff? The dashboard will turn the whole unit off during the night hours.")) return;
    if (body.enabled && !settings?.enabled &&
        !confirm("Enable automatic tuning? The dashboard will change the Zone 1 curve shift on its own.")) return;
    save.disabled = true;
    save.textContent = "Saving…";
    try {
      const res = await postJSON("api/tuning", { device_id: deviceId, settings: body });
      setValues(res.tuning.settings);
      render(res.tuning, lastDev);
    } catch (e) {
      if (e.status === 401) return showLogin(e.message);
      showAppError(e.message);
    } finally {
      save.disabled = false;
      save.textContent = "Save";
    }
  });
  discard.addEventListener("click", () => { if (settings) setValues(settings); });

  return {
    details,
    update(t, dev) {
      lastDev = dev;
      if (!dirty) setValues(t.settings);
      else {
        const selected = f.indoor_sensor.value;
        fillSensors(selected);
        f.indoor_sensor.value = selected;
      }
      render(t, dev);
    },
  };
}

// ------------------------------------------------------------------ render
function haSensorTiles(ha) {
  const title = el("h2", { class: "device-title", text: "Home Assistant sensors" });
  if (ha.error) return [title, el("div", { class: "card" }, el("p", { class: "error", text: ha.error }))];
  const sensors = ha.sensors ?? [];
  if (!sensors.length) return [title, el("p", { class: "muted", text: "No temperature sensors found." })];
  const tiles = el("div", { class: "tiles" });
  for (const s of sensors) {
    const value = typeof s.value === "number" ? `${s.value} ${s.unit ?? ""}`.trim() : s.state ?? "–";
    const changed = s.last_changed ? `changed ${new Date(s.last_changed).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : null;
    tiles.append(tile(s.name ?? s.entity_id, value, [s.entity_id, changed].filter(Boolean).join(" · "), typeof s.value !== "number"));
  }
  const note = ha.auto
    ? el("p", { class: "muted", text: "Showing all temperature sensors (max. 12). Configure specific entity IDs to choose." })
    : null;
  return [title, tiles, note].filter(Boolean);
}

function renderReport(report) {
  const container = $("devices");
  container.replaceChildren();
  $("status-line").textContent =
    `Updated ${new Date(report.generated_at).toLocaleString()} · auto-update every ${pollMinutes} min · ` +
    `${report.account.device_count} device(s) · ` +
    `token valid until ${report.account.token_expiration ? new Date(report.account.token_expiration).toLocaleString() : "–"}`;

  if (!report.devices.length) {
    container.append(el("p", { class: "muted", text: "No devices found on this account." }));
    return;
  }

  if (report.ha_sensors) container.append(...haSensorTiles(report.ha_sensors));

  for (const dev of report.devices) {
    container.append(el("h2", { class: "device-title", text: `${dev.info.name} (${dev.info.device_id})` }));
    container.append(overviewTiles(dev));

    if (dev.status?.error) {
      container.append(el("div", { class: "card" },
        el("p", { class: "error", text: `Live status unavailable: ${dev.status.error}` }),
        dev.status.cause ? el("p", { class: "muted", text: `Cause: ${dev.status.cause}` }) : null,
        el("p", { class: "muted", text: "Values marked “device list” come from the device overview instead." })));
    }
    container.append(section("History", historyView(dev.info.device_id), true));
    const tuning = tuningSection(dev);
    if (tuning) container.append(tuning);
    if (dev.consumption) container.append(section("Energy consumption", energyView(dev.consumption), true));
    container.append(section("Live status (DeviceStatus)", kvTable(dev.status ?? {})));
    container.append(section("Derived / computed", kvTable(dev.derived ?? {})));
    container.append(section("Configuration (DeviceInfo)", kvTable(dev.info ?? {})));
    container.append(section("Raw device list entry", kvTable(dev.raw_device_list_entry ?? {})));
  }
}

// ------------------------------------------------------------------ api
async function api(path, options = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...options });
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) {
    const err = new Error(data?.error ?? `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

const postJSON = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function setAccount(text, { canLogin, canLogout }) {
  $("user").textContent = text;
  $("btn-show-login").hidden = !canLogin;
  $("btn-logout").hidden = !canLogout;
}

// message: why login is needed; currentUser stays set if the server still has
// a (stale) session, so "Log out" can delete its stored token.
function showLogin(message) {
  stopAutoUpdate();
  $("app-view").hidden = true;
  $("toolbar").hidden = true;
  $("login-view").hidden = false;
  $("login-error").hidden = !message;
  $("login-error").textContent = message ?? "";
  setAccount(currentUser ? `${currentUser} · session expired` : "Not signed in",
    { canLogin: true, canLogout: !!currentUser });
  if (currentUser && !$("username").value) $("username").value = currentUser;
  ($("username").value ? $("password") : $("username")).focus();
}

function showApp(username) {
  currentUser = username;
  $("login-view").hidden = true;
  $("app-view").hidden = false;
  $("toolbar").hidden = false;
  const until = tokenExpiration ? ` · token until ${new Date(tokenExpiration).toLocaleString()}` : "";
  setAccount(`Signed in as ${username}${until}`, { canLogin: false, canLogout: true });
  startAutoUpdate();
}

function applySession(s) {
  allowControl = !!s.allow_control;
  pollMinutes = s.poll_minutes ?? pollMinutes;
  currentUser = s.logged_in ? s.username : null;
  tokenExpiration = s.token_expiration ?? null;
}

// The server refreshes every pollMinutes; check its cached result once a minute.
async function checkLatest() {
  try {
    const { report } = await api("api/report/latest");
    if (report && report.generated_at !== lastReport?.generated_at) {
      lastReport = report;
      renderReport(report);
    }
  } catch (e) {
    if (e.status === 401) showLogin(e.message);
  }
}

function startAutoUpdate() {
  stopAutoUpdate();
  autoTimer = setInterval(checkLatest, 60_000);
}

function stopAutoUpdate() {
  if (autoTimer) clearInterval(autoTimer);
  autoTimer = null;
}

async function loadReport() {
  const btn = $("btn-refresh");
  btn.disabled = true;
  btn.textContent = "Loading…";
  $("app-error").hidden = true;
  try {
    const consumption = $("opt-consumption").checked ? "1" : "0";
    lastReport = await api(`api/report?consumption=${consumption}`);
    renderReport(lastReport);
  } catch (e) {
    if (e.status === 401) return showLogin(e.message);
    showAppError(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Refresh";
  }
}

// ------------------------------------------------------------------ events
$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("btn-login");
  btn.disabled = true;
  btn.textContent = "Signing in…";
  $("login-error").hidden = true;
  try {
    const res = await postJSON("api/login", { username: $("username").value, password: $("password").value });
    $("password").value = "";
    applySession(res);
    showApp(res.username);
    await loadReport();
  } catch (e) {
    showLogin(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Sign in";
  }
});

$("btn-refresh").addEventListener("click", loadReport);

$("btn-show-login").addEventListener("click", () => showLogin($("login-error").hidden ? null : $("login-error").textContent));

$("btn-logout").addEventListener("click", async () => {
  await postJSON("api/logout", {}).catch(() => {});
  lastReport = null;
  currentUser = null;
  tokenExpiration = null;
  $("devices").replaceChildren();
  $("username").value = "";
  showLogin();
});

$("btn-download").addEventListener("click", () => {
  if (!lastReport) return;
  const blob = new Blob([JSON.stringify(lastReport, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: `aquarea_${new Date().toISOString().replace(/[:.]/g, "-")}.json` });
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
});

(async () => {
  try {
    const s = await api("api/session");
    applySession(s);
    if (s.logged_in) {
      showApp(s.username);
      const { report } = await api("api/report/latest");
      if (report) { lastReport = report; renderReport(report); }
      else await loadReport();
    }
    else showLogin();
  } catch (e) {
    showLogin(e.message);
  }
})();
