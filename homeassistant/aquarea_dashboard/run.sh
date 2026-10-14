#!/usr/bin/with-contenv bashio
set -e

# /config = app_config folder (visible as /addon_configs/<repo>_aquarea_dashboard)
# /data   = private persistent storage (login tokens)
ARGS=(
  --host 0.0.0.0
  --port 8099
  --allow-ip 172.30.32.2
  --shared-session
  --poll-minutes "$(bashio::config 'poll_minutes')"
  --history-file /config/history.csv
  --token-file /data/tokens.json
  --tuning-file /data/tuning.json
)

if bashio::config.true 'allow_control'; then
  bashio::log.warning "Control is ENABLED: the dashboard and its automatic functions can change the heat pump."
  ARGS+=(--allow-control)
fi
if bashio::config.true 'debug'; then
  ARGS+=(--debug)
fi
for sensor in $(bashio::config 'ha_sensors'); do
  ARGS+=(--ha-sensor "$sensor")
done
if bashio::config.has_value 'heat_curve'; then
  ARGS+=("--heat-curve=$(bashio::config 'heat_curve')")
fi

bashio::log.info "Starting Aquarea Dashboard..."
cd /app
exec /opt/venv/bin/python3 web.py "${ARGS[@]}"
