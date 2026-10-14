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
| `poll_minutes` | Update interval (1–1440, default 1). Energy data is refreshed at most every 10 minutes. |
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
  down* at night or when the indoor sensor is above the indoor target. While
  the compressor is off this also happens later, as soon as the night starts
  or the room gets warmer than the target.
- When the compressor starts again, the shift returns to normal (also later
  while it runs, should that command have been skipped or failed).
- **Min. compressor run** (default 120 min, 0 = off): until a run has lasted
  this long, the shift also goes up near thermo off when no heat is needed
  (night, warm room), never above *normal + max. shift up* (so a run can still
  end earlier). After that the shift returns to normal and the compressor stops
  on its own.
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
the night with its trend over the last 90 min, is still at or above the indoor
target. A rising trend counts as flat. This only applies while the compressor
is off, and the trend only uses readings from 30 min after its stop (heating
skews them); with less than 45 min of such readings no projection is made, so
the earliest decision is 75 min after a stop.

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
