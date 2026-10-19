#!/usr/bin/env python3
"""W3 T3：受控故障 (a) 移除／加回 SG 80 規則、(b) 停／啟 inspection、(c) 停／啟 nginx。

每個故障：印出預期 → 製造故障 → 有逾時上限的 curl（結束碼與 HTTP 狀態碼分開記）→ 恢復 → 再 curl。
只動 .local/resources.json 記錄的那個 SG 與主機。
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import ops  # noqa: E402


def probe(ip, label):
    exit_code, code, body, secs = ops.curl(f"http://{ip}/health", 8)
    ops.log(f"{label}: curl exit={exit_code} http={code} time={secs}s body={body[:100]!r}")
    return exit_code, code


def wait_ok(ip, sha, label):
    for _ in range(40):
        ok, _ = ops.health_ok(ip, sha)
        if ok:
            break
        time.sleep(1)
    probe(ip, label)


def remote(ip, command):
    r = ops.ssh(ip, command)
    out = (r.stdout + r.stderr).decode().strip().replace("\n", " | ")
    ops.log(f"ssh `{command}` -> rc={r.returncode} {out}")


def sg_rules(o, sg):
    perms = o.aws("ec2", "describe-security-groups", "--group-ids", sg)["SecurityGroups"][0]["IpPermissions"]
    return sorted(f"tcp/{p['FromPort']} from {r['CidrIp']}" for p in perms for r in p.get("IpRanges", []))


def fault_a(o, res):
    sg, ip, cidr = res["sg_id"], res["public_ip"], res["source_cidr"]
    rule = [{"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
             "IpRanges": [{"CidrIp": cidr, "Description": "Codespace HTTP"}]}]
    print("\n== (a) SG 擋住：移除 TCP 80 規則，再加回完全相同的規則")
    print("   預期：新的 HTTP 連線封包被 SG 丟棄，沒有任何回應 → curl 逾時 exit=28、http=000、約 8 秒")
    ops.log(f"SG {sg} rules before: {sg_rules(o, sg)}")
    o.aws("ec2", "revoke-security-group-ingress", "--group-id", sg, "--ip-permissions", json.dumps(rule))
    ops.log(f"SG {sg} rules after revoke: {sg_rules(o, sg)}")
    time.sleep(3)
    probe(ip, "(a) fault")
    o.aws("ec2", "authorize-security-group-ingress", "--group-id", sg, "--ip-permissions", json.dumps(rule))
    ops.log(f"SG {sg} rules after re-adding: {sg_rules(o, sg)}")
    wait_ok(ip, res["commit"], "(a) recovered")


def fault_b(res):
    ip = res["public_ip"]
    print("\n== (b) 後端停止：停 inspection，保留 nginx")
    print("   預期：封包過了 SG、nginx 收到但轉不到 127.0.0.1:8080 → 很快回 HTTP 502，curl exit=0")
    remote(ip, "sudo systemctl stop inspection; systemctl is-active inspection nginx")
    probe(ip, "(b) fault")
    remote(ip, "sudo systemctl start inspection; sleep 1; systemctl is-active inspection nginx")
    wait_ok(ip, res["commit"], "(b) recovered")


def fault_c(res):
    ip = res["public_ip"]
    print("\n== (c) 教師延伸：停 nginx（inspection 仍在）")
    print("   預期：封包過了 SG、主機上 80 沒有人聽 → 立刻被拒絕連線 exit=7、http=000")
    remote(ip, "sudo systemctl stop nginx; systemctl is-active inspection nginx")
    probe(ip, "(c) fault")
    remote(ip, "sudo systemctl start nginx; systemctl is-active inspection nginx")
    wait_ok(ip, res["commit"], "(c) recovered")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("faults", nargs="*", choices=["a", "b", "c"], help="預設三個都做")
    args = parser.parse_args()
    args.faults = args.faults or ["a", "b", "c"]
    o = ops.Ops()
    res = ops.load(ops.RESOURCES, {})
    ops.confirm([f"Plan (W3 T3) on instance {res['instance_id']} ({res['public_ip']}), SG {res['sg_id']}:",
                 "  (a) revoke then re-add the identical TCP 80 rule; (b) stop/start inspection; (c) stop/start nginx",
                 "  no resources are created or deleted"])
    probe(res["public_ip"], "baseline")
    if "a" in args.faults:
        fault_a(o, res)
    if "b" in args.faults:
        fault_b(res)
    if "c" in args.faults:
        fault_c(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
