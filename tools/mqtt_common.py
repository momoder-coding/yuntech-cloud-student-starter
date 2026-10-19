"""W6：閘道／consumer 身分的 MQTT 5 連線（QoS 1、8883、雙向 TLS），給 mqtt_check 與 gateway_sim 共用。

憑證、私鑰、Root CA 與端點照 0-2 的檔名從 .local/iot/ 讀；不關閉 TLS 檢查。
"""
import json
from pathlib import Path
import ssl
import threading

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

ROOT = Path(__file__).resolve().parents[1]
IOT = ROOT / ".local" / "iot"


def default_code():
    cfg = json.loads((ROOT / ".local" / "config.json").read_text())
    return f"{cfg['group']}-{cfg['owner']}"


class Session:
    def __init__(self, role, code, on_recv=None):
        if role not in ("gw", "consumer"):
            raise ValueError("role must be gw or consumer")
        self.client_id = f"{role}-{code}"
        self.client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2, client_id=self.client_id,
                                  protocol=mqtt.MQTTv5)
        self.client.tls_set(ca_certs=str(IOT / "AmazonRootCA1.pem"), certfile=str(IOT / f"{role}.cert.pem"),
                            keyfile=str(IOT / f"{role}.private.key"), cert_reqs=ssl.CERT_REQUIRED,
                            tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self.connack = None
        self.connected = threading.Event()
        self.cv = threading.Condition()
        self.pubacks = {}
        self.subacks = {}
        self.on_recv = on_recv
        self.client.on_connect = self._on_connect
        self.client.on_publish = self._on_publish
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if self.connack is None:
            self.connack = reason_code.value
        self.connected.set()

    def _on_publish(self, client, userdata, mid, reason_code, properties):
        with self.cv:
            self.pubacks[mid] = reason_code.value
            self.cv.notify_all()

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        with self.cv:
            self.subacks[mid] = reason_codes[0].value
            self.cv.notify_all()

    def _on_message(self, client, userdata, msg):
        if self.on_recv:
            self.on_recv(msg.topic, msg.payload)

    def connect(self, timeout=20):
        endpoint = (IOT / "endpoint").read_text().strip()
        self.client.connect_async(endpoint, 8883, keepalive=30)
        self.client.loop_start()
        self.connected.wait(timeout)
        return self.connack

    def publish(self, topic, payload, timeout=20):
        """以 QoS 1 發布並等 PUBACK（用訊息編號核對）；回傳原因碼，逾時回 None。"""
        mid = self.client.publish(topic, payload, qos=1).mid
        with self.cv:
            self.cv.wait_for(lambda: mid in self.pubacks, timeout)
            return self.pubacks.get(mid)

    def subscribe(self, topic_filter, timeout=20):
        mid = self.client.subscribe(topic_filter, qos=1)[1]
        with self.cv:
            self.cv.wait_for(lambda: mid in self.subacks, timeout)
            return self.subacks.get(mid)

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()
