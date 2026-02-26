import json
import os
import re
import time
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt
import requests


def env(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    return value.strip() if isinstance(value, str) else default


def env_bool(name: str, default: bool = False) -> bool:
    value = env(name, str(default)).lower()
    return value in {"1", "true", "yes", "on"}


ST_TOKEN = env("ST_TOKEN")
ST_API_BASE = env("ST_API_BASE", "https://api.smartthings.com/v1").rstrip("/")
MQTT_HOST = env("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(env("MQTT_PORT", "1883"))
MQTT_USER = env("MQTT_USER")
MQTT_PASSWORD = env("MQTT_PASSWORD")
TOPIC_PREFIX = env("MQTT_TOPIC_PREFIX", "smartthings").strip("/")
POLL_INTERVAL = max(5, int(env("POLL_INTERVAL_SECONDS", "30")))
PUBLISH_DISCOVERY = env_bool("PUBLISH_DISCOVERY", True)

HTTP_TIMEOUT = 20
AVAIL_ONLINE = "online"
AVAIL_OFFLINE = "offline"

SESSION = requests.Session()
SESSION.headers.update(
    {
        "Authorization": f"Bearer {ST_TOKEN}",
        "Content-Type": "application/json",
    }
)

KNOWN_DEVICES: Dict[str, Dict[str, Any]] = {}
LAST_STATE: Dict[str, str] = {}
LAST_ATTR_STATE: Dict[str, str] = {}
DISCOVERY_PUBLISHED: set = set()


def log(level: str, message: str) -> None:
    print(f"[{level}] {message}", flush=True)


def st_get(path: str) -> Dict[str, Any]:
    url = f"{ST_API_BASE}{path}"
    response = SESSION.get(url, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json()


def st_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{ST_API_BASE}{path}"
    response = SESSION.post(url, data=json.dumps(payload), timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    if response.text:
        return response.json()
    return {}


def fetch_devices() -> List[Dict[str, Any]]:
    payload = st_get("/devices")
    return payload.get("items", [])


def fetch_device_status(device_id: str) -> Dict[str, Any]:
    return st_get(f"/devices/{device_id}/status")


def sanitize_id(value: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", value.lower())


def extract_attributes(status: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    components = status.get("components", {})
    for component, component_payload in components.items():
        if not isinstance(component_payload, dict):
            continue
        for capability, capability_payload in component_payload.items():
            if not isinstance(capability_payload, dict):
                continue
            for attribute, attribute_payload in capability_payload.items():
                if not isinstance(attribute_payload, dict):
                    continue
                key = f"{component}.{capability}.{attribute}"
                result[key] = {
                    "component": component,
                    "capability": capability,
                    "attribute": attribute,
                    "value": attribute_payload.get("value"),
                    "unit": attribute_payload.get("unit"),
                }
    return result


def command_topic(device_id: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/command"


def set_topic(device_id: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/set"


def capability_set_topic(device_id: str, component: str, capability: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/{component}/{capability}/set"


def state_topic(device_id: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/state"


def attribute_state_topic(
    device_id: str, component: str, capability: str, attribute: str
) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/{component}/{capability}/{attribute}/state"


def availability_topic(device_id: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/availability"


def mqtt_discovery_topic(entity_domain: str, object_id: str) -> str:
    return f"homeassistant/{entity_domain}/{object_id}/config"


def publish_discovery_config(
    client: mqtt.Client,
    device_id: str,
    device: Dict[str, Any],
    attrs: Dict[str, Dict[str, Any]],
) -> None:
    if not PUBLISH_DISCOVERY:
        return

    label = device.get("label") or device.get("name") or device_id
    manufacturer = device.get("manufacturerName") or "Samsung"
    model = device.get("deviceTypeName") or "SmartThings Device"
    sw = device.get("firmwareVersion") or ""
    identifiers = [f"smartthings_{device_id}"]
    device_meta = {
        "identifiers": identifiers,
        "name": label,
        "manufacturer": manufacturer,
        "model": model,
        "sw_version": sw,
    }

    def pub(entity_domain: str, suffix: str, payload: Dict[str, Any]) -> None:
        object_id = f"smartthings_{device_id}_{suffix}"
        if object_id in DISCOVERY_PUBLISHED:
            return
        topic = mqtt_discovery_topic(entity_domain, object_id)
        payload["device"] = device_meta
        payload["availability_topic"] = availability_topic(device_id)
        client.publish(topic, json.dumps(payload), retain=True)
        DISCOVERY_PUBLISHED.add(object_id)

    binary_map = {
        "contact": ("open", "closed", "door"),
        "motion": ("active", "inactive", "motion"),
        "water": ("wet", "dry", "moisture"),
        "presence": ("present", "not present", "presence"),
        "occupancy": ("occupied", "unoccupied", "occupancy"),
        "smoke": ("detected", "clear", "smoke"),
    }

    handled_keys: set = set()

    def attr_value_template(attr_key: str) -> str:
        return "{{ value_json['" + attr_key + "'] }}"

    def publish_switch(
        component: str,
        capability: str,
        attribute: str,
        payload_on: str,
        payload_off: str,
        state_on: str,
        state_off: str,
        label_suffix: str,
    ) -> None:
        attr_key = f"{component}.{capability}.{attribute}"
        if attr_key not in attrs:
            return
        info = attrs[attr_key]
        if info.get("value") is None:
            return
        safe_suffix = sanitize_id(attr_key)
        handled_keys.add(attr_key)
        pub(
            "switch",
            safe_suffix,
            {
                "name": f"{label} {label_suffix}",
                "state_topic": state_topic(device_id),
                "command_topic": capability_set_topic(device_id, component, capability),
                "state_value_template": attr_value_template(attr_key),
                "payload_on": payload_on,
                "payload_off": payload_off,
                "state_on": state_on,
                "state_off": state_off,
                "unique_id": f"smartthings_{device_id}_{safe_suffix}",
            },
        )

    def publish_number(
        component: str,
        capability: str,
        attribute: str,
        label_suffix: str,
        command: str,
        min_v: int = 0,
        max_v: int = 100,
        step: int = 1,
    ) -> None:
        attr_key = f"{component}.{capability}.{attribute}"
        info = attrs.get(attr_key)
        if not info:
            return
        value = info.get("value")
        if not isinstance(value, (int, float)):
            return
        safe_suffix = sanitize_id(attr_key)
        handled_keys.add(attr_key)
        payload: Dict[str, Any] = {
            "name": f"{label} {label_suffix}",
            "state_topic": state_topic(device_id),
            "command_topic": capability_set_topic(device_id, component, capability),
            "value_template": attr_value_template(attr_key),
            "command_template": (
                '{"command":"'
                + command
                + '","arguments":[{{ value | float }}]}'
            ),
            "min": min_v,
            "max": max_v,
            "step": step,
            "mode": "slider",
            "unique_id": f"smartthings_{device_id}_{safe_suffix}",
        }
        unit = info.get("unit")
        if unit:
            payload["unit_of_measurement"] = unit
        pub("number", safe_suffix, payload)

    def publish_select(
        component: str,
        capability: str,
        attribute: str,
        supported_attribute: str,
        label_suffix: str,
        command: str,
    ) -> None:
        attr_key = f"{component}.{capability}.{attribute}"
        supported_key = f"{component}.{capability}.{supported_attribute}"
        info = attrs.get(attr_key)
        supported = attrs.get(supported_key)
        if not info or not supported:
            return
        current = info.get("value")
        options = supported.get("value")
        if not isinstance(current, str) or not isinstance(options, list):
            return
        options = [opt for opt in options if isinstance(opt, str) and opt]
        if not options:
            return
        safe_suffix = sanitize_id(attr_key)
        handled_keys.add(attr_key)
        pub(
            "select",
            safe_suffix,
            {
                "name": f"{label} {label_suffix}",
                "state_topic": state_topic(device_id),
                "command_topic": capability_set_topic(device_id, component, capability),
                "value_template": attr_value_template(attr_key),
                "options": options,
                "command_template": (
                    '{"command":"'
                    + command
                    + '","arguments":["{{ value }}"]}'
                ),
                "unique_id": f"smartthings_{device_id}_{safe_suffix}",
            },
        )

    # Control entities for known command-capable capabilities.
    publish_switch("main", "switch", "switch", "on", "off", "on", "off", "Power")
    publish_switch("main", "audioMute", "mute", "mute", "unmute", "muted", "unmuted", "Mute")
    publish_number("main", "audioVolume", "volume", "Volume", "setVolume", 0, 100, 1)
    publish_number("main", "switchLevel", "level", "Level", "setLevel", 0, 100, 1)
    publish_select(
        "main",
        "mediaInputSource",
        "inputSource",
        "supportedInputSources",
        "Input Source",
        "setInputSource",
    )
    publish_select(
        "main",
        "samsungvd.mediaInputSource",
        "inputSource",
        "supportedInputSources",
        "Input Source",
        "setInputSource",
    )
    publish_select(
        "main",
        "custom.picturemode",
        "pictureMode",
        "supportedPictureModes",
        "Picture Mode",
        "setPictureMode",
    )
    publish_select(
        "main",
        "samsungvd.pictureMode",
        "pictureMode",
        "supportedPictureModes",
        "Picture Mode",
        "setPictureMode",
    )
    publish_select(
        "main",
        "custom.soundmode",
        "soundMode",
        "supportedSoundModes",
        "Sound Mode",
        "setSoundMode",
    )
    publish_select(
        "main",
        "samsungvd.soundMode",
        "soundMode",
        "supportedSoundModes",
        "Sound Mode",
        "setSoundMode",
    )
    publish_select(
        "main",
        "ovenMode",
        "ovenMode",
        "supportedOvenModes",
        "Oven Mode",
        "setOvenMode",
    )
    publish_select(
        "main",
        "samsungce.ovenMode",
        "ovenMode",
        "supportedOvenModes",
        "Oven Mode",
        "setOvenMode",
    )

    for attr_key, info in attrs.items():
        component = info["component"]
        capability = info["capability"]
        attribute = info["attribute"]
        value = info.get("value")
        unit = info.get("unit")
        if attr_key in handled_keys:
            continue

        # MQTT discovery entities require scalar state.
        if isinstance(value, (list, dict)) or value is None:
            continue

        safe_suffix = sanitize_id(attr_key)
        value_template = "{{ value_json['" + attr_key + "'] }}"
        unique = f"smartthings_{device_id}_{safe_suffix}"
        name = f"{label} {component} {capability} {attribute}"

        if capability == "switch" and attribute == "switch" and str(value).lower() in {"on", "off"}:
            pub(
                "switch",
                safe_suffix,
                {
                    "name": name,
                    "state_topic": state_topic(device_id),
                    "command_topic": capability_set_topic(device_id, component, capability),
                    "state_value_template": value_template,
                    "payload_on": "on",
                    "payload_off": "off",
                    "state_on": "on",
                    "state_off": "off",
                    "unique_id": unique,
                },
            )
            continue

        if capability == "lock" and attribute in {"lock", "lockState"}:
            pub(
                "lock",
                safe_suffix,
                {
                    "name": name,
                    "state_topic": state_topic(device_id),
                    "command_topic": capability_set_topic(device_id, component, capability),
                    "value_template": value_template,
                    "state_locked": "locked",
                    "state_unlocked": "unlocked",
                    "payload_lock": "lock",
                    "payload_unlock": "unlock",
                    "unique_id": unique,
                },
            )
            continue

        if isinstance(value, bool):
            pub(
                "binary_sensor",
                safe_suffix,
                {
                    "name": name,
                    "state_topic": state_topic(device_id),
                    "value_template": value_template,
                    "payload_on": True,
                    "payload_off": False,
                    "unique_id": unique,
                },
            )
            continue

        key = attribute.lower()
        if isinstance(value, str) and key in binary_map:
            payload_on, payload_off, device_class = binary_map[key]
            pub(
                "binary_sensor",
                safe_suffix,
                {
                    "name": name,
                    "state_topic": state_topic(device_id),
                    "value_template": value_template,
                    "payload_on": payload_on,
                    "payload_off": payload_off,
                    "device_class": device_class,
                    "unique_id": unique,
                },
            )
            continue

        payload: Dict[str, Any] = {
            "name": name,
            "state_topic": state_topic(device_id),
            "value_template": value_template,
            "unique_id": unique,
        }
        if isinstance(value, (int, float)):
            payload["state_class"] = "measurement"
        if unit:
            payload["unit_of_measurement"] = unit
        if capability == "temperatureMeasurement":
            payload["device_class"] = "temperature"
        elif capability == "relativeHumidityMeasurement":
            payload["device_class"] = "humidity"
        elif capability == "battery":
            payload["device_class"] = "battery"
        elif capability == "illuminanceMeasurement":
            payload["device_class"] = "illuminance"
            if not unit:
                payload["unit_of_measurement"] = "lx"
        pub("sensor", safe_suffix, payload)


def infer_command(payload: str) -> Optional[Dict[str, Any]]:
    normalized = payload.strip().lower()
    if normalized in {"on", "off"}:
        return {
            "commands": [
                {"component": "main", "capability": "switch", "command": normalized}
            ]
        }
    if normalized in {"lock", "unlock"}:
        return {
            "commands": [
                {"component": "main", "capability": "lock", "command": normalized}
            ]
        }
    if normalized in {"open", "close"}:
        return {
            "commands": [
                {
                    "component": "main",
                    "capability": "doorControl",
                    "command": normalized,
                }
            ]
        }
    return None


def parse_command_payload(payload: bytes) -> Optional[Dict[str, Any]]:
    text = payload.decode("utf-8", errors="ignore").strip()
    if not text:
        return None
    try:
        raw = json.loads(text)
        if isinstance(raw, dict) and "commands" in raw:
            return raw
        if isinstance(raw, dict) and {"capability", "command"} <= set(raw.keys()):
            if "component" not in raw:
                raw["component"] = "main"
            return {"commands": [raw]}
    except json.JSONDecodeError:
        pass
    return infer_command(text)


def parse_capability_command_payload(
    payload: bytes, component: str, capability: str
) -> Optional[Dict[str, Any]]:
    text = payload.decode("utf-8", errors="ignore").strip()
    if not text:
        return None

    command: Dict[str, Any] = {"component": component, "capability": capability}
    text_command_map = {
        "switch": {
            "on": "on",
            "off": "off",
        },
        "lock": {
            "lock": "lock",
            "unlock": "unlock",
            "locked": "lock",
            "unlocked": "unlock",
        },
        "doorControl": {
            "open": "open",
            "close": "close",
            "closed": "close",
        },
        "audioMute": {
            "mute": "mute",
            "unmute": "unmute",
            "muted": "mute",
            "unmuted": "unmute",
            "on": "mute",
            "off": "unmute",
        },
    }
    argument_command_map = {
        "audioVolume": "setVolume",
        "switchLevel": "setLevel",
        "mediaInputSource": "setInputSource",
        "samsungvd.mediaInputSource": "setInputSource",
        "custom.picturemode": "setPictureMode",
        "samsungvd.pictureMode": "setPictureMode",
        "custom.soundmode": "setSoundMode",
        "samsungvd.soundMode": "setSoundMode",
        "ovenMode": "setOvenMode",
        "samsungce.ovenMode": "setOvenMode",
    }
    try:
        raw = json.loads(text)
        if isinstance(raw, dict):
            if "commands" in raw:
                return raw
            if "command" in raw:
                command["command"] = raw["command"]
                if "arguments" in raw:
                    command["arguments"] = raw["arguments"]
                return {"commands": [command]}
        if isinstance(raw, (int, float)):
            command["command"] = "setLevel"
            command["arguments"] = [raw]
            return {"commands": [command]}
    except json.JSONDecodeError:
        pass

    normalized = text.strip().lower()
    if capability in text_command_map and normalized in text_command_map[capability]:
        command["command"] = text_command_map[capability][normalized]
        return {"commands": [command]}
    if capability in argument_command_map:
        command["command"] = argument_command_map[capability]
        try:
            num_value: Any = float(text) if "." in text else int(text)
            command["arguments"] = [num_value]
        except ValueError:
            command["arguments"] = [text]
        return {"commands": [command]}

    command["command"] = text
    return {"commands": [command]}


def extract_device_id(topic: str) -> Optional[str]:
    parts = topic.split("/")
    if len(parts) < 3:
        return None
    if parts[0] != TOPIC_PREFIX:
        return None
    return parts[1]


def send_command(device_id: str, command_payload: Dict[str, Any]) -> bool:
    try:
        st_post(f"/devices/{device_id}/commands", command_payload)
        log("INFO", f"Command sent to device {device_id}: {command_payload}")
        return True
    except requests.HTTPError as err:
        body = ""
        if err.response is not None:
            body = err.response.text
        log("ERROR", f"SmartThings command failed for {device_id}: {err} {body}")
        return False
    except Exception as err:
        log("ERROR", f"Unexpected command error for {device_id}: {err}")
        return False


def on_connect(client: mqtt.Client, _userdata: Any, _flags: Dict[str, Any], rc: int) -> None:
    if rc != 0:
        log("ERROR", f"Failed to connect to MQTT broker (rc={rc})")
        return
    log("INFO", f"Connected to MQTT broker at {MQTT_HOST}:{MQTT_PORT}")
    client.subscribe(f"{TOPIC_PREFIX}/+/set")
    client.subscribe(f"{TOPIC_PREFIX}/+/command")
    client.subscribe(f"{TOPIC_PREFIX}/+/+/+/set")


def on_message(client: mqtt.Client, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
    parts = msg.topic.split("/")
    payload: Optional[Dict[str, Any]] = None
    device_id: Optional[str] = None

    if len(parts) == 5 and parts[0] == TOPIC_PREFIX and parts[4] == "set":
        device_id = parts[1]
        component = parts[2]
        capability = parts[3]
        payload = parse_capability_command_payload(msg.payload, component, capability)
    else:
        device_id = extract_device_id(msg.topic)
        if device_id:
            payload = parse_command_payload(msg.payload)

    if not device_id:
        return
    if not payload:
        log("WARNING", f"Ignoring empty or invalid command on {msg.topic}")
        return
    if send_command(device_id, payload):
        # Refresh status quickly after a command to reduce stale state in HA.
        publish_device_state(client, device_id, force=True)


def publish_device_state(client: mqtt.Client, device_id: str, force: bool = False) -> None:
    try:
        device = KNOWN_DEVICES.get(device_id, {})
        status = fetch_device_status(device_id)
        attrs = extract_attributes(status)
        state_payload = {
            "device_id": device_id,
            "name": device.get("label") or device.get("name") or device_id,
            "updated_at": int(time.time()),
        }
        for key, info in attrs.items():
            value = info.get("value")
            state_payload[key] = value
            # Keep backward-compatible keys for main component entries.
            if info.get("component") == "main":
                legacy_key = f"{info.get('capability')}.{info.get('attribute')}"
                if legacy_key not in state_payload:
                    state_payload[legacy_key] = value
        encoded = json.dumps(state_payload, separators=(",", ":"), sort_keys=True)
        if force or LAST_STATE.get(device_id) != encoded:
            client.publish(state_topic(device_id), encoded, retain=True)
            for info in attrs.values():
                component = info["component"]
                capability = info["capability"]
                attribute = info["attribute"]
                value = info.get("value")
                attr_topic = attribute_state_topic(
                    device_id, component, capability, attribute
                )
                attr_encoded = json.dumps(value, separators=(",", ":"), sort_keys=True)
                attr_cache_key = f"{device_id}|{component}|{capability}|{attribute}"
                if force or LAST_ATTR_STATE.get(attr_cache_key) != attr_encoded:
                    client.publish(attr_topic, attr_encoded, retain=True)
                    LAST_ATTR_STATE[attr_cache_key] = attr_encoded
            client.publish(availability_topic(device_id), AVAIL_ONLINE, retain=True)
            LAST_STATE[device_id] = encoded
            publish_discovery_config(client, device_id, device, attrs)
    except requests.HTTPError as err:
        body = ""
        if err.response is not None:
            body = err.response.text
        log("ERROR", f"Unable to fetch status for {device_id}: {err} {body}")
        client.publish(availability_topic(device_id), AVAIL_OFFLINE, retain=True)
    except Exception as err:
        log("ERROR", f"Unexpected status error for {device_id}: {err}")
        client.publish(availability_topic(device_id), AVAIL_OFFLINE, retain=True)


def refresh_devices() -> List[str]:
    ids: List[str] = []
    devices = fetch_devices()
    for item in devices:
        device_id = item.get("deviceId")
        if not device_id:
            continue
        KNOWN_DEVICES[device_id] = item
        ids.append(device_id)
    return ids


def build_mqtt_client() -> mqtt.Client:
    client = mqtt.Client()
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD or None)
    client.on_connect = on_connect
    client.on_message = on_message
    return client


def main() -> None:
    if not ST_TOKEN:
        raise RuntimeError("ST_TOKEN is required")

    log("INFO", "Validating SmartThings API access")
    initial_devices = refresh_devices()
    log("INFO", f"Found {len(initial_devices)} SmartThings devices")

    client = build_mqtt_client()
    client.will_set(f"{TOPIC_PREFIX}/bridge/status", AVAIL_OFFLINE, retain=True)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    client.publish(f"{TOPIC_PREFIX}/bridge/status", AVAIL_ONLINE, retain=True)

    try:
        while True:
            try:
                for device_id in refresh_devices():
                    publish_device_state(client, device_id)
            except requests.HTTPError as err:
                body = ""
                if err.response is not None:
                    body = err.response.text
                log("ERROR", f"SmartThings poll failed: {err} {body}")
            except Exception as err:
                log("ERROR", f"Unexpected polling failure: {err}")
            time.sleep(POLL_INTERVAL)
    finally:
        client.publish(f"{TOPIC_PREFIX}/bridge/status", AVAIL_OFFLINE, retain=True)
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
