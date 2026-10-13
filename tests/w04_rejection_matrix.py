#!/usr/bin/env python3
"""W4 拒絕矩陣：7 列一次跑完，輸出可直接貼進繳交範本。

- 送出前先查一次主機「現在」的公開位址（不沿用上週記下的）。
- 權杖從 .local/app.env 讀進記憶體，不列印、不出現在輸出。
- 出口 IP 會輪替，遇到連線逾時會重試；重試次數會印出來。
"""
import argparse
import json
from pathlib import Path
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 8
MAX_TRY = 25


def awsx(*args):
    result = subprocess.run(["python3", "deploy/awsx.py", *args], cwd=ROOT,
                            capture_output=True, text=True)
    if result.returncode:
        print("STOP:", result.stderr.strip(), file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def load_env():
    values = {}
    for line in (ROOT / ".local" / "app.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values["REPORTER_TOKEN"], values["OPERATOR_TOKEN"]


def call(host, method, path, body=None, token=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(f"http://{host}{path}", data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    for attempt in range(1, MAX_TRY + 1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
                return response.status, json.loads(raw), attempt
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw), attempt
            except json.JSONDecodeError:
                return error.code, {"raw": raw.decode("utf-8", "replace")[:120]}, attempt
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == MAX_TRY:
                return None, {"error": "unreachable_after_retries"}, attempt
            time.sleep(0.5)
    return None, {"error": "unreachable"}, MAX_TRY


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", default="g06-m01", help="你的代號，例如 g06-m01")
    parser.add_argument("--host", help="略過查詢，直接指定位址（預設會查現在的主機）")
    args = parser.parse_args()

    reporter, operator = load_env()
    if args.host:
        host = args.host
    else:
        instance_id = json.loads((ROOT / ".local" / "resources.json").read_text(encoding="utf-8"))["w03_instance_id"]
        host = awsx("ec2", "describe-instances", "--instance-ids", instance_id,
                    "--query", "Reservations[0].Instances[0].PublicIpAddress", "--output", "text")

    health, body, tries = call(host, "GET", "/health")
    print(f"目標主機 {host}｜/health http={health}（第 {tries} 次嘗試）"
          f"｜version={body.get('version', '?')}｜auth_configured={body.get('auth_configured', '?')}")
    print()

    event_id = f"{args.code}-{random.randint(1000, 9999)}"
    good = {"event_id": event_id, "device_id": f"{args.code}-d01",
            "observed_at": "2026-10-13T18:30:00+08:00", "type": "status",
            "note": "w04 rejection matrix"}
    no_tz = dict(good, event_id=f"{event_id}b", observed_at="2026-10-13T18:30:00")

    rows = [
        ("1", "reporter 送一筆合法事件", "POST", "/events", good, reporter, 201),
        ("2", "同上，不帶權杖", "POST", "/events", good, None, 401),
        ("3", "operator 權杖送事件", "POST", "/events", good, operator, 403),
        ("4", "reporter，observed_at 沒有時區", "POST", "/events", no_tz, reporter, 400),
        ("5", "reporter，再送一次 #1", "POST", "/events", good, reporter, 409),
        ("6", "reporter 權杖讀清單", "GET", "/events", None, reporter, 403),
        ("7", "operator 權杖讀清單", "GET", "/events", None, operator, 200),
    ]

    failures = 0
    for number, title, method, path, payload, token, expected in rows:
        status, body, tries = call(host, method, path, payload, token)
        shown = dict(body)
        if number == "7":
            shown = {"count": body.get("count"),
                     "contains_#1": any(item.get("event_id") == event_id
                                        for item in body.get("events", []))}
        ok = status == expected
        failures += 0 if ok else 1
        print(f"第 {number} 列 {title}")
        print(f"  http={status}（預期 {expected}，{'符合' if ok else '不符'}）"
              f"｜嘗試 {tries} 次")
        print(f"  回應本文 {json.dumps(shown, ensure_ascii=False)}")
    print()
    print(f"event_id（第 1 列的）：{event_id}")
    print(f"結果：{'7 列全部符合預期' if not failures else str(failures) + ' 列與預期不符'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
