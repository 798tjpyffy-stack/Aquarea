# Aquarea Dashboard

Dashboard for Panasonic Aquarea heat pumps (Aquarea Smart Cloud), opened from the
Home Assistant sidebar ("Aquarea").

## Usage

1. Start the app and open **Aquarea** in the sidebar.
2. Log in with your Panasonic account. The password is not stored; only the
   login token (in the app's private `/data`), which is renewed automatically.
   The login is shared by all Home Assistant clients (browser, companion app).
3. Data is fetched every *Update interval* minutes, also when no browser is open.

Panasonic allows one active login per account: logging in with the Panasonic app
can interrupt the dashboard. A second Panasonic account (shared device) avoids this.

## Options

| Option | Description |
| --- | --- |
| `poll_minutes` | Update interval (1–1440, default 1). Every update fetches the live status (1 cloud request). To save requests, the device list and the month/year energy totals are refreshed hourly, today's energy every 5 minutes, but on every update while the unit is on and the compressor is not running (half the compressor starts are only seen in the energy data). While the unit is switched off, updates run every 5 minutes (today's energy every 15), except at the night-time shutoff's times (night start/end, gate time, end of an early off) and for 10 minutes after a power command. The refresh button fetches everything. |
| `allow_control` | Enable changing the heat pump from the dashboard (heating target/shift, power, quiet mode) and the automatic functions (hysteresis tuning, night-time shutoff, quiet mode auto). |
| `debug` | Verbose logging. |
| `ha_sensors` | Home Assistant temperature sensors (entity IDs) shown as tiles and recorded in the history. Empty: all temperature sensors (max. 12). |
| `heat_curve` | Zone 1 heating curve as `OUTDOOR:WATER` points, e.g. `5:30,-7:34` (linear in between, flat outside). The dashboard shows and records the target water temperature = curve + shift. |

## Data

`history.csv` (timestamp, operation status, pump duty, outdoor temperature,
zone 1 water temperature) is written to the app config folder:
`/addon_configs/<repo>_aquarea_dashboard/history.csv` (Samba / File editor).

## Hysteresis tuning

With `allow_control` and Zone 1 on a heating curve, the **Hysteresis tuning**
section changes the curve shift automatically to lengthen compressor runs:

- While the compressor runs (estimated from the history) and the room needs heat,
  the shift goes up by 1 when the water is within 1 K of the thermo off point
  (target + 3 K), up to *normal + max. shift up*.
- When the compressor stops, the shift returns to normal, or to *normal − shift
  down* in the night hours while a night-time shutoff is planned or running, or
  when the indoor sensor is above the indoor target. In the night hours without
  a shutoff (switch off, forecast conditions not met, unit on again after the
  6am gate or turned on elsewhere, dynamic start not reached) the indoor
  temperature decides as by day. While
  the compressor is off this also happens later, as soon as the night starts
  or the room gets warmer than the target.
- When the compressor starts again, the shift returns to normal (also later
  while it runs, should that command have been skipped or failed).
- **Min. compressor run** (default 120 min, 0 = off): until a run has lasted
  this long, the shift also goes up near thermo off when no heat is needed
  (night, warm room), never above *normal + max. shift up* (so a run can still
  end earlier). After that the shift returns to normal and the compressor stops
  on its own.
- **Up step wait** (default 85 %, 0 = at once): a +1 step waits this share of
  the time the water took for its last whole degree, counted from when it
  reached its current degree. A late step keeps the unit's own regulation from
  working harder than needed and avoids spikes in its power calculation. The
  degree is timed in the current run, else the last timed degree is used. Once
  the water reaches the thermo off point (target + 3 K; the unit stops 3 min
  later) the step is made at once. If the compressor stops while a step was
  still possible, the log shows "Compressor stopped at shift … before max.".
- **Max. compressor pause** (default 180 min, 0 = off): if the compressor stays
  off this long with the shift below normal (shift down) and the room is not
  warmer than the indoor target, the shift returns to normal so the compressor
  restarts on its own; it is not lowered again until the compressor has run.
- Below the *min. outdoor* temperature (default 0 °C) tuning is off and the
  shift returns to normal.
- During a defrost and for 10 minutes after it, tuning and the night-time
  shutoff take no action (quiet mode auto still does). The compressor counts as
  running throughout, so a defrost is no extra start, and the lower water
  temperature after it is not taken as a stop until the water has risen again.

At most one step per 5 minutes and 6 commands per hour. A shift changed elsewhere
(Panasonic app, unit, manual control) pauses tuning. The indoor sensor must be
one of the `ha_sensors` (or leave `ha_sensors` empty). Settings, state and log are
stored in the app's private `/data/tuning.json`.

### Night-time shutoff

A separate switch in the same section turns the whole unit (including hot water)
off at the start of the night hours and back on at their end, if:

- the forecast mean temperature of the day after the night is above the
  threshold (default 8 °C), and
- the forecast lowest temperature of the night is not below the limit
  (default −4 °C). If the night forecast drops below it while the unit is off,
  the unit is turned back on.

The hourly forecast comes from [Open-Meteo](https://open-meteo.com) (no API key),
refreshed every hour, for the Home Assistant home location rounded to 0.01°.
Only a unit the dashboard turned off is turned back on; turning it on elsewhere
ends the shutoff for that night.

The unit is only turned off once the compressor has been off for the **wait
after compressor stop** (default 30 min): a run is never cut short and may last
into the night (with the min. compressor run above). When the unit reports on
again after the dashboard turned it on, the shift (still lowered from the
night) is set back to normal. If the unit does not report off after 3 attempts, the
dashboard gives up for that night, but still turns it back on at the end of the
night should it report off later.

**Early shutoff** (default 60 min, 0 = never): within this time before the
night the unit can already be turned off (same compressor rule), so it doesn't
start once more for a short run just before the night.

**Prevent unnecessary last run** (checkbox, up to 180 min before the night by
default): within this time before the night the unit can also be turned off
(same compressor rule) if the indoor temperature, projected to the start of
the night with the learned room cooling model, is still at or above the
indoor target. This only applies while
the compressor is off; the decision is made once the wait after compressor
stop has passed.

The cooling model describes how fast the room cools while the compressor is
off:

    dT_in/dt = -k * (T_in - T_out) + g + e(t)

- `k`: heat loss per hour (shown as the time constant 1/k in hours),
- `g`: constant internal gains (only once the data has enough spread between
  indoor and outdoor temperature, otherwise 0),
- `e(t)`: people at home add heat in the evening until 22:00 (`evening`);
  afterwards the warmed air and surfaces give it back to the walls and floor,
  the room cools faster for a while (`release`, fading over hours).

The projection steps through the hours with the outdoor temperature from the
weather forecast (shifted to the unit's own outdoor reading, or constant
without a forecast); a rising rate counts as flat, sun is not modelled.

The samples are collected from the history and stored with the model in the
app's private `/data/cooling.json` (first run: the whole history, then every
30 min the new rows). Samples are 2 h chunks (at least 60 min) of
compressor-off time from 17:00 to 08:00 (no sun), starting 30 min after a
compressor stop, without defrost or hot water; each has the least-squares
slope of the indoor sensor and the mean indoor and outdoor temperature. The
model is fitted once a day with medians (robust against open windows and
similar): `k` (and `g`) from at least 15 night samples starting between 00:00
and 08:00, then the evening and release terms from the remaining samples.
Until a model exists, the unit is only turned off with the early shutoff.
Changing the indoor sensor starts a new collection. The model is shown as
"Room cooling (learned)".

**Stay off after the night** (checkbox, on by default): if the indoor
temperature is at or above the indoor target at the end of a shutoff night,
the unit stays off and is turned on once the room is below the target (or the
indoor reading is missing). If the cooling model shows the room stays
at or above the target until the next night, the unit stays off the whole day:
within the early shutoff time before the next night, or with **prevent
unnecessary last run** within its time if the projection to the night is still
at or above the target, the next shutoff night takes over without turning the
unit on in between (if its forecast conditions are met). Not after an early
off, not after the 6am gate ended the night.

**Dynamic night start** (checkbox, off by default; earliest 18:00, latest
03:00, margin 0 K): an alternative to early shutoff and prevent unnecessary
last run (both are ignored while it is on). Between the earliest and the latest
start the unit is turned off as soon as the room, projected with the cooling
model with the unit off, is still at or above the 6am gate threshold + margin
at the end of the night ("night until"), the compressor has been off for the
shutoff wait time and the forecast conditions are met. A warm room starts the
night earlier, a colder one later (the compressor runs again, and the
remaining night gets shorter). If the projection is not reached by the latest
start, the unit stays on that night. Without a cooling model the night starts
at "night from". After **stay off after the night**, the next night only takes
over if this projection is met. Early off, auto early off and the 6am gate are
unchanged. The earliest start must be at or before "night from", the latest at
or after it.

**6am gate** (checkbox, on by default; time 06:00, threshold 21.0 °C): from
the gate time until the end of the night, an indoor temperature below the
threshold ends the shutoff night early: the unit is turned back on (shift back
to normal) and stays on for the rest of that night. Before the unit has been
turned off, it also prevents a shutoff from the gate time on. It applies to
early off as well (from the first gate time after the early off started): the
early off is ended, and an overlapping shutoff night with it. The gate time
must lie within the night hours, otherwise it never applies to the shutoff
night.

### Early off

For when the pump is not needed, e.g. the wood stove is burning or the sun has
made it too warm: **Early off** in the Operation tile starts a shutoff now,
either until the end of the night hours or for a number of hours (default 12).
It works like a shutoff night that starts now, without the forecast conditions
and the night-time shutoff switch: the tuning runs in down mode (a running
compressor still reaches the min. run, then the shift goes back to normal so it
stops on its own, then down), the unit is turned off once the compressor has
been off for the wait time, and turned back on at the end (the shift goes back
to normal). There is no max. pause reset during early off. If a shutoff night
overlaps, the unit stays off in between. **End early off** turns a unit it
turned off back on at once (unless a shutoff night applies).

**Auto early off** (tuning settings, on by default, threshold 23.0 °C): when
the indoor temperature rises above the threshold while the unit is on, an early
off starts automatically and lasts until the room is below the indoor target
(the threshold must be above the target). After it ended, or after ending it
manually, it only starts again once the room has been below the indoor target.
It does not start while a shutoff night has the unit off.

## Quiet mode auto

"Auto settings" in the Quiet mode tile sets the quiet level from the outdoor
temperature reported by the unit (checked on every update):

- below the threshold (default 2 °C) quiet 0, above it quiet 3; exactly at the
  threshold the level is kept (the unit reports whole degrees).
- **Advanced**: above the threshold, quiet 3 after each compressor stop and back
  to 0 once the compressor runs and the water reaches target − K (default 2 K).
- **Quiet 0 after a defrost**: after a defrost, quiet stays 0 until the outdoor
  temperature reaches the set value (default 5 °C).

A quiet level changed elsewhere (Panasonic app, unit, the Set button) pauses the
automatic mode. At most 10 commands per hour.
