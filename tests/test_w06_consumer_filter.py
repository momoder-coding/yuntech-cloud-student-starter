"""W6 離線測試：consumer 先檢查設備清單、回應分類；GET /events?device_id= 篩選；不關閉 TLS 檢查。"""
import importlib.util
import json
from pathlib import Path
import re
import secrets
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    import paho.mqtt.client  # noqa: F401
except ImportError:  # CI 沒裝 paho：只測 consumer 的處理邏輯，給一個空殼
    paho = types.ModuleType("paho")
    paho.mqtt = types.ModuleType("paho.mqtt")
    paho.mqtt.client = types.ModuleType("paho.mqtt.client")
    paho.mqtt.enums = types.ModuleType("paho.mqtt.enums")
    paho.mqtt.enums.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    sys.modules.update({"paho": paho, "paho.mqtt": paho.mqtt, "paho.mqtt.client": paho.mqtt.client,
                        "paho.mqtt.enums": paho.mqtt.enums})

service = load("w06_service", ROOT / "app/service.py")
consumer = load("w06_consumer", ROOT / "app/consumer.py")
REPORTER, OPERATOR = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
DEVICES = frozenset(f"g06-m01-d{i:02d}" for i in range(1, 11))


class Server:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        version = Path(self.tmp.name) / "version"
        version.write_text("c" * 40)
        self.server = service.make_server(version, port=0,
                                          env={"REPORTER_TOKEN": REPORTER, "OPERATOR_TOKEN": OPERATOR})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()


def event(i, device="g06-m01-d01", run="r0"):
    return {"event_id": f"g06-m01-{run}-{device[-3:]}-{i:02d}", "device_id": device,
            "observed_at": "2026-10-19T22:00:00+08:00", "type": "status"}


class ConsumerTests(unittest.TestCase):
    def test_device_not_in_list_is_rejected_without_calling_api(self):
        calls = []
        original = consumer.forward
        consumer.forward = lambda ev, token: calls.append(ev) or "created"
        try:
            result = consumer.handle(json.dumps(event(1, "g06-m01-d99")).encode(), DEVICES, REPORTER)
            self.assertEqual(result, ("g06-m01-r0-d99-01", "rejected"))
            self.assertEqual(calls, [])
            self.assertEqual(consumer.handle(b"not json", DEVICES, REPORTER), ("-", "rejected"))
            self.assertEqual(consumer.handle(b"[1]", DEVICES, REPORTER), ("-", "rejected"))
            self.assertEqual(calls, [])
        finally:
            consumer.forward = original

    def test_classification_against_real_api(self):
        with Server() as srv:
            consumer.API = srv.base + "/events"
            ev = json.dumps(event(1)).encode()
            self.assertEqual(consumer.handle(ev, DEVICES, REPORTER)[1], "created")
            self.assertEqual(consumer.handle(ev, DEVICES, REPORTER)[1], "duplicate")
            bad = json.dumps(dict(event(2), observed_at="no tz")).encode()
            self.assertEqual(consumer.handle(bad, DEVICES, REPORTER)[1], "rejected")
        consumer.API = "http://127.0.0.1:9/events"  # 沒有服務 → error
        self.assertEqual(consumer.handle(json.dumps(event(3)).encode(), DEVICES, REPORTER)[1], "error")
        self.assertEqual([consumer.classify(s) for s in (201, 200, 400, 409, 401, 500, 503)],
                         ["created", "duplicate", "rejected", "rejected", "rejected", "error", "error"])

    def test_tls_checks_are_not_disabled(self):
        # 執行時才組字串（避免常數折疊後讓 README 的 grep 在 .pyc 裡命中本測試）
        banned = re.compile("".join(["tls_insecure_set", r"\(True\)", "|CERT_", "NONE"]))
        for folder in ("app", "tools", "tests"):
            for path in (ROOT / folder).rglob("*.py"):
                self.assertIsNone(banned.search(path.read_text()), path)


class FilterTests(unittest.TestCase):
    def get(self, base, path, token):
        req = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_filter_by_device_newest_first(self):
        with Server() as srv:
            consumer.API = srv.base + "/events"
            for i in range(1, 4):
                for device in ("g06-m01-d01", "g06-m01-d02"):
                    consumer.handle(json.dumps(event(i, device)).encode(), DEVICES, REPORTER)
            status, body = self.get(srv.base, "/events?device_id=g06-m01-d01", OPERATOR)
            self.assertEqual(status, 200)
            self.assertEqual([e["event_id"] for e in body["events"]],
                             ["g06-m01-r0-d01-03", "g06-m01-r0-d01-02", "g06-m01-r0-d01-01"])
            self.assertEqual(self.get(srv.base, "/events", OPERATOR)[1]["count"], 6)
            self.assertEqual(self.get(srv.base, "/events?device_id=g06-m01-d01", REPORTER)[0], 403)
            status, body = self.get(srv.base, "/events?device_id=bad%20id", OPERATOR)
            self.assertEqual((status, body["field"]), (400, "device_id"))
            with urllib.request.urlopen(srv.base + "/") as resp:
                page = resp.read().decode()
            self.assertIn('id="device"', page)
            self.assertIn("encodeURIComponent", page)


if __name__ == "__main__":
    unittest.main()
