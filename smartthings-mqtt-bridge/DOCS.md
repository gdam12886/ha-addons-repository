# Home Assistant Add-on: SmartThings MQTT Bridge

Bridge Samsung SmartThings devices to MQTT topics so Home Assistant can consume state and send commands.

## Features

- Polls SmartThings device status and publishes retained MQTT state topics.
- Auto-discovers entities for all scalar SmartThings attributes across all components.
- Publishes every SmartThings attribute (including objects/lists) to dedicated MQTT topics.
- Subscribes to MQTT command topics and forwards commands to SmartThings.
- Optional Home Assistant MQTT Discovery for common capabilities.

## Installation

1. Add this repository to Home Assistant add-on store:
`https://github.com/gdam12886/ha-addons-repository`
2. Install **SmartThings MQTT Bridge**.
3. Start the add-on and check logs.

## Configuration

Example:

```yaml
smartthings_token: "your-smartthings-personal-access-token"
smartthings_api_base: "https://api.smartthings.com/v1"
mqtt_host: "core-mosquitto"
mqtt_port: 1883
mqtt_user: ""
mqtt_password: ""
mqtt_topic_prefix: "smartthings"
poll_interval_seconds: 30
publish_discovery: true
```

### Option: `smartthings_token`

SmartThings personal access token with access to your devices.
Create one at: `https://account.smartthings.com/tokens`

### Option: `smartthings_api_base`

SmartThings API base URL. Keep default unless needed.

### Option: `mqtt_host`, `mqtt_port`, `mqtt_user`, `mqtt_password`

MQTT broker connection settings.

### Option: `mqtt_topic_prefix`

Base topic used by this bridge. Default is `smartthings`.

### Option: `poll_interval_seconds`

How often device status is refreshed from SmartThings API.

### Option: `publish_discovery`

If `true`, publishes Home Assistant MQTT discovery config for supported capabilities.

## MQTT Topics

For each SmartThings device ID `<device_id>`:

- State: `<prefix>/<device_id>/state` (JSON, retained)
- Availability: `<prefix>/<device_id>/availability` (`online` / `offline`, retained)
- Attribute state: `<prefix>/<device_id>/<component>/<capability>/<attribute>/state`
- Simple command: `<prefix>/<device_id>/set`
- Advanced command JSON: `<prefix>/<device_id>/command`
- Capability command: `<prefix>/<device_id>/<component>/<capability>/set`

Simple command payload examples:

- `on` / `off` (switch)
- `lock` / `unlock`
- `open` / `close`

Advanced command payload example:

```json
{
  "commands": [
    {
      "component": "main",
      "capability": "switch",
      "command": "on"
    }
  ]
}
```

Capability command examples:

- Topic: `smartthings/<device_id>/main/switch/set` payload: `on`
- Topic: `smartthings/<device_id>/main/lock/set` payload: `lock`
- Topic: `smartthings/<device_id>/main/switchLevel/set` payload: `{"command":"setLevel","arguments":[50]}`

From your examples, these are now directly supported via capability topics:

- `main/audioVolume/set` with payload `12` (maps to `setVolume`)
- `main/audioMute/set` with payload `mute` or `unmute`
- `main/mediaInputSource/set` with payload `HDMI2` (maps to `setInputSource`)
- `main/custom.picturemode/set` with payload `Movie` (maps to `setPictureMode`)
- `main/custom.soundmode/set` with payload `Standard` (maps to `setSoundMode`)
- `main/samsungvd.pictureMode/set` with payload `Eco` (maps to `setPictureMode`)
- `main/samsungvd.soundMode/set` with payload `Standard` (maps to `setSoundMode`)

## Notes

- SmartThings cloud APIs are rate-limited. Use a reasonable polling interval.
- Some device capabilities may need custom command payloads on the `/command` topic.
