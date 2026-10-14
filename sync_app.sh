#!/bin/sh
# Copy the dashboard code into the Home Assistant app folder (its Docker build context).
set -eu
cd "$(dirname "$0")"
app=homeassistant/aquarea_dashboard/app
for f in "$app"/*.py; do cp "$(basename "$f")" "$app/"; done
cp static/* "$app/static/"
grep '^version' homeassistant/aquarea_dashboard/config.yaml
