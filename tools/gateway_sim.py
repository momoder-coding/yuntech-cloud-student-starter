#!/usr/bin/env python3
"""W6 規格 F：模擬閘道，以閘道身分發布 32 則到 cxe/<代號>/gw/events。

10 個設備各 3 筆 → 原樣重送第 1 筆 → 1 筆不在清單的 <代號>-d99。每則等 PUBACK（用訊息編號核對）才送下一則。
  python3 tools/gateway_sim.py --code g06-m01 --run r1
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

from mqtt_common import ROOT, Session

LAST_RUN = ROOT / ".local" / "w06-last-run.json"


def build_messages(code, run):
    prefix = f"{code}-{run}"
    if not re.fullmatch(r"[A-Za-z0-9-]{1,40}", prefix):
        raise SystemExit("STOP: prefix must match ^[A-Za-z0-9-]{1,40}$")
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    msgs = []
    for d in range(1, 11):
        for k in range(1, 4):
            msgs.append({"event_id": f"{prefix}-d{d:02d}-{k:02d}", "device_id": f"{code}-d{d:02d}",
                         "observed_at": now, "type": "status"})
    payloads = [json.dumps(m) for m in msgs]
    payloads.append(payloads[0])  # 原樣重送第 1 筆
    payloads.append(json.dumps({"event_id": f"{prefix}-d99-01", "device_id": f"{code}-d99",
                                "observed_at": now, "type": "status"}))
    return prefix, payloads


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--code", required=True)
    parser.add_argument("--run", required=True, help="r1、r2…；每跑一次對帳就換")
    args = parser.parse_args()
    prefix, payloads = build_messages(args.code, args.run)
    topic = f"cxe/{args.code}/gw/events"
    s = Session("gw", args.code)
    connack = s.connect()
    print(f"connack={connack} client=gw-{args.code} topic={topic} prefix={prefix}", flush=True)
    if connack != 0:
        s.close()
        return 1
    ok = 0
    for i, payload in enumerate(payloads, 1):
        rc = s.publish(topic, payload)
        ok += rc == 0
        print(f"#{i:02d} event_id={json.loads(payload)['event_id']} puback={rc}", flush=True)
    s.close()
    print(f"puback_ok={ok}/{len(payloads)}")
    LAST_RUN.write_text(json.dumps({"prefix": prefix, "puback_ok": ok, "total": len(payloads),
                                    "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n")
    return 0 if ok == len(payloads) else 2


if __name__ == "__main__":
    sys.exit(main())
