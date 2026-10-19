#!/usr/bin/env python3
"""W6 consumer：訂閱自己的 IoT 主題 → 檢查 device_id 在設備清單 → 用 reporter 權杖交給本機 API。

設定全從環境變數（/etc/inspection/app.env）讀；每則只印 `event_id=<id> result=<分類>`，不印權杖或整則訊息。
需相容主機上的 Python 3.9；paho-mqtt 2.1.0 由打包器裝在 /opt/inspection/vendor。
"""
import json
import os
import re
import ssl
import urllib.error
import urllib.request

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

EVENT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
API = "http://127.0.0.1:8080/events"


def classify(status):
    if status == 201:
        return "created"
    if status == 200:
        return "duplicate"
    if 400 <= status < 500:
        return "rejected"
    return "error"


def forward(event, token):
    """回傳 created／duplicate／rejected／error。"""
    req = urllib.request.Request(API, data=json.dumps(event).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return classify(resp.status)
    except urllib.error.HTTPError as exc:
        return classify(exc.code)
    except (urllib.error.URLError, OSError):
        return "error"


def handle(payload, devices, token):
    """處理一則訊息；回傳 (event_id, result)。先檢查設備清單，不在清單就不送 API。"""
    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "-", "rejected"
    if not isinstance(event, dict):
        return "-", "rejected"
    event_id = event.get("event_id")
    event_id = event_id if isinstance(event_id, str) and EVENT_ID.fullmatch(event_id) else "-"
    if event.get("device_id") not in devices:
        return event_id, "rejected"
    return event_id, forward(event, token)


def main():
    env = os.environ
    topic, token = env["IOT_TOPIC"], env["REPORTER_TOKEN"]
    devices = frozenset(d.strip() for d in env["IOT_DEVICE_IDS"].split(",") if d.strip())
    client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2, client_id=env["IOT_CLIENT_ID"],
                         protocol=mqtt.MQTTv5)
    client.tls_set(ca_certs=env["IOT_CA"], certfile=env["IOT_CERT"], keyfile=env["IOT_KEY"],
                   cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(client, userdata, flags, reason_code, properties):
        print(f"connack rc={reason_code.value}", flush=True)
        if not reason_code.is_failure:
            client.subscribe(topic, qos=1)  # 每次（重新）連線都重新訂閱

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        print(f"subscribed rc={reason_codes[0].value}", flush=True)

    def on_message(client, userdata, msg):
        event_id, result = handle(msg.payload, devices, token)
        print(f"event_id={event_id} result={result}", flush=True)

    client.on_connect, client.on_subscribe, client.on_message = on_connect, on_subscribe, on_message
    client.connect_async(env["IOT_ENDPOINT"], 8883, keepalive=60)
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
