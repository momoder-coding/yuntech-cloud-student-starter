#!/usr/bin/env python3
"""W3 T2：記錄五層「第一次觀測到」的 UTC 時間。

先在背景啟動本程式，再執行 deploy/up.sh；它會等 .local/resources.json 出現新的 instance_id，
接著各層平行輪詢：running、狀態檢查 2/2、cloud-init（含 --wait 前後時間）、80／127.0.0.1:8080 監聽、/health。
"""
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import ops  # noqa: E402

first = {}
lock = threading.Lock()


def mark(layer, detail):
    with lock:
        if layer not in first:
            first[layer] = ops.now()
            ops.log(f"FIRST {layer}: {detail}")


def poll(layer, check, stop, interval=3):
    while not stop.is_set() and layer not in first:
        try:
            detail = check()
            if detail:
                mark(layer, detail)
        except Exception:  # 主機開機中，連不上是預期的
            pass
        time.sleep(interval)


def main():
    timeout = 900
    old = ops.load(ops.RESOURCES, {}).get("instance_id")
    o = ops.Ops()
    ops.log(f"waiting for a new instance_id in .local/resources.json (current: {old})")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = ops.load(ops.RESOURCES, {})
        if res.get("instance_id") and res["instance_id"] != old:
            break
        time.sleep(1)
    iid, sha = res["instance_id"], res["commit"]
    ops.log(f"observing {iid}, expected version {sha}")
    state = {"ip": None}

    def running():
        d = ops.describe_instance(o, iid)
        if d and d["State"]["Name"] == "running":
            state["ip"] = d.get("PublicIpAddress")
            return f"state=running ip={state['ip']}"

    def status_checks():
        s = o.aws("ec2", "describe-instance-status", "--instance-ids", iid, "--include-all-instances")
        st = s["InstanceStatuses"][0]
        if st["InstanceStatus"]["Status"] == "ok" and st["SystemStatus"]["Status"] == "ok":
            return "status checks 2/2 passed (system ok, instance ok)"

    def cloud_init():
        if not state["ip"]:
            return None
        before = ops.now()
        r = ops.ssh(state["ip"], "cloud-init status --wait; echo rc=$?", timeout=900)
        if r.returncode == 255:
            return None  # SSH 還連不上
        out = r.stdout.decode().strip().replace("\n", " ")
        return f"`cloud-init status --wait` started {before}, finished {ops.now()}, result: {out}"

    def listening():
        if not state["ip"]:
            return None
        r = ops.ssh(state["ip"], "sudo ss -ltnH", timeout=30)
        text = r.stdout.decode()
        if r.returncode == 0 and "127.0.0.1:8080" in text and (":80 " in text or "*:80" in text):
            lines = [ln.split()[3] for ln in text.splitlines() if ln.split()[3].endswith((":80", ":8080"))]
            return "listening " + ", ".join(sorted(set(lines)))

    def health():
        if not state["ip"]:
            return None
        ok, body = ops.health_ok(state["ip"], sha)
        if ok:
            return f"/health 200 version == commit; body={body}"

    stop = threading.Event()
    layers = [("1 running", running), ("2 status checks 2/2", status_checks), ("3 cloud-init done", cloud_init),
              ("4 nginx:80 + inspection:127.0.0.1:8080 listening", listening), ("5 /health 200 + version", health)]
    threads = [threading.Thread(target=poll, args=(n, f, stop), daemon=True) for n, f in layers]
    for t in threads:
        t.start()
    while time.monotonic() < deadline and len(first) < len(layers):
        time.sleep(1)
    stop.set()
    print("\nFive-layer first-observed times (UTC):")
    for name, _ in layers:
        print(f"  {name:<52} {first.get(name, 'NOT OBSERVED within timeout')}")
    return 0 if len(first) == len(layers) else 1


if __name__ == "__main__":
    sys.exit(main())
