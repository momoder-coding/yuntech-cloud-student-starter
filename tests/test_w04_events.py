"""W4 離線測試：事件契約、判斷順序、顯示頁。不需要 AWS、不需要網路。

測試會真的讀 tests/fixtures/ 的檔案；把成功那筆改壞，這裡就應該失敗。
"""
import importlib.util
import json
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
spec = importlib.util.spec_from_file_location("w04_service", ROOT / "app/service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)

REPORTER = "r" * 43
OPERATOR = "o" * 43


class ServiceFixture(unittest.TestCase):
    """起一個只綁 127.0.0.1 的服務，權杖用測試值。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        version = Path(self.tmp.name) / "version"
        version.write_text("b" * 40)
        self.server = service.make_server(version, port=0,
                                          tokens={"reporter": REPORTER, "operator": OPERATOR})
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.tmp.cleanup()

    def call(self, method, path, body=None, token=None, content_type="application/json"):
        data = None if body is None else (body if isinstance(body, bytes)
                                          else json.dumps(body).encode("utf-8"))
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", content_type)
        if token:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode())
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except json.JSONDecodeError:
                return error.code, raw.decode()

    def fixture(self, name):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class EventContract(ServiceFixture):
    def test_fixtures_are_the_source_of_truth(self):
        status, _ = self.call("POST", "/events", self.fixture("w04_valid.json"), REPORTER)
        self.assertEqual(status, 201)
        status, body = self.call("POST", "/events", self.fixture("w04_invalid_no_tz.json"), REPORTER)
        self.assertEqual((status, body["field"]), (400, "observed_at"))
        status, body = self.call("POST", "/events", self.fixture("w04_invalid_type.json"), REPORTER)
        self.assertEqual((status, body["field"]), (400, "type"))

    def test_a_broken_valid_fixture_must_fail(self):
        """把成功那筆的時區拿掉，同一個測資就應該被拒（證明測試真的讀了檔案）。"""
        broken = self.fixture("w04_valid.json")
        broken["observed_at"] = "2026-10-13T18:30:00"
        status, body = self.call("POST", "/events", broken, REPORTER)
        self.assertEqual((status, body["field"]), (400, "observed_at"))

    def test_health_reports_auth_configured(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertIs(body["auth_configured"], True)
        self.assertEqual(body["version"], "b" * 40)

    def test_401_before_400_when_token_missing(self):
        status, body = self.call("POST", "/events", {"observed_at": "bad"}, None)
        self.assertEqual((status, body["field"]), (401, "authorization"))

    def test_403_when_role_is_wrong(self):
        status, body = self.call("POST", "/events", self.fixture("w04_valid.json"), OPERATOR)
        self.assertEqual((status, body["field"]), (403, "authorization"))

    def test_created_event_gets_received_at(self):
        fixture = self.fixture("w04_valid.json")
        status, body = self.call("POST", "/events", fixture, REPORTER)
        self.assertEqual(status, 201)
        self.assertTrue(body["received_at"].endswith("Z"))
        self.assertEqual(body["event_id"], fixture["event_id"])
        self.assertNotIn("received_at", fixture)

    def test_duplicate_is_409(self):
        fixture = self.fixture("w04_valid.json")
        self.assertEqual(self.call("POST", "/events", fixture, REPORTER)[0], 201)
        status, body = self.call("POST", "/events", fixture, REPORTER)
        self.assertEqual((status, body["error"]), (409, "duplicate_event_id"))

    def test_extra_field_rejected(self):
        fixture = self.fixture("w04_valid.json")
        fixture["student_id"] = "secret"
        status, body = self.call("POST", "/events", fixture, REPORTER)
        self.assertEqual((status, body["field"]), (400, "student_id"))

    def test_note_must_be_string(self):
        fixture = self.fixture("w04_valid.json")
        fixture["note"] = None
        status, body = self.call("POST", "/events", fixture, REPORTER)
        self.assertEqual((status, body["field"]), (400, "note"))

    def test_body_limit(self):
        fixture = self.fixture("w04_valid.json")
        fixture["note"] = "x" * 5000
        status, body = self.call("POST", "/events", fixture, REPORTER)
        self.assertEqual((status, body["field"]), (400, "body"))

    def test_content_type_must_be_json(self):
        status, body = self.call("POST", "/events", b"event_id=x", REPORTER,
                                 content_type="text/plain")
        self.assertEqual((status, body["field"]), (400, "content_type"))

    def test_errors_do_not_echo_the_token(self):
        status, body = self.call("POST", "/events", {"observed_at": "bad"}, "wrong-token-value")
        rendered = json.dumps(body)
        self.assertEqual(status, 401)
        self.assertNotIn("wrong-token-value", rendered)


class ReadContract(ServiceFixture):
    def test_reporter_cannot_list(self):
        status, _ = self.call("GET", "/events", token=REPORTER)
        self.assertEqual(status, 403)

    def test_operator_lists_newest_first(self):
        first = self.fixture("w04_valid.json")
        second = dict(first, event_id="g06-m01-0004")
        self.assertEqual(self.call("POST", "/events", first, REPORTER)[0], 201)
        self.assertEqual(self.call("POST", "/events", second, REPORTER)[0], 201)
        status, body = self.call("GET", "/events", token=OPERATOR)
        self.assertEqual(status, 200)
        self.assertEqual([item["event_id"] for item in body["events"]],
                         ["g06-m01-0004", "g06-m01-0001"])

    def test_operator_gets_one_and_404_for_unknown(self):
        fixture = self.fixture("w04_valid.json")
        self.call("POST", "/events", fixture, REPORTER)
        status, body = self.call("GET", "/events/" + fixture["event_id"], token=OPERATOR)
        self.assertEqual((status, body["event_id"]), (200, fixture["event_id"]))
        status, body = self.call("GET", "/events/g06-m01-9999", token=OPERATOR)
        self.assertEqual((status, body["error"]), (404, "not_found"))


class DisplayPage(ServiceFixture):
    def test_page_is_safe(self):
        status, html = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("textContent", html)          # 事件內容用 textContent 放入頁面
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("localStorage", html)
        self.assertNotIn("?token=", html)
        self.assertIn("Authorization", html)


if __name__ == "__main__":
    unittest.main()
