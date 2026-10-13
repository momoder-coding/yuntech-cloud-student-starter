#!/usr/bin/env python3
"""W3/W4 inspection service.

Routes
    GET  /health            任何人      200，含 auth_configured
    GET  /                  任何人      顯示頁（權杖只在頁面變數，不進網址）
    POST /events            reporter    201／401／403／400／409
    GET  /events            operator    200，最新 50 筆
    GET  /events/{event_id} operator    200／404

POST 的判斷順序固定：401 → 403 → 400 → 409 → 201。
權杖一律經環境變數提供（systemd EnvironmentFile=/etc/inspection/app.env），
程式不列印權杖、不列印整筆事件、不列印請求標頭。
本週事件只存在記憶體（W5 改存資料庫）。
"""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
import hmac
import json
import os
from pathlib import Path
import re
import urllib.parse

MAX_BODY = 4096          # 本文上限 4 KiB，超過一律 400
MAX_NOTE = 200
LIST_LIMIT = 50
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
DEVICE_RE = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
TYPES = ("status", "anomaly", "test")
FIELDS = ("event_id", "device_id", "observed_at", "type", "note")

PAGE = """<!doctype html>
<html lang="zh-Hant">
<meta charset="utf-8">
<title>巡檢事件</title>
<style>
 body{font-family:system-ui,sans-serif;margin:2rem;max-width:60rem}
 input,button{font-size:1rem;padding:.3rem .5rem}
 li{margin:.4rem 0;padding:.4rem;border-bottom:1px solid #ddd;white-space:pre-wrap}
</style>
<h1>巡檢事件</h1>
<p>貼上 operator 權杖後讀取。權杖只留在這個頁面的變數裡，不寫進網址、不存進瀏覽器。</p>
<p><input id="token" type="password" size="40" placeholder="operator 權杖"> <button id="load">讀取</button></p>
<p id="msg"></p>
<ul id="list"></ul>
<script>
const token = document.getElementById("token");
const msg = document.getElementById("msg");
const list = document.getElementById("list");
document.getElementById("load").addEventListener("click", async () => {
  msg.textContent = "";
  list.textContent = "";
  const value = token.value;
  if (!value) { msg.textContent = "請先貼上權杖"; return; }
  try {
    const response = await fetch("/events", {headers: {Authorization: "Bearer " + value}});
    const data = await response.json();
    if (!response.ok) { msg.textContent = response.status + " " + (data.error || ""); return; }
    msg.textContent = "共 " + data.events.length + " 筆";
    for (const item of data.events) {
      const li = document.createElement("li");
      li.textContent = item.received_at + "  " + item.device_id + "  " + item.event_id +
                       "  " + item.type + "  " + (item.note || "");
      list.appendChild(li);
    }
  } catch (error) {
    msg.textContent = "讀取失敗";
  }
});
</script>
</html>
"""


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _valid_timestamp(value):
    """ISO 8601 且必須帶時區；沒有時區一律不合法。"""
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def validate_event(payload):
    """回傳 (event, None) 或 (None, (欄位, 原因))。不修改 payload。"""
    if not isinstance(payload, dict):
        return None, ("body", "invalid_json_object")
    extra = [key for key in payload if key not in FIELDS]
    if extra:
        return None, (extra[0], "unexpected_field")
    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not ID_RE.match(event_id):
        return None, ("event_id", "invalid_event_id")
    device_id = payload.get("device_id")
    if not isinstance(device_id, str) or not DEVICE_RE.match(device_id):
        return None, ("device_id", "invalid_device_id")
    if not _valid_timestamp(payload.get("observed_at")):
        return None, ("observed_at", "invalid_observed_at")
    if payload.get("type") not in TYPES:
        return None, ("type", "invalid_type")
    if "note" in payload:
        note = payload["note"]
        if not isinstance(note, str):
            return None, ("note", "invalid_note")
        if len(note) > MAX_NOTE:
            return None, ("note", "note_too_long")
    event = {key: payload[key] for key in FIELDS if key in payload}
    return event, None


def make_server(version_file, port=8080, tokens=None):
    version = Path(version_file).read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", version):
        raise ValueError("version must contain the deployed 40-character Git commit SHA")
    started = _utcnow()
    if tokens is None:
        tokens = {"reporter": os.environ.get("REPORTER_TOKEN", ""),
                  "operator": os.environ.get("OPERATOR_TOKEN", "")}
    events = {}
    lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        # ---- 共用工具 -------------------------------------------------
        def _role(self):
            header = self.headers.get("Authorization", "")
            scheme, _, value = header.partition(" ")
            if scheme.lower() != "bearer" or not value:
                return None
            for role in ("reporter", "operator"):
                expected = tokens.get(role) or ""
                if expected and hmac.compare_digest(value, expected):
                    return role
            return None

        def _send(self, code, payload, content_type="application/json; charset=utf-8"):
            data = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(
                payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def _error(self, code, reason, field):
            self._send(code, {"error": reason, "field": field})

        def _read_body(self, length):
            try:
                return self.rfile.read(length)
            except (OSError, ValueError):
                return b""

        def _length(self):
            raw = self.headers.get("Content-Length")
            if raw is None:
                return None
            try:
                return int(raw)
            except ValueError:
                return -1

        def _json(self, body):
            try:
                return json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None

        def _send_events(self, items, code=200):
            self._send(code, {"events": items, "count": len(items)})

        # ---- 路由 -----------------------------------------------------
        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/events":
                return self._get_events()
            if path.startswith("/events/"):
                return self._get_event(path[len("/events/"):])
            if path == "/health":
                return self._send(200, {"status": "ok", "service": "inspection",
                                        "version": version, "started_at": started,
                                        "auth_configured": bool(tokens.get("reporter")
                                                                and tokens.get("operator"))})
            if path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            return self._send(404, {"error": "not_found", "field": "path"})

        def _get_events(self):
            role = self._role()
            if role is None:
                return self._error(401, "unauthorized", "authorization")
            if role != "operator":
                return self._error(403, "forbidden", "authorization")
            with lock:
                items = [events[key] for key in list(events)][-LIST_LIMIT:]
            self._send_events(list(reversed(items)))

        def _get_event(self, event_id):
            role = self._role()
            if role is None:
                return self._error(401, "unauthorized", "authorization")
            if role != "operator":
                return self._error(403, "forbidden", "authorization")
            event_id = urllib.parse.unquote(event_id)
            with lock:
                event = events.get(event_id)
            if event is None:
                return self._error(404, "not_found", "event_id")
            self._send(200, event)

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            if path != "/events":
                return self._send(404, {"error": "not_found", "field": "path"})

            length = self._length()
            # 1) 先確認是誰：沒帶權杖一律 401，不從錯誤訊息洩漏欄位規則
            role = self._role()
            if role is None:
                if length is None or length > MAX_BODY:
                    self.close_connection = True
                else:
                    self._read_body(length)
                return self._error(401, "unauthorized", "authorization")
            # 2) 角色不對：403
            if role != "reporter":
                if length is None or length > MAX_BODY:
                    self.close_connection = True
                else:
                    self._read_body(length)
                return self._error(403, "forbidden", "authorization")
            # 3) 內容格式：400（Content-Type、大小、JSON、欄位）
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type != "application/json":
                if length is None or length > MAX_BODY:
                    self.close_connection = True
                else:
                    self._read_body(length)
                return self._error(400, "unsupported_media_type", "content_type")
            if length is None or length <= 0:
                return self._error(400, "empty_body", "body")
            if length > MAX_BODY:
                self.close_connection = True
                return self._error(400, "body_too_large", "body")
            payload = self._json(self._read_body(length))
            event, problem = validate_event(payload)
            if problem:
                field, reason = problem
                return self._error(400, reason, field)
            # 4) 重複：409
            with lock:
                if event["event_id"] in events:
                    return self._error(409, "duplicate_event_id", "event_id")
                # 5) 建立
                stored = dict(event)
                stored["received_at"] = _utcnow()
                events[event["event_id"]] = stored
            self._send(201, stored)

        def log_message(self, fmt, *args):
            pass  # 不寫任何請求路徑、本文、標頭或權杖

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.lock = lock
    server.events = events
    server.tokens = tokens
    return server


if __name__ == "__main__":
    make_server(Path(__file__).with_name("version")).serve_forever()
