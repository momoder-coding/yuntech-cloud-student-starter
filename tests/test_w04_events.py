"""W4 離線測試：事件契約、拒絕順序、測資與顯示頁；不呼叫 AWS。"""
import importlib.util
import json
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = sorted((ROOT / "tests" / "fixtures").glob("w04-*.json"))
spec = importlib.util.spec_from_file_location("w04_service", ROOT / "app/service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)

REPORTER = secrets.token_urlsafe(32)
OPERATOR = secrets.token_urlsafe(32)


class EventApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        version = Path(self.tmp.name) / "version"
        version.write_text("b" * 40)
        env = {"REPORTER_TOKEN": REPORTER, "OPERATOR_TOKEN": OPERATOR}
        self.server = service.make_server(version, port=0, env=env)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.tmp.cleanup()

    def call(self, method, path, token=None, body=None, content_type="application/json"):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        if data is not None:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def post(self, event, token=REPORTER, **kw):
        status, text = self.call("POST", "/events", token, event, **kw)
        return status, json.loads(text)

    def test_fixtures_are_read_and_match_expectations(self):
        self.assertEqual(len(FIXTURES), 3, "need 1 success + 2 rejection fixtures")
        expected = sorted(json.loads(p.read_text())["expect_status"] for p in FIXTURES)
        self.assertEqual(expected, [201, 400, 400])
        for path in FIXTURES:
            case = json.loads(path.read_text())
            with self.subTest(fixture=path.name):
                status, body = self.post(case["event"])
                self.assertEqual(status, case["expect_status"], body)
                if status == 201:
                    self.assertEqual(body["event_id"], case["event"]["event_id"])
                    self.assertTrue(body["received_at"].endswith("Z"))
                else:
                    self.assertEqual(body, {"error": body["error"], "field": case["expect_field"]})

    def test_rejection_matrix_order(self):
        ok = json.loads((ROOT / "tests/fixtures/w04-ok.json").read_text())["event"]
        no_tz = dict(ok, event_id="g06-m01-0900", observed_at="2026-10-19T22:00:00")
        rows = [
            (self.post(ok)[0], 201),
            (self.post(ok, token=None)[0], 401),
            (self.post(ok, token=OPERATOR)[0], 403),
            (self.post(no_tz), (400, "observed_at")),
            (self.post(ok)[0], 409),
            (self.call("GET", "/events", REPORTER)[0], 403),
        ]
        for got, want in rows:
            if isinstance(want, tuple):
                self.assertEqual((got[0], got[1]["field"]), want)
            else:
                self.assertEqual(got, want)
        status, text = self.call("GET", "/events", OPERATOR)
        self.assertEqual(status, 200)
        self.assertIn(ok["event_id"], [e["event_id"] for e in json.loads(text)["events"]])

    def test_authentication_before_validation(self):
        status, body = self.post({"event_id": "!!", "bogus": 1}, token=None)
        self.assertEqual((status, body["field"]), (401, "authorization"))
        status, body = self.post({"event_id": "!!"}, token="wrong-token-wrong-token")
        self.assertEqual(status, 401)
        status, body = self.post({"event_id": "!!"}, token=OPERATOR)
        self.assertEqual(status, 403)

    def test_validation_rules(self):
        ok = json.loads((ROOT / "tests/fixtures/w04-ok.json").read_text())["event"]
        cases = [
            (dict(ok, event_id="x" * 65), "event_id"),
            (dict(ok, device_id="bad id"), "device_id"),
            (dict(ok, observed_at="yesterday"), "observed_at"),
            (dict(ok, type="alarm"), "type"),
            (dict(ok, note=None), "note"),
            (dict(ok, note="n" * 201), "note"),
            ({k: v for k, v in ok.items() if k != "device_id"}, "device_id"),
            ([ok], "body"),
        ]
        for event, field in cases:
            with self.subTest(field=field):
                status, body = self.post(event)
                self.assertEqual((status, body["field"]), (400, field))
        status, body = self.post(ok, content_type="text/plain")
        self.assertEqual((status, body["field"]), (400, "content-type"))
        big = dict(ok, event_id="g06-m01-0901", note="n" * 200)
        status, body = self.post(json.dumps(big).encode() + b" " * 4096)
        self.assertEqual((status, body["field"]), (400, "body"))
        status, body = self.post(b"{not json")
        self.assertEqual((status, body["field"]), (400, "body"))

    def test_note_is_optional_and_lookup_by_id(self):
        ok = json.loads((ROOT / "tests/fixtures/w04-ok.json").read_text())["event"]
        event = {k: v for k, v in ok.items() if k != "note"}
        self.assertEqual(self.post(event)[0], 201)
        status, text = self.call("GET", "/events/" + ok["event_id"], OPERATOR)
        self.assertEqual(status, 200)
        self.assertNotIn("note", json.loads(text))
        self.assertEqual(self.call("GET", "/events/" + ok["event_id"], REPORTER)[0], 403)
        self.assertEqual(self.call("GET", "/events/g06-m01-none", OPERATOR)[0], 404)

    def test_tokens_never_echoed(self):
        ok = json.loads((ROOT / "tests/fixtures/w04-ok.json").read_text())["event"]
        for token in (REPORTER, OPERATOR, "guess-" + REPORTER):
            for method, path, body in (("POST", "/events", ok), ("GET", "/events", None), ("GET", "/health", None)):
                _, text = self.call(method, path, token, body)
                self.assertNotIn(REPORTER, text)
                self.assertNotIn(OPERATOR, text)
                if method == "POST" and json.loads(text).get("error"):
                    self.assertEqual(set(json.loads(text)), {"error", "field"})

    def test_health_reports_auth_and_page_is_safe(self):
        status, text = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertIs(json.loads(text)["auth_configured"], True)
        status, page = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("textContent", page)
        for banned in ("innerHTML", "localStorage", "sessionStorage", "?token="):
            self.assertNotIn(banned, page)

    def test_no_tokens_means_auth_not_configured(self):
        version = Path(self.tmp.name) / "version"
        server = service.make_server(version, port=0, env={})
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base = "http://127.0.0.1:" + str(server.server_port)
            with urllib.request.urlopen(base + "/health") as resp:
                self.assertIs(json.load(resp)["auth_configured"], False)
            req = urllib.request.Request(base + "/events", headers={"Authorization": "Bearer "})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req)
            self.assertEqual(caught.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
