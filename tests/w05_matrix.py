#!/usr/bin/env python3
"""W5 T3：冪等矩陣 5 列。輸出可直接貼進繳交範本；不印權杖、密碼或請求標頭。

用法：python3 tests/w05_matrix.py [--seq 0201]
"""
import argparse
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
sys.path.insert(0, str(ROOT / "tests"))
import ops  # noqa: E402
from w04_matrix import call, current_base, read_env  # noqa: E402

PSQL = """sudo bash -c 'set -a; . /etc/inspection/app.env; set +a
  PGPASSWORD="$DB_PASSWORD" psql "host=$DB_HOST dbname=$DB_NAME user=$DB_USER sslmode=verify-full sslrootcert=/etc/inspection/rds-ca.pem" \\
    -v event_id="EVENT_ID"'"""


def psql_count(host, event_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", event_id):
        raise SystemExit("bad event_id")
    r = ops.ssh(host, PSQL.replace("EVENT_ID", event_id),
                stdin=b"SELECT count(*) FROM events WHERE event_id = :'event_id';\n")
    return r.returncode, " ".join(r.stdout.decode().split())


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seq", default=time.strftime("%H%M"))
    args = parser.parse_args()
    env = read_env(ops.LOCAL / "app.env")
    reporter, operator = env["REPORTER_TOKEN"], env["OPERATOR_TOKEN"]
    cfg = ops.load(ops.CONFIG, {})
    base = current_base()
    host = base.split("//")[1]
    h = json.loads(call(base, "GET", "/health")[1])
    print(f"version={h['version']} db_configured={str(h.get('db_configured')).lower()}")
    event = json.loads((ROOT / "tests/fixtures/w04-ok.json").read_text())["event"]
    event.update(event_id=f"{cfg['group']}-{cfg['owner']}-{args.seq}", note="W5 idempotency check")
    results = []

    def row(n, desc, want, status, body):
        ok = str(status) == str(want)
        results.append(ok)
        print(f"#{n} {desc} | expect {want} | HTTP {status} | {'PASS' if ok else 'FAIL'} | {body}")

    row(1, "送一筆新事件", 201, *call(base, "POST", "/events", reporter, event))
    row(2, "原樣重送（不新增）", 200, *call(base, "POST", "/events", reporter, event))
    row(3, "同 ID、note 不同", 409, *call(base, "POST", "/events", reporter, dict(event, note="different note")))
    r = ops.ssh(host, "sudo systemctl restart inspection; systemctl is-active inspection")
    for _ in range(30):
        if call(base, "GET", "/health")[0] == 200:
            break
        time.sleep(1)
    row(4, f"restart inspection（{r.stdout.decode().strip()}）後查 #1", 200,
        *call(base, "GET", f"/events/{event['event_id']}", operator))
    rc, out = psql_count(host, event["event_id"])
    count = re.search(r"-+ (\d+) \(1 row\)", out)
    ok = rc == 0 and count is not None and count.group(1) == "1"
    results.append(ok)
    print(f"#5 EC2 上 psql 查 #1 的筆數 | expect 1 | psql rc={rc} | {'PASS' if ok else 'FAIL'} | {out}")
    print(f"result: {sum(results)}/5 rows as expected")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
