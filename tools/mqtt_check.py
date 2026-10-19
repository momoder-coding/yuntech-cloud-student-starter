#!/usr/bin/env python3
"""W6 規格 B：用閘道或 consumer 身分做一次連線測試（越權測試也用它）。

  python3 tools/mqtt_check.py --as consumer --sub cxe/<代號>/gw/events --wait 60
  python3 tools/mqtt_check.py --as gw --pub cxe/<代號>/gw/events
印出 connack=<原因碼>，以及 puback=<原因碼> 或 suback=<原因碼>；收到訊息印 recv topic=… payload=…。
"""
import argparse
import json
import sys
import time

from mqtt_common import Session, default_code


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as", dest="role", choices=["gw", "consumer"], required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--pub", metavar="TOPIC")
    action.add_argument("--sub", metavar="TOPIC_FILTER")
    parser.add_argument("--wait", type=float, default=10, help="訂閱後等幾秒收訊息")
    parser.add_argument("--code", default=None, help="代號，預設讀 .local/config.json")
    args = parser.parse_args()
    code = args.code or default_code()

    def recv(topic, payload):
        print(f"recv topic={topic} payload={payload.decode('utf-8', 'replace')}", flush=True)

    s = Session(args.role, code, on_recv=recv)
    connack = s.connect()
    print(f"connack={connack}", flush=True)
    if connack != 0:
        s.close()
        return 1
    if args.pub:
        rc = s.publish(args.pub, json.dumps({"check": "t2", "code": code}))
        print(f"puback={rc}", flush=True)
    else:
        rc = s.subscribe(args.sub)
        print(f"suback={rc}", flush=True)
        if rc is not None and rc < 128:
            time.sleep(args.wait)
    s.close()
    return 0 if rc is not None and rc < 128 else 2


if __name__ == "__main__":
    sys.exit(main())
