#!/usr/bin/with-contenv bashio

export DEYE_ENTITY=$(bashio::config 'deye_entity')
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN}"
export HA_URL="http://supervisor/core"
export DATA_DIR=/data
export PORT="${INGRESS_PORT:-8099}"

cd /app
python3 main.py
