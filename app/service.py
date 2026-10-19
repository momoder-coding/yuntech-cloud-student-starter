#!/usr/bin/env python3
"""巡檢服務：W3 /health；W4 事件 API（認證、授權、驗證）與顯示頁。

需相容主機上的 Python 3.9（Amazon Linux 2023）。不寫日誌、不回顯權杖或整份本文。
"""
from datetime import datetime, timezone
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import parse_qs, urlsplit

MAX_BODY = 4096
FIELDS = ("event_id", "device_id", "observed_at", "type", "note")
REQUIRED = FIELDS[:4]
TYPES = ("status", "anomaly", "test")
EVENT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
DEVICE_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
# 必須帶時區；小數秒限 3 或 6 位（Python 3.9 的 fromisoformat 只接受這兩種）
OBSERVED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{3}|\.\d{6})?)?(Z|[+-]\d{2}:\d{2})")


class Reject(Exception):
    def __init__(self, status, error, field):
        super().__init__(error)
        self.status, self.error, self.field = status, error, field


def utc_iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def validate(event):
    """依契約逐欄檢查；回傳正規化的事件（observed_at 為 aware datetime）。"""
    if not isinstance(event, dict):
        raise Reject(400, "body must be a JSON object", "body")
    extra = sorted(set(event) - set(FIELDS))
    if extra:
        raise Reject(400, "unexpected field", extra[0])
    for name in REQUIRED:
        if name not in event:
            raise Reject(400, "missing required field", name)
    for name, pattern, rule in (("event_id", EVENT_ID, "1-64"), ("device_id", DEVICE_ID, "1-32")):
        if not isinstance(event[name], str) or not pattern.fullmatch(event[name]):
            raise Reject(400, f"must be {rule} characters of A-Z a-z 0-9 - _", name)
    value = event["observed_at"]
    try:
        if not isinstance(value, str) or not OBSERVED_AT.fullmatch(value):
            raise ValueError
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Reject(400, "must be ISO 8601 with a timezone, e.g. 2026-09-29T10:00:00+08:00", "observed_at")
    if event["type"] not in TYPES:
        raise Reject(400, "must be one of status, anomaly, test", "type")
    note = event.get("note")
    if "note" in event and (not isinstance(note, str) or len(note) > 200):
        raise Reject(400, "must be a string of at most 200 characters", "note")
    return {"event_id": event["event_id"], "device_id": event["device_id"], "observed_at": observed,
            "type": event["type"], "note": note}


def public(row):
    out = dict(row)
    out["observed_at"] = utc_iso(row["observed_at"])
    out["received_at"] = utc_iso(row["received_at"])
    if out["note"] is None:
        del out["note"]
    return out


class MemoryStore:
    """W4：事件只存在記憶體，服務重開就消失。"""
    configured = False

    def __init__(self):
        self.lock = threading.Lock()
        self.rows = {}

    def add(self, event):
        with self.lock:
            if event["event_id"] in self.rows:
                return "exists", None
            row = dict(event, received_at=datetime.now(timezone.utc))
            self.rows[event["event_id"]] = row
            return "created", row

    def list(self, limit=50):
        with self.lock:
            rows = sorted(self.rows.values(), key=lambda r: r["received_at"], reverse=True)
        return rows[:limit]

    def get(self, event_id):
        with self.lock:
            return self.rows.get(event_id)


PAGE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>巡檢事件</title>
<style nonce="NONCE">body{font:15px system-ui,sans-serif;margin:24px;max-width:960px}
table{border-collapse:collapse;width:100%;margin-top:12px}td,th{border:1px solid #ccc;padding:4px 8px;text-align:left}
input{padding:4px;margin-right:6px}#msg{margin-top:8px;color:#a00}</style></head>
<body><h1>巡檢事件</h1>
<label>operator 權杖 <input id="token" type="password" autocomplete="off"></label>
<button id="load">讀取</button> <button id="clear">清空權杖</button>
<div id="msg"></div>
<table><thead><tr><th>event_id</th><th>device_id</th><th>observed_at</th><th>type</th><th>note</th>
<th>received_at</th></tr></thead><tbody id="rows"></tbody></table>
<script nonce="NONCE">
const $ = (id) => document.getElementById(id);
$("clear").onclick = () => { $("token").value = ""; };
$("load").onclick = async () => {
  const token = $("token").value;
  const rows = $("rows");
  $("msg").textContent = "";
  rows.replaceChildren();
  const resp = await fetch("/events", {headers: {"Authorization": "Bearer " + token}, cache: "no-store"});
  const data = await resp.json();
  if (!resp.ok) { $("msg").textContent = resp.status + " " + data.error; return; }
  for (const ev of data.events) {
    const tr = document.createElement("tr");
    for (const key of ["event_id", "device_id", "observed_at", "type", "note", "received_at"]) {
      const td = document.createElement("td");
      td.textContent = ev[key] === undefined ? "" : String(ev[key]);
      tr.appendChild(td);
    }
    rows.appendChild(tr);
  }
  $("msg").textContent = data.events.length + " 筆";
};
</script></body></html>
"""


def make_server(version_file, port=8080, env=None, store=None):
    env = os.environ if env is None else env
    version = Path(version_file).read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", version):
        raise ValueError("version must contain the deployed 40-character Git commit SHA")
    started = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    tokens = {"reporter": env.get("REPORTER_TOKEN", ""), "operator": env.get("OPERATOR_TOKEN", "")}
    auth_configured = all(len(t) >= 16 for t in tokens.values()) and tokens["reporter"] != tokens["operator"]
    store = store or MemoryStore()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def send_json(self, status, body):
            self.send_bytes(status, json.dumps(body, ensure_ascii=False).encode("utf-8"),
                            "application/json; charset=utf-8")

        def send_bytes(self, status, data, content_type, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def drain(self):
            """先讀掉本文再回錯誤，避免連線被重設而讓客戶端收不到回應。"""
            if getattr(self, "body_read", False):
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if 0 < length <= 65536:
                    self.rfile.read(length)
            except (ValueError, OSError):
                pass

        def require(self, role):
            header = self.headers.get("Authorization", "")
            given = header[7:].strip().encode() if header.startswith("Bearer ") else b""
            who = None
            if auth_configured and given:
                for name, secret in tokens.items():
                    if hmac.compare_digest(given, secret.encode()):
                        who = name
            if who is None:
                raise Reject(401, "missing or invalid token", "authorization")
            if who != role:
                raise Reject(403, "this role is not allowed here", "authorization")

        def read_event(self):
            media = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if media != "application/json":
                raise Reject(400, "Content-Type must be application/json", "content-type")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                raise Reject(400, "Content-Length required", "body")
            if length > MAX_BODY:
                raise Reject(400, "body larger than 4 KiB", "body")
            self.body_read = True
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise Reject(400, "body is not valid JSON", "body")

        def route(self, method):
            url = urlsplit(self.path)
            path = url.path
            if method == "GET" and path == "/health":
                return 200, {"status": "ok", "service": "inspection", "version": version, "started_at": started,
                             "auth_configured": auth_configured, "db_configured": store.configured}
            if method == "GET" and path == "/":
                nonce = secrets.token_urlsafe(16)
                csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                       "connect-src 'self'; base-uri 'none'; form-action 'none'")
                self.send_bytes(200, PAGE.replace("NONCE", nonce).encode("utf-8"), "text/html; charset=utf-8",
                                {"Content-Security-Policy": csp, "Referrer-Policy": "no-referrer"})
                return None
            if path == "/events" and method == "POST":
                self.require("reporter")
                event = validate(self.read_event())
                result, row = store.add(event)
                if result == "created":
                    return 201, public(row)
                raise Reject(409, "event_id already exists", "event_id")
            if path == "/events" and method == "GET":
                self.require("operator")
                rows = store.list()
                return 200, {"count": len(rows), "events": [public(r) for r in rows]}
            if path.startswith("/events/") and method == "GET":
                self.require("operator")
                row = store.get(path[len("/events/"):])
                if row is None:
                    raise Reject(404, "event not found", "event_id")
                return 200, public(row)
            raise Reject(404, "not_found", "path")

        def handle_method(self, method):
            try:
                result = self.route(method)
                if result:
                    self.send_json(*result)
            except Reject as rej:
                self.drain()
                self.send_json(rej.status, {"error": rej.error, "field": rej.field})
            except Exception:  # 不把例外內容回給客戶端
                self.drain()
                self.send_json(500, {"error": "internal error", "field": "server"})

        def do_GET(self):
            self.handle_method("GET")

        def do_POST(self):
            self.handle_method("POST")

        def log_message(self, fmt, *args):
            pass  # Never log request paths, bodies, headers or query strings.

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    make_server(Path(__file__).with_name("version")).serve_forever()
