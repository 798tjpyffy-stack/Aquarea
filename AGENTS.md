# AGENTS.md – Aquarea Dashboard

Context for coding agents working on this repository. Keep it up to date when
versions, deployment or behaviour change.

## What this is

A local web dashboard (aiohttp + vanilla JS, no build step) for a Panasonic
Aquarea heat pump (L series, one device, Zone 1 on a heating curve), packaged
as a Home Assistant **app** (formerly "add-on"), opened through HA ingress
("Aquarea" in the sidebar). Besides showing live values and a CSV history it
can **control** the unit (curve shift, power, quiet mode) and runs automatic
functions: hysteresis tuning, night-time shutoff, early off, quiet mode auto.

The owner's goal: long compressor runs (few starts), no unnecessary runs at
night or when the room is warm, minimal wear of the unit's EEPROM, and quiet
operation when it does not cost efficiency.

## Two versions (branches)

| Branch | Version line | Data source | Status |
| --- | --- | --- | --- |
| `main` | 0.1.x (latest 0.1.71) | Panasonic Aquarea **cloud** via `aioaquarea` (polling) | deployed to HA, pushed |
| `heishamon` | 0.2.x (latest 0.2.10) | **HeishaMon** over MQTT (local, live) | pushed, not yet deployed (owner deploys it on a weekend) |

- `heishamon` branched from `16d0b43` (daily command limits) and replaced the
  cloud layer: `heishamon.py`, `thermo.py`, `energy.py` are new;
  `aquarea_report.py`, `token_store.py`, `.env.example` exist only on `main`.
- **Every functional change must go into both branches** (unless it is
  specific to the data source). Port with
  `git diff A B -- <files> | git apply --3way` and resolve conflicts (the
  step-up code and texts differ, see below). Bump both versions
  (0.1.N+1 and 0.2.M+1).
- On the owner's machine `main` is checked out in a separate worktree
  (`~/Code/copilot-worktrees/leisha/christian-siegl-3stripes-miniature-eureka`),
  `heishamon` in `~/Code/leisha`. Same repository, so commits of both are
  visible from either tree.

### Differences that matter

| Topic | `main` (cloud) | `heishamon` |
| --- | --- | --- |
| Compressor on/off | estimated (`compressor.estimate`: water trend + 15-min avg power from `energy_today_kwh`; a water-based stop is dropped when the next clean avg power still shows ≥ 0.2 kW, 0.1.67/0.2.7) | measured: compressor frequency > 0 Hz (old cloud rows still estimated) |
| +1 step timing | `up_wait_pct` (% of the last whole degree's heat-up time, `water_rise`), `MIN_STEP` 5 min, urgent at target + 3 K | follows the unit's **thermo off timer** live (`thermo.ThermoWatch`): step `step_lead_s` (60 s) before the 3-min timer runs out, once per timer |
| Energy | cloud consumption (hourly kWh, lags ~15 min, local-day fix in 0.1.61) | integrated from HeishaMon power in/out (`energy.py`, `/data/energy.json`) |
| Restart rule | unknown | learned (`thermo.py`, `/data/thermo.json`) |
| Polling | cloud rate limits: status every poll, device list hourly, energy 5 min / fast mode, off-poll 5 min | MQTT live; `poll_minutes` = history/evaluation interval |
| Auth | Aquarea login, tokens in `data/tokens.json`, `--shared-session` in HA | MQTT credentials from the Mosquitto app (`services: mqtt:need`) |

## Repository layout

- Root `*.py` + `static/` = the code (run locally with `python web.py ...`).
- `homeassistant/aquarea_dashboard/` = the HA app (Docker build context):
  `config.yaml` (version, options), `Dockerfile`, `run.sh`, `DOCS.md`
  (user documentation shown in HA), `translations/en.yaml`,
  `app/` = a **copy** of root `*.py` + `static/`.
- `repository.yaml` at the root (Supervisor globs `**/config.*`).
- `./sync_app.sh` copies root code into `app/` — it only copies files that
  already exist in `app/`; **new modules must be copied once by hand**.
- `data/`, `output/`, `token`, `.env` are git-ignored (local, stale).

### Modules

- `web.py` – aiohttp server, JSON API (JSON-only POST = CSRF protection,
  strict CSP), polling loop, glue for all automatic functions, `--allow-control`.
- `tuning.py` – `Tuner`: settings/state/log per device in `tuning.json`
  (mode 600); hysteresis tuning, night-time shutoff, early off, outdoor
  thresholds, quiet auto entry points. The module docstring is the reference
  for the tuning rules.
- `quiet.py` – quiet mode auto (decide/record).
- `compressor.py` – compressor state per history row, daily stats
  (`daily_stats`: starts/hours of runs **started** that day), power segments.
- `cooling.py` – learned room cooling model (see below).
- `forecast.py` – Open-Meteo hourly forecast (no key), location rounded to 0.01°.
- `history.py` – CSV history (`read_rows` needs a `Path`; it parses types).
- `ha_sensors.py` – HA temperature sensors via REST (`SUPERVISOR_TOKEN` in the
  app; locally `HA_URL` + `HA_TOKEN` from the environment — never via CLI/chat).
- `heishamon.py`, `thermo.py`, `energy.py` – `heishamon` branch only.
- `aquarea_report.py`, `token_store.py` – `main` branch only (cloud).
- `static/app.js` – whole UI; `explain(key, text, title)` = collapsible help
  texts (keep them in sync with behaviour and `DOCS.md`).

## Build, run, test

- Python ≥ 3.11 (venv `.venv`, `python3.11`). No test suite in the repo;
  scenario scripts live in `/tmp/parity/` (macOS deletes old `/tmp` files —
  recreate as needed). Pattern: build `tuning.Observation(...)` objects (filter
  kwargs by `dataclasses.fields` so one script works on both branches), a
  `Tuner(Path(tmp)/"t.json")`, set `entry(dev)["settings"]`, pre-set
  `state["compressor"]` to avoid start/stop edges, call `evaluate()`.
- Syntax checks: `python -m py_compile *.py`, `node --check static/app.js`.
- Live data (read-only) via the Samba share:
  `/Volumes/addon_configs/local_aquarea_dashboard/history.csv` (~1 row/min).
  `tuning.json` lives in the app's private `/data` (not reachable).
- Local run (`main`): `python web.py` (127.0.0.1:8080; Aquarea login in the UI).
  Local run (`heishamon`): `python web.py --mqtt-host <broker>`
  (`MQTT_USER`/`MQTT_PASSWORD` from env or `.env`).
- Control is only possible with `--allow-control` (HA option `allow_control`).

## Release and deploy

1. Make the change on one branch, test, port to the other branch.
2. Bump `homeassistant/aquarea_dashboard/config.yaml` `version`.
3. `./sync_app.sh` (prints the version).
4. Commit (message style: `0.1.62: <what>`).
5. Deploy to HA (cloud version currently):
   `rsync -a --delete --exclude .DS_Store homeassistant/aquarea_dashboard/ /Volumes/local_apps/aquarea_dashboard/`
   (Samba share `local_apps`; "rsync mkpath: Permission denied" = share not
   mounted — mount it in Finder first). The owner then clicks **Update** in HA.
6. Push only when asked: `git push origin main heishamon`
   (GitHub `798tjpyffy-stack/Aquarea`, public; HA repository URL
   `https://github.com/798tjpyffy-stack/Aquarea`).

Switching from the local app to the repository app means a new slug: new
`/data` and `/addon_configs` folders (copy `history.csv`).

## Safety rules (do not weaken)

- The unit stores settings in **EEPROM**: at most 6 shift commands per hour
  and 48 per 24 h; quiet mode 10/h and 24/24 h. A skipped command is logged.
- A command not confirmed by the unit is resent once (`main`: after 10 min,
  cloud lag; `heishamon`: after 20 s); unconfirmed
  for 20 min → tuning pauses.
- A shift or quiet level changed elsewhere (app, unit, manual control) pauses
  the automatic function.
- During a defrost and 10 min after it (`compressor.DEFROST_SETTLE_MS`,
  `Observation.hold_until`) no shift or power command is sent.
- The night-time shutoff does not cut a run short: the unit is only turned off
  after the compressor has been off for `night_off_early_wait_min`. Exception
  (owner's request, 0.1.66/0.2.6): in the night hours (not before them, not in
  an early off) a run that has reached `min_run_min` (> 0) may be cut when the
  shutoff is due. Only a unit the dashboard turned off is turned back on.

## Features and reasoning

### Heat pump facts (Panasonic L series, service manual §14.1.2.1)

- **Thermo off**: water outlet > target + 3 K continuously for 3 min → the
  compressor stops (pump keeps running). Target = heating curve value + shift.
- **Restart**: manual says outlet < inlet at thermo off − 3 K after 3 min, but
  observed restarts depend on the target (a lower shift clearly delays the
  restart) → the rule is learned on `heishamon`.
- The unit works in whole °C. Heat curve is configured as `--heat-curve
  "5:30,-7:34"` (outdoor:water, linear, flat outside); shift = Zone 1 heat_set
  in curve mode.
- Observed: pump only ~40 W, standby ~10 W; after power-on the water dips ~1 K
  before the compressor starts; the room keeps rising ~0.2 K for 30–40 min after
  a stop (floor heating).

### Hysteresis tuning (curve shift)

Goal: longer runs, fewer starts. Modes from `Tuner._mode`:

- **off** – cold lock (see outdoor thresholds): normal shift, no up/down.
- **down** – early off, night hours of a night without shutoff because the
  forecast thresholds are not met (`_night_down`, `night_off` on; 0.1.70/0.2.9,
  before: shutoff nights), or room warmer than the indoor target.
- **hold** – indoor temperature unavailable.
- **night** (0.1.62/0.2.2) – room needs heat, between `night_start` and the
  first `night_off_gate_time` after it (independent of the shutoff switches):
  +1 steps only until the minimum run, no further.
- **up** – room at or below the indoor target.

Rules:

- While the compressor runs in **up** mode the shift goes +1 just before the
  unit would reach thermo off, up to normal + `up_max` (3). Steps are made as
  **late as possible**: the owner observed that the unit's own regulation uses
  more energy with a higher target.
- Until a run reaches `min_run_min` (120) the +1 steps are made in every mode
  except "off" (never above normal + up_max).
- After the minimum run (non-up modes) the **raised shift is kept until the
  compressor stops on its own** (0.1.62); only the cold lock resets at once.
  Exception (0.1.64/0.2.4): room warmer than the indoor target and the
  compressor still running `RAISED_KEEP_MAX` (1 h) after the last confirmed
  shift command → back to normal.
  Before 0.1.62 it went back to normal immediately.
- At a compressor **stop**: shift → normal, or normal − `down_max` (3) in
  down mode (lower target delays the next start). While off, a change to down
  mode also shifts down.
- At a compressor **start** (or whenever it runs below normal): back to normal.
- **Max. pause** (`max_pause_min` 180): off that long with the shift lowered and
  the room not warmer than the target → normal so it restarts; no shift down
  again until it has run (`pause_reset` flag).
- A stop while a step was still possible is logged ("early stop").
- Unit turned back on after a shutoff → `reset_after_on` → normal shift.

### Outdoor thresholds (0.1.61/0.2.1)

Shared `entry["outdoor"]` = `cold_threshold` (2 °C) / `cold_release` (4 °C)
hysteresis, so a flickering outdoor reading does not switch back and forth.
Tuning checks the outdoor temperature only when a +1 step is due; at or below
the threshold it goes "off" (cold lock) until ≥ release. Quiet auto: quiet 0
while cold-locked. (Replaced `settings.min_outdoor` and the quiet threshold;
old keys are dropped on load.)

Shift range (0.1.71/0.2.10, `shift_range`): `full_range_from` (8 °C) /
`range_2_from` (5 °C) in `entry["outdoor"]`: full `up_max`/`down_max` only
at or above 8 °C, at most ±2 at or above 5 °C, else ±1. Checked when a +1 step or a shift
down is decided; a shift already beyond the range is kept (avoids
extra EEPROM writes on a flickering reading). Missing keys get defaults on load.

### Night-time shutoff

Turns the whole unit off for the night (`night_start`–`night_end`) if the next
day's forecast mean is above `night_off_min_mean` and the night minimum not
below `night_off_min_low`; back on at the end (or earlier if the forecast drops).

- Early start (`night_off_early_min`) and "prevent an unnecessary last run"
  (`night_off_trend`, projected with the cooling model) before the night.
- **Dynamic start** (`night_off_dynamic`, between `_from` and `_until`): off as
  soon as the projected room temperature at the end of the night (unit off)
  is ≥ gate temp + margin; not reached by `_until` → no shutoff that night.
- **Stay off after the night** (`night_off_extend`): room still ≥ target at the
  end → stays off until below; may chain into the next night. Also at the end
  of an early off (0.1.68/0.2.8; also with `night_off` disabled), not after
  ending it manually (`early_off_cancelled`) or after the 6am gate.
- **6am gate** (`night_off_gate`, time `06:00`, temp 21.0): from the first gate
  time after the start, indoor below the threshold ends the night / early off.
- Switching the unit on elsewhere ends the shutoff for that night.
- In a shutoff night the room decides as by day (except the 0.1.62 night mode
  limit above); a night without shutoff because of the forecast is "down".

### Early off

Manual (Operation tile) or automatic (`early_off_auto`, indoor >
`early_off_auto_temp` 23.0 → until indoor < target): works like a shutoff
night starting now (no forecast conditions), tuning in down mode, no max-pause
reset. Use case: wood stove, sun.

### Cooling model (`cooling.py`)

`dT_in/dt = -k (T_in - T_out) + g + e(t)`; fitted daily from compressor-off
stretches (17:00–08:00, robust medians), evening gains until 22:00 and a
decaying release after. Used to project the room temperature (trend early off,
dynamic start, extend). Earlier approaches (indoor trend, temperature bins)
were replaced because they were less accurate.

### Quiet mode auto

Cold → quiet 0 (cold lock), otherwise quiet 3. Advanced: quiet 3 after each
stop, 0 once running and water ≥ target − `release_k`. After a defrost quiet 0
until outdoor ≥ `cold_release` (shared outdoor setting; was its own
`defrost_until` 5 °C before 0.1.65/0.2.5).

### Statistics

"Today: starts · running h · h per start" (`compressor.daily_stats`) counts
only runs that **started** today (a run across midnight belongs to the day
before; 0.1.62). Data gaps > `history_gap` count as unknown; same-state gaps
up to 2 h are bridged.

## Cloud version specifics (`main`)

- `aioaquarea` 1.0.7: `get_device_status` fails for this device → raw status
  is captured (`capture_raw_status`); the library's operation status is wrong
  (use zone status); `Device._zones` is shared across devices;
  `get_devices()` caches (reset `_device_manager._devices`).
- Token refresh is broken in the library → own refresh in
  `token_store.PersistentClient` (persists right after refresh).
- **App version**: Panasonic only accepts exact app versions; the library
  scrapes the App Store version → `_refresh_app_version` probes a few
  candidates (too many → HTTP 429). Symptom: everyone logged out, 401 code 4106.
- Consumption: the library sent `+00:00` as timezone → UTC days; fixed by
  setting the local offset (0.1.61). After that fix the first poll counted the
  00:00–01:00 hour once (one-off jump).
- Plain "HTTP 401" without JSON = expired HA ingress session cookie (the UI
  retries); all own 401s are JSON `{"error"}`.
- Never log aioaquarea at DEBUG in production (auth details).

## Conventions

- Plain, short user-facing texts (English), units with spaces (`22.5 °C`).
- Keep `DOCS.md`, the `app.js` explain texts and the `tuning.py` docstring
  consistent with behaviour.
- Minimal changes, no new dependencies without need, no secrets in code,
  CLI arguments or logs.
- Times are local and timezone-aware (`.astimezone()`); state timestamps are
  ISO strings (`_iso` / `_parse`).
