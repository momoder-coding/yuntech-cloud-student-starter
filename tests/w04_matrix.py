#!/usr/bin/env python3
"""W4 T3：對主機跑拒絕矩陣 7 列。

送出前先向 AWS 查主機「現在」的公開位址；權杖從 .local/app.env 讀，不印出。
用法：python3 tests/w04_matrix.py [--seq 0002]
"""
import argparse
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import ops  # noqa: E402


def read_env(path):
    values = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def call(base, method, path, token=None, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def current_base():
    o = ops.Ops()
    res = ops.load(ops.RESOURCES, {})
    inst = ops.describe_instance(o, res["instance_id"])
    if inst["State"]["Name"] != "running":
        raise SystemExit(f"STOP: instance {res['instance_id']} is {inst['State']['Name']}")
    return "http://" + inst["PublicIpAddress"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seq", default=time.strftime("%H%M"), help="event_id 流水號（預設用時分，重跑不撞號）")
    args = parser.parse_args()
    env = read_env(ops.LOCAL / "app.env")
    reporter, operator = env["REPORTER_TOKEN"], env["OPERATOR_TOKEN"]
    cfg = ops.load(ops.CONFIG, {})
    base = current_base()
    event = json.loads((ROOT / "tests/fixtures/w04-ok.json").read_text())["event"]
    event["event_id"] = f"{cfg['group']}-{cfg['owner']}-{args.seq}"
    no_tz = dict(event, observed_at=re.sub(r"(Z|[+-]\d{2}:\d{2})$", "", event["observed_at"]))

    _, health = call(base, "GET", "/health")
    h = json.loads(health)
    print(f"version={h['version']} auth_configured={str(h['auth_configured']).lower()} "
          f"host=x.x.x.{base.rsplit('.', 1)[1]}")
    rows = [
        ("reporter 送一筆合法事件", "201", lambda: call(base, "POST", "/events", reporter, event)),
        ("同上，不帶權杖", "401", lambda: call(base, "POST", "/events", None, event)),
        ("operator 權杖送事件", "403", lambda: call(base, "POST", "/events", operator, event)),
        ("reporter，observed_at 沒有時區", "400", lambda: call(base, "POST", "/events", reporter, no_tz)),
        ("reporter，再送一次 #1", "409", lambda: call(base, "POST", "/events", reporter, event)),
        ("reporter 權杖讀清單", "403", lambda: call(base, "GET", "/events", reporter)),
        ("operator 權杖讀清單", "200", lambda: call(base, "GET", "/events", operator)),
    ]
    passed = 0
    for i, (desc, want, fn) in enumerate(rows, 1):
        status, body = fn()
        if i == 7:
            listed = json.loads(body)
            ids = [e["event_id"] for e in listed.get("events", [])]
            body = json.dumps({"count": listed.get("count"), "contains_#1": event["event_id"] in ids,
                               "first": listed.get("events", [None])[0]}, ensure_ascii=False)
        ok = str(status) == want and (i != 7 or event["event_id"] in ids)
        passed += ok
        print(f"#{i} {desc} | expect {want} | HTTP {status} | {'PASS' if ok else 'FAIL'} | {body}")
    print(f"result: {passed}/7 rows as expected")
    return 0 if passed == 7 else 1


if __name__ == "__main__":
    sys.exit(main())
