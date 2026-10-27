#!/usr/bin/with-contenv bashio
set -e

# /config = app_config folder (visible as /addon_configs/<repo>_aquarea_dashboard)
# /data   = private persistent storage (tuning, energy, thermo state)

# MQTT broker from the Mosquitto app (services: mqtt:need)
export MQTT_HOST="$(bashio::services mqtt 'host')"
export MQTT_PORT="$(bashio::services mqtt 'port')"
export MQTT_USER="$(bashio::services mqtt 'username')"
export MQTT_PASSWORD="$(bashio::services mqtt 'password')"

ARGS=(
  --host 0.0.0.0
  --port 8099
  --allow-ip 172.30.32.2
  --poll-minutes "$(bashio::config 'poll_minutes')"
  --history-file /config/history.csv
  --tuning-file /data/tuning.json
  --mqtt-base-topic "$(bashio::config 'mqtt_base_topic')"
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

bashio::log.info "Starting Aquarea Dashboard (MQTT ${MQTT_HOST}:${MQTT_PORT})..."
cd /app
exec /opt/venv/bin/python3 web.py "${ARGS[@]}"
