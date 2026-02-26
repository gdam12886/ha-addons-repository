import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

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


def extract_main_attributes(status: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    main = status.get("components", {}).get("main", {})
    for capability, capability_payload in main.items():
        for attribute, attribute_payload in capability_payload.items():
            key = f"{capability}.{attribute}"
            result[key] = attribute_payload.get("value")
    return result


def command_topic(device_id: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/command"


def set_topic(device_id: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/set"


def state_topic(device_id: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/state"


def availability_topic(device_id: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/availability"


def mqtt_discovery_topic(entity_domain: str, object_id: str) -> str:
    return f"homeassistant/{entity_domain}/{object_id}/config"


def publish_discovery_config(
    client: mqtt.Client, device_id: str, device: Dict[str, Any], attrs: Dict[str, Any]
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

    if "switch.switch" in attrs:
        pub(
            "switch",
            "switch",
            {
                "name": f"{label} Switch",
                "state_topic": state_topic(device_id),
                "command_topic": set_topic(device_id),
                "state_value_template": "{{ value_json['switch.switch'] }}",
                "payload_on": "on",
                "payload_off": "off",
                "unique_id": f"smartthings_{device_id}_switch",
            },
        )

    if "temperatureMeasurement.temperature" in attrs:
        pub(
            "sensor",
            "temperature",
            {
                "name": f"{label} Temperature",
                "state_topic": state_topic(device_id),
                "value_template": "{{ value_json['temperatureMeasurement.temperature'] }}",
                "unit_of_measurement": "C",
                "device_class": "temperature",
                "state_class": "measurement",
                "unique_id": f"smartthings_{device_id}_temperature",
            },
        )

    if "relativeHumidityMeasurement.humidity" in attrs:
        pub(
            "sensor",
            "humidity",
            {
                "name": f"{label} Humidity",
                "state_topic": state_topic(device_id),
                "value_template": "{{ value_json['relativeHumidityMeasurement.humidity'] }}",
                "unit_of_measurement": "%",
                "device_class": "humidity",
                "state_class": "measurement",
                "unique_id": f"smartthings_{device_id}_humidity",
            },
        )

    if "contactSensor.contact" in attrs:
        pub(
            "binary_sensor",
            "contact",
            {
                "name": f"{label} Contact",
                "state_topic": state_topic(device_id),
                "value_template": "{{ value_json['contactSensor.contact'] }}",
                "payload_on": "open",
                "payload_off": "closed",
                "device_class": "door",
                "unique_id": f"smartthings_{device_id}_contact",
            },
        )

    if "motionSensor.motion" in attrs:
        pub(
            "binary_sensor",
            "motion",
            {
                "name": f"{label} Motion",
                "state_topic": state_topic(device_id),
                "value_template": "{{ value_json['motionSensor.motion'] }}",
                "payload_on": "active",
                "payload_off": "inactive",
                "device_class": "motion",
                "unique_id": f"smartthings_{device_id}_motion",
            },
        )

    if "battery.battery" in attrs:
        pub(
            "sensor",
            "battery",
            {
                "name": f"{label} Battery",
                "state_topic": state_topic(device_id),
                "value_template": "{{ value_json['battery.battery'] }}",
                "unit_of_measurement": "%",
                "device_class": "battery",
                "state_class": "measurement",
                "unique_id": f"smartthings_{device_id}_battery",
            },
        )


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


def on_message(client: mqtt.Client, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
    device_id = extract_device_id(msg.topic)
    if not device_id:
        return
    payload = parse_command_payload(msg.payload)
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
        attrs = extract_main_attributes(status)
        state_payload = {
            "device_id": device_id,
            "name": device.get("label") or device.get("name") or device_id,
            "updated_at": int(time.time()),
        }
        state_payload.update(attrs)
        encoded = json.dumps(state_payload, separators=(",", ":"), sort_keys=True)
        if force or LAST_STATE.get(device_id) != encoded:
            client.publish(state_topic(device_id), encoded, retain=True)
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
