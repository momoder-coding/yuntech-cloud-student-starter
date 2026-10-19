#!/usr/bin/env python3
"""W5 T1：送 1 筆事件 → sudo systemctl restart inspection → 再查；印出重啟前後的筆數與 event_id。

權杖從 .local/app.env 讀，不印出。用法：python3 tests/w05_restart.py [--seq 0101]
"""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
sys.path.insert(0, str(ROOT / "tests"))
import ops  # noqa: E402
from w04_matrix import call, current_base, read_env  # noqa: E402


def listing(base, token):
    status, body = call(base, "GET", "/events", token)
    data = json.loads(body)
    return status, data.get("count"), [e["event_id"] for e in data.get("events", [])]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seq", default=time.strftime("%H%M"))
    args = parser.parse_args()
    env = read_env(ops.LOCAL / "app.env")
    cfg = ops.load(ops.CONFIG, {})
    base = current_base()
    _, health = call(base, "GET", "/health")
    h = json.loads(health)
    print(f"version={h['version']} db_configured={str(h.get('db_configured')).lower()}")
    event = json.loads((ROOT / "tests/fixtures/w04-ok.json").read_text())["event"]
    event.update(event_id=f"{cfg['group']}-{cfg['owner']}-{args.seq}", note="W5 T1 restart experiment")
    status, body = call(base, "POST", "/events", env["REPORTER_TOKEN"], event)
    print(f"POST {event['event_id']}: HTTP {status}")
    status, count, ids = listing(base, env["OPERATOR_TOKEN"])
    print(f"重啟前：GET /events HTTP {status} count={count} event_ids={ids}")
    r = ops.ssh(base.split("//")[1], "sudo systemctl restart inspection; systemctl is-active inspection")
    print(f"sudo systemctl restart inspection -> {r.stdout.decode().strip()}")
    for _ in range(30):
        if call(base, "GET", "/health")[0] == 200:
            break
        time.sleep(1)
    status, count, ids = listing(base, env["OPERATOR_TOKEN"])
    print(f"重啟後：GET /events HTTP {status} count={count} event_ids={ids}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
