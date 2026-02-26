#!/usr/bin/with-contenv bashio
set -euo pipefail

bashio::log.info "Starting SmartThings MQTT Bridge"

bashio::config.require 'smartthings_token'
bashio::config.require 'mqtt_host'
bashio::config.require 'mqtt_topic_prefix'

export ST_TOKEN
export ST_API_BASE
export MQTT_HOST
export MQTT_PORT
export MQTT_USER
export MQTT_PASSWORD
export MQTT_TOPIC_PREFIX
export POLL_INTERVAL_SECONDS
export PUBLISH_DISCOVERY

ST_TOKEN="$(bashio::config 'smartthings_token')"
ST_API_BASE="$(bashio::config 'smartthings_api_base')"
MQTT_HOST="$(bashio::config 'mqtt_host')"
MQTT_PORT="$(bashio::config 'mqtt_port')"
MQTT_USER="$(bashio::config 'mqtt_user')"
MQTT_PASSWORD="$(bashio::config 'mqtt_password')"
MQTT_TOPIC_PREFIX="$(bashio::config 'mqtt_topic_prefix')"
POLL_INTERVAL_SECONDS="$(bashio::config 'poll_interval_seconds')"
PUBLISH_DISCOVERY="$(bashio::config 'publish_discovery')"

exec python3 /app/app.py
