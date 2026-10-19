#!/usr/bin/env python3
"""W6 規格 G：對帳腳本（在 Codespace 跑），一次印出範本第 5 段要的全部數字。

  python3 tests/w06_reconcile.py --code g06-m01 --run r1
不印權杖、密碼、私鑰；前綴先用正規式檢查再傳，psql 用 -v 傳值。
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

JOURNAL = ("sudo journalctl -u inspection-consumer -o cat --no-pager "
           "| grep -F 'event_id=PREFIX-' | grep -o 'result=[a-z]*' | sort | uniq -c")
PSQL = """sudo bash -c 'set -a; . /etc/inspection/app.env; set +a
  PGPASSWORD="$DB_PASSWORD" psql "host=$DB_HOST dbname=$DB_NAME user=$DB_USER sslmode=verify-full sslrootcert=/etc/inspection/rds-ca.pem" \\
    -v prefix="PREFIX-"'"""
SQL = b"SELECT count(*) FROM events WHERE left(event_id, length(:'prefix')) = :'prefix';\n"


def consumer_counts(host, prefix):
    r = ops.ssh(host, JOURNAL.replace("PREFIX", prefix))
    counts = {k: 0 for k in ("created", "duplicate", "rejected", "error")}
    for n, kind in re.findall(r"(\d+) result=([a-z]+)", r.stdout.decode()):
        counts[kind] = counts.get(kind, 0) + int(n)
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--code", required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()
    prefix = f"{args.code}-{args.run}"
    if not re.fullmatch(r"[A-Za-z0-9-]{1,40}", prefix):
        raise SystemExit("STOP: invalid prefix")
    base = current_base()
    host = base.split("//")[1]
    h = json.loads(call(base, "GET", "/health")[1])
    print(f"version={h['version']} db_configured={str(h.get('db_configured')).lower()} prefix={prefix}")

    last = json.loads((ops.LOCAL / "w06-last-run.json").read_text())
    if last["prefix"] != prefix:
        raise SystemExit(f"STOP: last gateway run was {last['prefix']}, not {prefix}")
    puback = last["puback_ok"]
    print(f"PUBACK (gateway): {puback}/{last['total']}")

    for attempt in range(13):
        counts = consumer_counts(host, prefix)
        if sum(counts.values()) >= 32:
            break
        time.sleep(5)
    print("consumer created/duplicate/rejected/error: {created}/{duplicate}/{rejected}/{error}".format(**counts))

    r = ops.ssh(host, PSQL.replace("PREFIX", prefix), stdin=SQL)
    db = re.search(r"-+\s+(\d+)\s+\(1 row\)", r.stdout.decode())
    db_count = int(db.group(1)) if db else None
    print(f"database rows with prefix {prefix}-: {db_count} (psql rc={r.returncode})")

    operator = read_env(ops.LOCAL / "app.env")["OPERATOR_TOKEN"]
    device = f"{args.code}-d01"
    status, body = call(base, "GET", f"/events?device_id={device}", operator)
    ids = [e["event_id"] for e in json.loads(body).get("events", []) if e["event_id"].startswith(prefix + "-")]
    print(f"GET /events?device_id={device}: HTTP {status}, this run: {len(ids)} {ids}")

    table = [("PUBACK", 32, puback),
             ("consumer created/duplicate/rejected/error", "30/1/1/0",
              "{created}/{duplicate}/{rejected}/{error}".format(**counts)),
             ("database rows (this prefix)", 30, db_count),
             (f"?device_id={device} (this prefix)", 3, len(ids))]
    print("\n| 位置 | 預期 | 實際 |\n|---|---|---|")
    for name, want, got in table:
        print(f"| {name} | {want} | {got} |")
    return 0 if all(str(w) == str(g) for _, w, g in table) else 1


if __name__ == "__main__":
    sys.exit(main())
