#!/usr/bin/env python3
"""g06-m01 的建立／部署／回收腳本本體；由 deploy/*.sh 呼叫。

- 只處理 .local/resources.json 記錄的 ID；刪除前核對標籤，不依名稱搜尋。
- 每建立一項立刻寫入 .local/resources.json（.local/ 不進 Git）。
- 執行前印出將建立／刪除的清單並等輸入 yes；不印出憑證、權杖、私鑰或密碼。
- AWS 呼叫一律經 scripts/lab.py 的 run_aws（clean_env、learnerlab profile）。
"""
import argparse
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "deploy"))
import lab  # noqa: E402
import make_user_data  # noqa: E402

LOCAL = ROOT / ".local"
RESOURCES = LOCAL / "resources.json"
CONFIG = LOCAL / "config.json"
SSH_KEY = Path.home() / ".ssh" / "yuntech_w03_ed25519"
INSTANCE_TYPE = "t3.micro"
AL2023_PARAM = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
ROUND_KEYS = ("instance_id", "volume_id", "eni_id", "sg_id", "sg_name", "key_pair_id", "key_name",
              "public_ip", "commit", "ami_id", "subnet_id", "az", "launched_at")


# ---------- 共用 ----------

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


class Ops:
    def __init__(self):
        self.ctx = lab.verify()
        self.region = self.ctx["region"]

    def aws(self, *args, raw=False):
        return lab.run_aws(list(args), self.region, raw=raw)

    def aws_code(self, *args):
        """執行並回傳 (結果, 錯誤碼)；用在讀回「查無」的情況。"""
        try:
            return self.aws(*args), None
        except lab.LabError as exc:
            match = re.search(r": ([A-Za-z0-9._-]+)\. Stop;", str(exc))
            return None, match.group(1) if match else str(exc)


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_resources(res):
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    lab.atomic_write(RESOURCES, json.dumps(res, indent=2, sort_keys=True) + "\n")


def confirm(lines):
    print("\n".join(lines), flush=True)
    if input("Type yes to proceed: ").strip() != "yes":
        raise lab.LabError("Cancelled; no changes made.")


def tags_for(cfg, week, name):
    return [{"Key": "course", "Value": lab.COURSE}, {"Key": "week", "Value": week},
            {"Key": "group", "Value": cfg["group"]}, {"Key": "owner", "Value": cfg["owner"]},
            {"Key": "Name", "Value": name}]


def tag_spec(resource_type, tags):
    return json.dumps([{"ResourceType": resource_type, "Tags": tags}])


def tag_map(tags):
    return {t["Key"]: t["Value"] for t in tags or []}


def check_owner_tags(label, tags, cfg):
    got = tag_map(tags)
    want = {"course": lab.COURSE, "group": cfg["group"], "owner": cfg["owner"]}
    bad = {k: got.get(k) for k, v in want.items() if got.get(k) != v}
    if bad:
        raise lab.LabError(f"{label}: ownership tags do not match ({bad}); refusing to touch it.")


def curl(url, max_time=8):
    """回傳 (curl 結束碼, HTTP 狀態碼, 本文, 秒數)。"""
    start = time.monotonic()
    proc = subprocess.run(["curl", "-s", "-o", "-", "-w", "\n%{http_code}", "--max-time", str(max_time), url],
                          capture_output=True, text=True)
    body, _, code = proc.stdout.rpartition("\n")
    return proc.returncode, code, body, round(time.monotonic() - start, 2)


def ssh(host, command, stdin=None, timeout=120):
    # 沒有要送的內容時不讓 ssh 讀我們自己的 stdin（否則會吃掉呼叫端腳本後面的指令）
    feed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(["ssh", "-i", str(SSH_KEY), "-o", "StrictHostKeyChecking=accept-new",
                           "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"ec2-user@{host}", command],
                          capture_output=True, timeout=timeout, **feed)


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def egress_ip():
    _, code, body, _ = curl("https://checkip.amazonaws.com", 8)
    if code != "200" or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", body.strip()):
        raise lab.LabError("Cannot determine Codespace egress IPv4.")
    return body.strip()


def load_config(args):
    cfg = load(CONFIG, {})
    for key in ("group", "owner", "source_cidr"):
        if getattr(args, key, None):
            cfg[key] = getattr(args, key)
    missing = [k for k in ("group", "owner") if not cfg.get(k)]
    if missing:
        raise lab.LabError(f"Missing {missing}; pass --group/--owner or create .local/config.json.")
    for key in ("group", "owner"):
        if not re.fullmatch(r"[a-z0-9]{1,10}", cfg[key]):
            raise lab.LabError("group/owner must be 1-10 lowercase letters or digits (no student ID).")
    return cfg


def health_ok(ip, sha):
    exit_code, code, body, _ = curl(f"http://{ip}/health", 5)
    if code != "200":
        return False, body
    try:
        return json.loads(body).get("version") == sha, body
    except ValueError:
        return False, body


def build_user_data(commit):
    sha, data = make_user_data.build(commit)
    out = LOCAL / f"user-data-{sha[:12]}-{int(time.time())}.sh"
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    with out.open("xb") as stream:
        out.chmod(0o600)
        stream.write(data)
    return sha, out, len(data)


def warn_dirty():
    if git("status", "--porcelain", "--untracked-files=no"):
        log("NOTE: working tree has uncommitted changes; only the committed version is deployed.")


# ---------- W3：up ----------

def find_network(ops):
    vpcs = ops.aws("ec2", "describe-vpcs", "--filters", "Name=is-default,Values=true")["Vpcs"]
    if not vpcs:
        raise lab.LabError("No default VPC; stop and ask the teacher (do not create one).")
    vpc = vpcs[0]["VpcId"]
    offered = {o["Location"] for o in ops.aws(
        "ec2", "describe-instance-type-offerings", "--location-type", "availability-zone",
        "--filters", f"Name=instance-type,Values={INSTANCE_TYPE}")["InstanceTypeOfferings"]}
    subnets = sorted((s for s in ops.aws("ec2", "describe-subnets", "--filters", f"Name=vpc-id,Values={vpc}",
                                         "Name=default-for-az,Values=true")["Subnets"]
                      if s["AvailabilityZone"] in offered), key=lambda s: s["AvailabilityZone"])
    if not subnets:
        raise lab.LabError(f"No default subnet supports {INSTANCE_TYPE}.")
    subnet = subnets[0]
    tables = ops.aws("ec2", "describe-route-tables", "--filters",
                     f"Name=association.subnet-id,Values={subnet['SubnetId']}")["RouteTables"]
    how = "explicit association"
    if not tables:
        tables = ops.aws("ec2", "describe-route-tables", "--filters", f"Name=vpc-id,Values={vpc}",
                         "Name=association.main,Values=true")["RouteTables"]
        how = "VPC main route table (no explicit association)"
    table = tables[0]
    igw = [r["GatewayId"] for r in table["Routes"]
           if r.get("DestinationCidrBlock") == "0.0.0.0/0" and r.get("GatewayId", "").startswith("igw-")]
    if not igw:
        raise lab.LabError("Subnet's route table has no 0.0.0.0/0 -> igw route; stop and ask for help.")
    return {"vpc_id": vpc, "subnet_id": subnet["SubnetId"], "az": subnet["AvailabilityZone"],
            "subnet_cidr": subnet["CidrBlock"], "map_public_ip": subnet["MapPublicIpOnLaunch"],
            "route_table_id": table["RouteTableId"], "route_table_how": how, "igw_id": igw[0]}


def find_ami(ops):
    ami_id = ops.aws("ssm", "get-parameter", "--name", AL2023_PARAM, "--query", "Parameter.Value",
                     "--output", "text", raw=True)
    image = ops.aws("ec2", "describe-images", "--image-ids", ami_id)["Images"][0]
    if image["Architecture"] != "x86_64":
        raise lab.LabError("AMI is not x86_64.")
    return {"ami_id": ami_id, "ami_name": image["Name"], "ami_arch": image["Architecture"],
            "ami_created": image["CreationDate"], "root_device": image["RootDeviceName"]}


def ensure_ssh_key():
    if not SSH_KEY.exists():
        SSH_KEY.parent.mkdir(mode=0o700, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "yuntech-w03", "-f", str(SSH_KEY)],
                       check=True)
        log(f"generated {SSH_KEY} (ed25519); private key stays in the Codespace")
    SSH_KEY.chmod(0o600)
    return SSH_KEY.with_suffix(".pub")


def cmd_up(args):
    ops = Ops()
    cfg = load_config(args)
    res = load(RESOURCES, {})
    if res.get("instance_id"):
        state = describe_instance(ops, res["instance_id"])
        if state and state["State"]["Name"] != "terminated":
            raise lab.LabError(f"Instance {res['instance_id']} is {state['State']['Name']}; only one host at a time. "
                               "Use down.sh first (or deploy.sh to update it).")
    my_ip = egress_ip()
    cidr = cfg.get("source_cidr") or f"{my_ip}/32"
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+/32", cidr):
        raise lab.LabError("Source must be a single IPv4 /32.")
    if cidr != f"{my_ip}/32":
        log(f"WARNING: configured source {cidr} differs from current Codespace egress {my_ip}/32")
    warn_dirty()
    sha, user_data, size = build_user_data(args.commit)
    net = find_network(ops)
    ami = find_ami(ops)
    pub = ensure_ssh_key()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    name = f"{cfg['group']}-{cfg['owner']}-w03-{stamp}"
    confirm([
        "Plan (W3 up.sh) — will CREATE:",
        f"  network  default VPC {net['vpc_id']}, default subnet {net['subnet_id']} ({net['az']}, {net['subnet_cidr']}),",
        f"           route table {net['route_table_id']} [{net['route_table_how']}] has 0.0.0.0/0 -> {net['igw_id']}",
        f"  AMI      {ami['ami_id']} {ami['ami_name']} ({ami['ami_arch']}, created {ami['ami_created']})",
        f"  1 SG     {name}: inbound TCP 22 and TCP 80 from {cidr} only",
        f"  1 key    {name}: import public key {pub} (private key stays in ~/.ssh)",
        f"  1 EC2    {INSTANCE_TYPE}, root gp3 encrypted DeleteOnTermination, IMDSv2 required, public IPv4",
        f"  deploy   commit {sha} via user data ({size} bytes < 16384)",
        f"  tags     course={lab.COURSE} week=w03 group={cfg['group']} owner={cfg['owner']}",
        "  cost     EC2 t3.micro + EBS gp3 8 GiB + 1 public IPv4 while running (see official pricing pages)",
        "  cleanup  deploy/down.sh (by recorded IDs) or down.sh --stop to keep it",
    ])
    t0 = time.monotonic()
    res.update({"region": ops.region, "group": cfg["group"], "owner": cfg["owner"], "commit": sha,
                "vpc_id": net["vpc_id"], "subnet_id": net["subnet_id"], "az": net["az"],
                "ami_id": ami["ami_id"], "source_cidr": cidr})
    for key in ("instance_id", "volume_id", "eni_id", "public_ip", "sg_id", "key_pair_id"):
        res.pop(key, None)

    tags = tags_for(cfg, "w03", name)
    sg = ops.aws("ec2", "create-security-group", "--group-name", name, "--vpc-id", net["vpc_id"],
                 "--description", f"W3 {cfg['group']}-{cfg['owner']} HTTP/SSH from Codespace /32 only",
                 "--tag-specifications", tag_spec("security-group", tags))["GroupId"]
    res.update(sg_id=sg, sg_name=name)
    save_resources(res)
    log(f"created SG {sg}")
    perms = [{"IpProtocol": "tcp", "FromPort": p, "ToPort": p,
              "IpRanges": [{"CidrIp": cidr, "Description": f"Codespace {d}"}]} for p, d in ((22, "SSH"), (80, "HTTP"))]
    ops.aws("ec2", "authorize-security-group-ingress", "--group-id", sg, "--ip-permissions", json.dumps(perms))
    log(f"SG {sg} inbound: TCP 22, 80 from {cidr}")

    key = ops.aws("ec2", "import-key-pair", "--key-name", name, "--public-key-material", f"fileb://{pub}",
                  "--tag-specifications", tag_spec("key-pair", tags))
    res.update(key_pair_id=key["KeyPairId"], key_name=name)
    save_resources(res)
    log(f"imported key pair {key['KeyPairId']} ({name})")

    bdm = [{"DeviceName": ami["root_device"],
            "Ebs": {"VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True}}]
    specs = [{"ResourceType": t, "Tags": tags} for t in ("instance", "volume", "network-interface")]
    inst = ops.aws("ec2", "run-instances", "--image-id", ami["ami_id"], "--instance-type", INSTANCE_TYPE,
                   "--key-name", name, "--subnet-id", net["subnet_id"], "--security-group-ids", sg,
                   "--associate-public-ip-address", "--user-data", f"file://{user_data}",
                   "--block-device-mappings", json.dumps(bdm),
                   "--metadata-options", "HttpTokens=required,HttpEndpoint=enabled,HttpPutResponseHopLimit=1",
                   "--tag-specifications", json.dumps(specs), "--count", "1")["Instances"][0]
    res.update(instance_id=inst["InstanceId"], launched_at=now())
    save_resources(res)
    log(f"launched instance {inst['InstanceId']}")

    while True:
        desc = describe_instance(ops, inst["InstanceId"])  # 剛建立時可能暫時查無
        if desc and desc["State"]["Name"] == "running" and desc.get("PublicIpAddress"):
            break
        time.sleep(2)
    ip = desc["PublicIpAddress"]
    t_running = now()
    exit_code, code, body, secs = curl(f"http://{ip}/health", 8)
    log(f"LAYER running: instance running at {t_running}, public IP {ip}")
    log(f"EARLY curl right after running: exit={exit_code} http={code} time={secs}s body={body[:120]!r}")
    subprocess.run(["ssh-keygen", "-R", ip], capture_output=True)  # 新主機：舊的同 IP 指紋不是它的
    res.update(public_ip=ip, volume_id=desc["BlockDeviceMappings"][0]["Ebs"]["VolumeId"],
               eni_id=desc["NetworkInterfaces"][0]["NetworkInterfaceId"])
    save_resources(res)
    log(f"recorded root volume {res['volume_id']} and ENI {res['eni_id']}")

    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        ok, body = health_ok(ip, sha)
        if ok:
            log(f"LAYER health: /health 200, version == {sha}")
            log(f"/health body: {body}")
            log(f"up.sh total: {time.monotonic() - t0:.0f}s from confirmation to healthy")
            return
        time.sleep(3)
    raise lab.LabError("/health did not become ready within 15 minutes; check cloud-init on the host.")


# ---------- W3：down ----------

def describe_instance(ops, instance_id):
    out, err = ops.aws_code("ec2", "describe-instances", "--instance-ids", instance_id)
    if err:
        return None
    return out["Reservations"][0]["Instances"][0]


def wait_state(ops, instance_id, want, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = describe_instance(ops, instance_id)["State"]["Name"]
        if state == want:
            return state
        time.sleep(5)
    raise lab.LabError(f"Instance did not reach {want} in time.")


def cmd_down(args):
    ops = Ops()
    cfg = load_config(args)
    res = load(RESOURCES, {})
    if not res.get("instance_id"):
        raise lab.LabError("No instance recorded in .local/resources.json.")
    inst = describe_instance(ops, res["instance_id"])
    if not inst:
        raise lab.LabError("Recorded instance not found.")
    check_owner_tags(f"instance {res['instance_id']}", inst.get("Tags"), cfg)
    if args.stop:
        confirm(["Plan (down.sh --stop) — will STOP (not delete):",
                 f"  EC2 {res['instance_id']} (now {inst['State']['Name']}); SG, key pair, EBS, ENI are kept"])
        ops.aws("ec2", "stop-instances", "--instance-ids", res["instance_id"])
        log(f"readback: instance {res['instance_id']} is {wait_state(ops, res['instance_id'], 'stopped')}")
        return
    sg = ops.aws("ec2", "describe-security-groups", "--group-ids", res["sg_id"])["SecurityGroups"][0]
    check_owner_tags(f"SG {res['sg_id']}", sg.get("Tags"), cfg)
    kp = ops.aws("ec2", "describe-key-pairs", "--key-pair-ids", res["key_pair_id"])["KeyPairs"][0]
    check_owner_tags(f"key pair {res['key_pair_id']}", kp.get("Tags"), cfg)
    root = inst["BlockDeviceMappings"][0]["Ebs"]
    eni = inst["NetworkInterfaces"][0]
    if root["VolumeId"] != res["volume_id"] or eni["NetworkInterfaceId"] != res["eni_id"]:
        raise lab.LabError("Volume/ENI attached to the instance differ from the recorded IDs; stop and inspect.")
    confirm([
        "Plan (down.sh) — will DELETE exactly these recorded IDs (tags verified):",
        f"  EC2 instance  {res['instance_id']} -> terminate",
        f"  root EBS      {res['volume_id']} (DeleteOnTermination={root['DeleteOnTermination']}) -> deleted with instance",
        f"  ENI           {res['eni_id']} (DeleteOnTermination={eni['Attachment']['DeleteOnTermination']}) -> deleted with instance",
        f"  SG            {res['sg_id']} ({res['sg_name']}) -> delete after ENI is gone",
        f"  key pair      {res['key_pair_id']} ({res['key_name']}) -> delete",
        "  default VPC/subnet/IGW are only referenced and are NOT touched",
    ])
    t0 = time.monotonic()
    ops.aws("ec2", "terminate-instances", "--instance-ids", res["instance_id"])
    log(f"terminate requested for {res['instance_id']}")
    wait_state(ops, res["instance_id"], "terminated")
    for _ in range(36):
        _, err = ops.aws_code("ec2", "delete-security-group", "--group-id", res["sg_id"])
        if err is None:
            break
        if err != "DependencyViolation":
            raise lab.LabError(f"delete-security-group failed: {err}")
        time.sleep(5)
    ops.aws("ec2", "delete-key-pair", "--key-pair-id", res["key_pair_id"])
    log(f"deleted SG {res['sg_id']} and key pair {res['key_pair_id']} ({time.monotonic() - t0:.0f}s)")

    print("Readback:")
    print(f"  instance {res['instance_id']}: {describe_instance(ops, res['instance_id'])['State']['Name']}")
    checks = [("root EBS", res["volume_id"], ("ec2", "describe-volumes", "--volume-ids")),
              ("ENI", res["eni_id"], ("ec2", "describe-network-interfaces", "--network-interface-ids")),
              ("SG", res["sg_id"], ("ec2", "describe-security-groups", "--group-ids")),
              ("key pair", res["key_pair_id"], ("ec2", "describe-key-pairs", "--key-pair-ids"))]
    for label, rid, cmd in checks:
        for _ in range(24):
            _, err = ops.aws_code(*cmd, rid)
            if err:
                break
            time.sleep(5)
        print(f"  {label} {rid}: {'not found (' + err + ')' if err else 'STILL EXISTS'}")
    retired = {k: res.get(k) for k in ROUND_KEYS}
    retired["deleted_at"] = now()
    res.setdefault("retired_rounds", []).append(retired)
    for key in ROUND_KEYS:
        res.pop(key, None)
    save_resources(res)


# ---------- W4：start（T1 恢復）與 deploy ----------

def sg_cidrs(ops, sg_id):
    perms = ops.aws("ec2", "describe-security-groups", "--group-ids", sg_id)["SecurityGroups"][0]["IpPermissions"]
    return {(p["FromPort"], r["CidrIp"]) for p in perms for r in p.get("IpRanges", [])}


def cmd_start(args):
    ops = Ops()
    cfg = load_config(args)
    res = load(RESOURCES, {})
    inst = describe_instance(ops, res["instance_id"])
    check_owner_tags(f"instance {res['instance_id']}", inst.get("Tags"), cfg)
    old_ip = res.get("public_ip")
    db = describe_db(ops, res["db_instance_id"]) if res.get("db_instance_id") else None
    if db:
        log(f"RDS {res['db_instance_id']} status: {db['DBInstanceStatus']}")
    if inst["State"]["Name"] == "stopped" or (db and db["DBInstanceStatus"] == "stopped"):
        confirm([f"Plan (start): START instance {res['instance_id']} ({inst['State']['Name']})"
                 + (f" and RDS {res['db_instance_id']} ({db['DBInstanceStatus']})" if db else "")
                 + "; no new resources"])
    if db and db["DBInstanceStatus"] == "stopped":  # RDS 要好幾分鐘，先發出啟動、不必等它
        ops.aws("rds", "start-db-instance", "--db-instance-identifier", res["db_instance_id"])
        log("RDS start requested")
    if inst["State"]["Name"] == "stopped":
        ops.aws("ec2", "start-instances", "--instance-ids", res["instance_id"])
        wait_state(ops, res["instance_id"], "running")
    else:
        log(f"instance is already {inst['State']['Name']}; not starting it again")
    ip = describe_instance(ops, res["instance_id"])["PublicIpAddress"]
    res["public_ip"] = ip
    save_resources(res)
    log(f"public IP: old {old_ip} -> new {ip} (Start assigns a new address; no EIP in this course)")
    new = f"{egress_ip()}/32"
    rules = sg_cidrs(ops, res["sg_id"])
    log(f"Codespace egress now {new}; SG {res['sg_id']} rules {sorted(rules)}")
    if rules != {(22, new), (80, new)}:
        old = sorted({c for _, c in rules})
        confirm([f"Plan: replace SG {res['sg_id']} source {old} -> {new} on TCP 22 and 80 (exact /32, not wider)"])
        for port, cidr in rules:
            ops.aws("ec2", "revoke-security-group-ingress", "--group-id", res["sg_id"], "--protocol", "tcp",
                    "--port", str(port), "--cidr", cidr)
        for port, desc in ((22, "SSH"), (80, "HTTP")):
            perm = [{"IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                     "IpRanges": [{"CidrIp": new, "Description": f"Codespace {desc}"}]}]
            ops.aws("ec2", "authorize-security-group-ingress", "--group-id", res["sg_id"],
                    "--ip-permissions", json.dumps(perm))
        res["source_cidr"] = new
        save_resources(res)
        cfg["source_cidr"] = new
        lab.atomic_write(CONFIG, json.dumps(cfg) + "\n")
        log(f"SG rules now {sorted(sg_cidrs(ops, res['sg_id']))}")
    if db:
        while describe_db(ops, res["db_instance_id"])["DBInstanceStatus"] != "available":
            time.sleep(15)
        log(f"RDS {res['db_instance_id']} is available")
    for _ in range(100):
        exit_code, code, body, _ = curl(f"http://{ip}/health", 5)
        if code == "200":
            break
        time.sleep(3)
    log(f"/health: http={code} body={body}")


def secrets_payload():
    """組合要放上主機 /etc/inspection/app.env 的內容；只檢查權限，不印出內容。"""
    parts = []
    for name, required in (("app.env", True), ("db.env", False)):
        path = LOCAL / name
        if not path.exists():
            if required:
                raise lab.LabError(f".local/{name} is missing; generate it first (README W4).")
            continue
        mode = path.stat().st_mode & 0o777
        if mode != 0o600:
            raise lab.LabError(f".local/{name} must be 600 (is {oct(mode)[2:]}); stop.")
        text = path.read_text()
        parts.append(text if text.endswith("\n") else text + "\n")
    return "".join(parts).encode(), [p for p in ("app.env", "db.env") if (LOCAL / p).exists()]


INSTALL = 'f=$(mktemp) && cat > "$f" && sudo bash "$f" > /tmp/inspection-install.log 2>&1; rc=$?; rm -f "$f"; ' \
          'tail -n 3 /tmp/inspection-install.log; exit $rc'
PUT_SECRETS = ("sudo sh -c 'umask 077; install -d -m 755 /etc/inspection && cat > /etc/inspection/.app.env.new && "
               "chown root:root /etc/inspection/.app.env.new && chmod 600 /etc/inspection/.app.env.new && "
               "mv -f /etc/inspection/.app.env.new /etc/inspection/app.env && systemctl restart inspection && "
               "stat -c \"%a %U %n\" /etc/inspection /etc/inspection/app.env'")


def cmd_deploy(args):
    ops = Ops()
    cfg = load_config(args)
    res = load(RESOURCES, {})
    inst = describe_instance(ops, res["instance_id"])
    check_owner_tags(f"instance {res['instance_id']}", inst.get("Tags"), cfg)
    if inst["State"]["Name"] != "running":
        raise lab.LabError(f"Instance is {inst['State']['Name']}; run `python3 deploy/ops.py start` first.")
    ip = inst["PublicIpAddress"]
    res["public_ip"] = ip
    payload, secret_files = secrets_payload()
    iot = iot_payloads(cfg)
    if iot:
        payload += iot[0]
        secret_files.append("iot/endpoint (IOT_ lines)")
    warn_dirty()
    sha, user_data, size = build_user_data(args.commit)
    confirm([
        "Plan (deploy.sh) — update the SAME host, no new resources:",
        f"  target   {res['instance_id']} at {ip}",
        f"  commit   {sha} (user data {size} bytes, committed files only)",
        f"  secrets  .local/{' + .local/'.join(secret_files)} -> /etc/inspection/app.env (root, 600) via SSH stdin",
        *(["  iot      consumer cert, key, Root CA -> /etc/inspection/iot/ (dir 700, files 600, owner inspection)",
           "           then restart inspection-consumer"] if iot else []),
        "  then     restart inspection and check /health version",
    ])
    t0 = time.monotonic()
    r = ssh(ip, INSTALL, stdin=user_data.read_bytes(), timeout=900)
    log(f"installer rc={r.returncode}: {r.stdout.decode().strip()[-300:]}")
    if r.returncode:
        raise lab.LabError("Installer failed on the host; see /tmp/inspection-install.log there.")
    r = ssh(ip, PUT_SECRETS, stdin=payload)
    del payload
    if r.returncode:
        raise lab.LabError("Placing the secret file failed (output withheld).")
    log("secret file placed: " + r.stdout.decode().strip().replace("\n", "; "))
    if iot:
        for name, data in iot[1].items():
            r = ssh(ip, PUT_IOT_FILE.replace("NAME", name), stdin=data)
            if r.returncode:
                raise lab.LabError(f"Placing /etc/inspection/iot/{name} failed (output withheld).")
        r = ssh(ip, "sudo systemctl restart inspection-consumer; sleep 3; "
                    "systemctl is-active inspection inspection-consumer | paste -sd' '")
        log("consumer files placed; systemctl is-active inspection inspection-consumer: " + r.stdout.decode().strip())
    expect_db = "db.env" in secret_files
    for _ in range(60):
        exit_code, code, body, _ = curl(f"http://{ip}/health", 5)
        try:
            h = json.loads(body) if code == "200" else {}
        except ValueError:
            h = {}
        if h.get("version") == sha and h.get("auth_configured") is True and (
                not expect_db or h.get("db_configured") is True):
            res["commit"] = sha
            save_resources(res)
            log(f"/health: {body}")
            log(f"deploy.sh done in {time.monotonic() - t0:.0f}s; version == {sha}")
            return
        time.sleep(2)
    raise lab.LabError(f"/health did not report version {sha} with secrets configured; last: http={code} {body}")


# ---------- W5 T4／W6 步驟 8：保留（停止主機與 RDS） ----------

def short(rid):
    return "…" + rid[-4:]


def cmd_keep(args):
    ops = Ops()
    cfg = load_config(args)
    res = load(RESOURCES, {})
    inst = describe_instance(ops, res["instance_id"])
    check_owner_tags(f"instance {res['instance_id']}", inst.get("Tags"), cfg)
    db = describe_db(ops, res["db_instance_id"]) if res.get("db_instance_id") else None
    confirm(["Plan (keep) — STOP only, nothing is deleted:",
             f"  EC2 {res['instance_id']} ({inst['State']['Name']}) -> stop",
             *([f"  RDS {res['db_instance_id']} ({db['DBInstanceStatus']}) -> stop "
                "(AWS auto-starts it after 7 days stopped)"] if db else []),
             "  kept: SG, key pair, EBS, ENI" + (", private subnets, route table, DB subnet group, SG-db"
                                                  if db else "")])
    if inst["State"]["Name"] not in ("stopped", "stopping"):
        out = ops.aws("ec2", "stop-instances", "--instance-ids", res["instance_id"], "--query",
                      "StoppingInstances[0].CurrentState.Name", "--output", "text", raw=True)
        log(f"ec2 stop-instances -> {out}")
    if db and db["DBInstanceStatus"] == "available":
        out = ops.aws("rds", "stop-db-instance", "--db-instance-identifier", res["db_instance_id"], "--query",
                      "DBInstance.DBInstanceStatus", "--output", "text", raw=True)
        log(f"rds stop-db-instance -> {out}")
    wait_state(ops, res["instance_id"], "stopped")
    while db and describe_db(ops, res["db_instance_id"])["DBInstanceStatus"] != "stopped":
        time.sleep(15)
    print("Readback:")
    print(f"  EC2 {short(res['instance_id'])}: {describe_instance(ops, res['instance_id'])['State']['Name']}")
    if db:
        print(f"  RDS {short(res['db_instance_id'])}: {describe_db(ops, res['db_instance_id'])['DBInstanceStatus']}")
    for role in ("gw", "consumer"):
        if res.get(f"iot_{role}_cert_id"):
            st = ops.aws("iot", "describe-certificate", "--certificate-id", res[f"iot_{role}_cert_id"], "--query",
                         "certificateDescription.status", "--output", "text", raw=True)
            print(f"  IoT {role} certificate {short(res[f'iot_{role}_cert_id'])}: {st}")
    print("Kept (ID last 4): EC2 {}, SG {}, key {}, EBS {}, ENI {}".format(
        *(short(res[k]) for k in ("instance_id", "sg_id", "key_pair_id", "volume_id", "eni_id"))))
    if db:
        print("Kept (ID last 4): RDS {}, subnets {}, route table {}, SG-db {}, subnet group {}".format(
            short(res["db_instance_id"]), [short(s) for s in res["db_subnet_ids"]], short(res["db_route_table_id"]),
            short(res["db_sg_id"]), res["db_subnet_group"]))


# ---------- W5：db-up ----------

DB_ENV = LOCAL / "db.env"
DB_NAME = "inspection"
DB_USER = "inspection_app"


def pick_private_cidrs(ops, vpc_id, count=2):
    vpc = ops.aws("ec2", "describe-vpcs", "--vpc-ids", vpc_id)["Vpcs"][0]
    used = [ipaddress.ip_network(s["CidrBlock"]) for s in ops.aws(
        "ec2", "describe-subnets", "--filters", f"Name=vpc-id,Values={vpc_id}")["Subnets"]]
    free = [n for n in reversed(list(ipaddress.ip_network(vpc["CidrBlock"]).subnets(new_prefix=24)))
            if not any(n.overlaps(u) for u in used)]
    if len(free) < count:
        raise lab.LabError("Not enough free /24 ranges in the VPC.")
    return [str(n) for n in free[:count]]


def write_db_env(values):
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    lab.atomic_write(DB_ENV, "".join(f"{k}={v}\n" for k, v in values.items()))


def read_kv(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def describe_db(ops, ident):
    out, err = ops.aws_code("rds", "describe-db-instances", "--db-instance-identifier", ident)
    return None if err else out["DBInstances"][0]


def cmd_db_up(args):
    ops = Ops()
    cfg = load_config(args)
    res = load(RESOURCES, {})
    if not res.get("sg_id") or not res.get("vpc_id") or not res.get("az"):
        raise lab.LabError("No host SG/VPC recorded; the W3 host must exist first.")
    host_sg = res["sg_id"]
    azs = sorted(z["ZoneName"] for z in ops.aws("ec2", "describe-availability-zones",
                                                 "--filters", "Name=state,Values=available")["AvailabilityZones"])
    other_az = next(z for z in azs if z != res["az"])
    db_azs = [res["az"], other_az]
    cidrs = res.get("db_subnet_cidrs") or pick_private_cidrs(ops, res["vpc_id"])
    ident = res.get("db_instance_id") or f"{cfg['group']}-{cfg['owner']}-w05-db"
    name = f"{cfg['group']}-{cfg['owner']}-w05"
    skip = [k for k in ("db_route_table_id", "db_subnet_ids", "db_subnet_group", "db_sg_id", "db_instance_id")
            if res.get(k)]
    confirm([
        "Plan (W5 db-up.sh) — will CREATE in VPC " + res["vpc_id"] + ":",
        f"  route table  {name}-private: only the implicit local route (no IGW, no NAT)",
        f"  2 subnets    {cidrs[0]} in {db_azs[0]}, {cidrs[1]} in {db_azs[1]}; both explicitly associated to it",
        f"  subnet group {name}-db-subnets (the 2 private subnets)",
        f"  SG-db        {name}-db: inbound TCP 5432 only, source = host SG {host_sg} (no CIDR)",
        f"  RDS          {ident}: PostgreSQL, db.t3.micro, 20 GiB gp3, PubliclyAccessible=false, encrypted,",
        f"               single-AZ in {db_azs[0]}, initial DB '{DB_NAME}', user '{DB_USER}', backups off",
        "  secret       password generated here -> .local/db.env (600); never printed or put on a command line",
        f"  tags         course={lab.COURSE} week=w05 group={cfg['group']} owner={cfg['owner']}",
        "  cost         RDS db.t3.micro + 20 GiB gp3 while running (storage also while stopped)",
        "  skip         already recorded: " + (", ".join(skip) or "none"),
    ])
    t0 = time.monotonic()
    res["db_subnet_cidrs"] = cidrs
    save_resources(res)

    if not res.get("db_route_table_id"):
        rt = ops.aws("ec2", "create-route-table", "--vpc-id", res["vpc_id"], "--tag-specifications",
                     tag_spec("route-table", tags_for(cfg, "w05", f"{name}-private")))["RouteTable"]["RouteTableId"]
        res["db_route_table_id"] = rt
        save_resources(res)
        log(f"created route table {rt}")
    if not res.get("db_subnet_ids"):
        res["db_subnet_ids"] = []
    for i, (cidr, az) in enumerate(zip(cidrs, db_azs)):
        if i < len(res["db_subnet_ids"]):
            continue
        sn = ops.aws("ec2", "create-subnet", "--vpc-id", res["vpc_id"], "--cidr-block", cidr,
                     "--availability-zone", az, "--tag-specifications",
                     tag_spec("subnet", tags_for(cfg, "w05", f"{name}-private-{az}")))["Subnet"]["SubnetId"]
        res["db_subnet_ids"].append(sn)
        save_resources(res)
        assoc = ops.aws("ec2", "associate-route-table", "--route-table-id", res["db_route_table_id"],
                        "--subnet-id", sn)["AssociationId"]
        log(f"created subnet {sn} ({cidr}, {az}); associated to {res['db_route_table_id']} ({assoc})")
    routes = ops.aws("ec2", "describe-route-tables", "--route-table-ids", res["db_route_table_id"])["RouteTables"][0]
    log("private route table routes: " + json.dumps(
        [{"dest": r.get("DestinationCidrBlock"), "target": r.get("GatewayId")} for r in routes["Routes"]]))
    if not res.get("db_subnet_group"):
        ops.aws("rds", "create-db-subnet-group", "--db-subnet-group-name", f"{name}-db-subnets",
                "--db-subnet-group-description", f"W5 private subnets {cfg['group']}-{cfg['owner']}",
                "--subnet-ids", *res["db_subnet_ids"], "--tags", json.dumps(tags_for(cfg, "w05", f"{name}-db-subnets")))
        res["db_subnet_group"] = f"{name}-db-subnets"
        save_resources(res)
        log(f"created DB subnet group {res['db_subnet_group']}")
    if not res.get("db_sg_id"):
        sg = ops.aws("ec2", "create-security-group", "--group-name", f"{name}-db", "--vpc-id", res["vpc_id"],
                     "--description", "W5 PostgreSQL 5432 from the host SG only",
                     "--tag-specifications", tag_spec("security-group", tags_for(cfg, "w05", f"{name}-db")))["GroupId"]
        res["db_sg_id"] = sg
        save_resources(res)
        perm = [{"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
                 "UserIdGroupPairs": [{"GroupId": host_sg, "Description": "inspection host SG"}]}]
        ops.aws("ec2", "authorize-security-group-ingress", "--group-id", sg, "--ip-permissions", json.dumps(perm))
        log(f"created SG-db {sg}: TCP 5432 from {host_sg}")
    if not res.get("db_instance_id"):
        password = secrets.token_urlsafe(24)
        write_db_env({"DB_HOST": "", "DB_PORT": "5432", "DB_NAME": DB_NAME, "DB_USER": DB_USER,
                      "DB_PASSWORD": password})
        request = {"DBInstanceIdentifier": ident, "Engine": "postgres", "DBInstanceClass": "db.t3.micro",
                   "AllocatedStorage": 20, "StorageType": "gp3", "PubliclyAccessible": False,
                   "StorageEncrypted": True, "MultiAZ": False, "AvailabilityZone": db_azs[0],
                   "DBName": DB_NAME, "MasterUsername": DB_USER, "MasterUserPassword": password,
                   "DBSubnetGroupName": res["db_subnet_group"], "VpcSecurityGroupIds": [res["db_sg_id"]],
                   "BackupRetentionPeriod": 0, "CopyTagsToSnapshot": True,
                   "Tags": tags_for(cfg, "w05", ident)}
        with tempfile.NamedTemporaryFile("w", dir=LOCAL, prefix=".rds-", suffix=".json", delete=False) as tmp:
            os.chmod(tmp.name, 0o600)
            json.dump(request, tmp)
        try:
            ops.aws("rds", "create-db-instance", "--cli-input-json", f"file://{tmp.name}")
        finally:
            os.unlink(tmp.name)
        del password, request
        res["db_instance_id"] = ident
        res["db_created_at"] = now()
        save_resources(res)
        log(f"create-db-instance {ident} requested (password only in .local/db.env, 600)")
    last = None
    while time.monotonic() - t0 < 1800:
        db = describe_db(ops, ident)
        if db and db["DBInstanceStatus"] != last:
            last = db["DBInstanceStatus"]
            log(f"RDS status: {last} ({time.monotonic() - t0:.0f}s)")
        if last == "available":
            break
        time.sleep(15)
    if last != "available":
        raise lab.LabError("RDS did not become available in 30 minutes; do NOT rerun blindly, inspect first.")
    env = read_kv(DB_ENV)
    env["DB_HOST"] = db["Endpoint"]["Address"]
    write_db_env(env)
    print("Readback:")
    print(json.dumps({"DBInstanceIdentifier": "...." + ident[-4:], "DBInstanceStatus": db["DBInstanceStatus"],
                      "PubliclyAccessible": db["PubliclyAccessible"], "Engine": db["Engine"],
                      "EngineVersion": db["EngineVersion"], "DBInstanceClass": db["DBInstanceClass"],
                      "AllocatedStorage": db["AllocatedStorage"], "StorageType": db["StorageType"],
                      "StorageEncrypted": db["StorageEncrypted"], "MultiAZ": db["MultiAZ"],
                      "AvailabilityZone": db["AvailabilityZone"], "DBName": db.get("DBName"),
                      "VpcSecurityGroups": [g["VpcSecurityGroupId"] for g in db["VpcSecurityGroups"]],
                      "DBSubnetGroup": db["DBSubnetGroup"]["DBSubnetGroupName"]}, indent=2))
    log(f"db-up.sh total {time.monotonic() - t0:.0f}s; .local/db.env updated with the endpoint (mode "
        f"{oct(DB_ENV.stat().st_mode & 0o777)[2:]})")


# ---------- W6：iot-up ----------

IOT_DIR = LOCAL / "iot"
ROOT_CA_URL = "https://www.amazontrust.com/repository/AmazonRootCA1.pem"


def iot_names(cfg):
    code = f"{cfg['group']}-{cfg['owner']}"
    return {"code": code, "topic": f"cxe/{code}/gw/events", "gw_client": f"gw-{code}",
            "consumer_client": f"consumer-{code}", "gw_policy": f"cxe-{code}-gw",
            "consumer_policy": f"cxe-{code}-consumer",
            "devices": [f"{code}-d{i:02d}" for i in range(1, 11)]}


def write_secret_file(path, text):
    IOT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    IOT_DIR.chmod(0o700)
    if path.exists():
        raise lab.LabError(f"{path.name} already exists; refusing to overwrite a key or certificate.")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


def cmd_iot_up(args):
    ops = Ops()
    cfg = load_config(args)
    res = load(RESOURCES, {})
    n = iot_names(cfg)
    arn = f"arn:aws:iot:{ops.region}:{ops.ctx['account']}"
    policies = {
        "gw": {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "iot:Connect", "Resource": f"{arn}:client/{n['gw_client']}"},
            {"Effect": "Allow", "Action": "iot:Publish", "Resource": f"{arn}:topic/{n['topic']}"}]},
        "consumer": {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "iot:Connect", "Resource": f"{arn}:client/{n['consumer_client']}"},
            {"Effect": "Allow", "Action": "iot:Subscribe", "Resource": f"{arn}:topicfilter/{n['topic']}"},
            {"Effect": "Allow", "Action": "iot:Receive", "Resource": f"{arn}:topic/{n['topic']}"}]},
    }
    skip = [k for k in ("iot_gw_cert_id", "iot_consumer_cert_id", "iot_gw_policy", "iot_consumer_policy")
            if res.get(k)]
    confirm([
        "Plan (W6 iot-up.sh) — will CREATE in AWS IoT Core:",
        f"  2 certificates (ACTIVE): gateway, consumer; private keys written straight to .local/iot/ (700/600)",
        f"  policy {n['gw_policy']}: Connect client/{n['gw_client']}; Publish topic/{n['topic']}",
        f"  policy {n['consumer_policy']}: Connect client/{n['consumer_client']}; "
        f"Subscribe topicfilter/{n['topic']}; Receive topic/{n['topic']}",
        "  attach each policy to its certificate; download Amazon Root CA 1; no wildcards",
        "  cost   idle certificates/policies are free; messages billed per million (tiny here)",
        "  skip   already recorded: " + (", ".join(skip) or "none"),
    ])
    IOT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    IOT_DIR.chmod(0o700)
    endpoint = ops.aws("iot", "describe-endpoint", "--endpoint-type", "iot:Data-ATS")["endpointAddress"]
    (IOT_DIR / "endpoint").write_text(endpoint + "\n")
    log(f"endpoint saved to .local/iot/endpoint ({endpoint.split('.')[0][:4]}...{endpoint[-24:]})")
    for role in ("gw", "consumer"):
        key = f"iot_{role}_cert_id"
        if not res.get(key):
            out = ops.aws("iot", "create-keys-and-certificate", "--set-as-active")  # 輸出只留在記憶體
            write_secret_file(IOT_DIR / f"{role}.private.key", out["keyPair"]["PrivateKey"])
            write_secret_file(IOT_DIR / f"{role}.cert.pem", out["certificatePem"])
            res[key] = out["certificateId"]
            res[f"iot_{role}_cert_arn"] = out["certificateArn"]
            save_resources(res)
            del out
            log(f"created {role} certificate {res[key]} (key/cert written to .local/iot/, not shown)")
        pkey = f"iot_{role}_policy"
        if not res.get(pkey):
            name = n[f"{role}_policy"]
            ops.aws("iot", "create-policy", "--policy-name", name, "--policy-document", json.dumps(policies[role]),
                    "--tags", json.dumps(tags_for(cfg, "w06", name)[:4]))
            res[pkey] = name
            save_resources(res)
            log(f"created policy {name}")
        ops.aws("iot", "attach-policy", "--policy-name", res[pkey], "--target", res[f"iot_{role}_cert_arn"])
        log(f"attached {res[pkey]} -> certificate {res[key]}")
    ca = IOT_DIR / "AmazonRootCA1.pem"
    if not ca.exists():
        subprocess.run(["curl", "--fail", "--silent", "--show-error", "--proto", "=https", "--tlsv1.2",
                        "-o", str(ca), ROOT_CA_URL], check=True)
        ca.chmod(0o600)
        log("downloaded Amazon Root CA 1 to .local/iot/AmazonRootCA1.pem")
    print("Readback:")
    for role in ("gw", "consumer"):
        cert = ops.aws("iot", "describe-certificate", "--certificate-id", res[f"iot_{role}_cert_id"])
        attached = ops.aws("iot", "list-attached-policies", "--target", res[f"iot_{role}_cert_arn"])["policies"]
        print(f"  {role} certificate {res[f'iot_{role}_cert_id'][:12]}...: "
              f"{cert['certificateDescription']['status']}; policies {[p['policyName'] for p in attached]}")
    modes = sorted(f"{oct(p.stat().st_mode & 0o777)[2:]} {p.name}" for p in IOT_DIR.iterdir())
    print(f"  .local/iot ({oct(IOT_DIR.stat().st_mode & 0o777)[2:]}): {modes}")


def iot_payloads(cfg):
    """W6：consumer 需要的 IOT_ 設定行與三個檔案；.local/iot 不存在時回傳 None。"""
    if not (IOT_DIR / "consumer.private.key").exists():
        return None
    n = iot_names(cfg)
    files = {name: (IOT_DIR / name).read_bytes()
             for name in ("AmazonRootCA1.pem", "consumer.cert.pem", "consumer.private.key")}
    lines = {"IOT_ENDPOINT": (IOT_DIR / "endpoint").read_text().strip(), "IOT_TOPIC": n["topic"],
             "IOT_CLIENT_ID": n["consumer_client"], "IOT_DEVICE_IDS": ",".join(n["devices"]),
             "IOT_CA": "/etc/inspection/iot/AmazonRootCA1.pem",
             "IOT_CERT": "/etc/inspection/iot/consumer.cert.pem",
             "IOT_KEY": "/etc/inspection/iot/consumer.private.key"}
    return "".join(f"{k}={v}\n" for k, v in lines.items()).encode(), files


PUT_IOT_FILE = ("sudo sh -c 'umask 077; install -d -o inspection -g inspection -m 700 /etc/inspection/iot && "
                "cat > /etc/inspection/iot/.NAME.new && chown inspection:inspection /etc/inspection/iot/.NAME.new && "
                "chmod 600 /etc/inspection/iot/.NAME.new && mv -f /etc/inspection/iot/.NAME.new "
                "/etc/inspection/iot/NAME'")


# ---------- 進入點 ----------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up", help="W3：建立 SG、key pair、一台主機並部署指定 commit")
    up.add_argument("--commit", default="HEAD")
    down = sub.add_parser("down", help="W3：以記錄的 ID 回收（--stop 只停止）")
    down.add_argument("--stop", action="store_true")
    start = sub.add_parser("start", help="W4 T1：啟動保留的主機、記錄新位址、核對 SG 的 /32")
    deploy = sub.add_parser("deploy", help="W4：把已 commit 的版本與秘密檔部署到同一台主機")
    deploy.add_argument("--commit", default="HEAD")
    db_up = sub.add_parser("db-up", help="W5：私有子網×2、路由表、子網群組、SG-db、RDS PostgreSQL")
    iot_up = sub.add_parser("iot-up", help="W6：IoT 憑證×2、精確政策×2、Root CA")
    keep = sub.add_parser("keep", help="W5 T4／W6 步驟 8：停止主機與 RDS 並讀回保留清單")
    for p in (up, down, start, deploy, db_up, iot_up, keep):
        p.add_argument("--group")
        p.add_argument("--owner")
        p.add_argument("--source-cidr", dest="source_cidr")
    args = parser.parse_args()
    try:
        {"up": cmd_up, "down": cmd_down, "start": cmd_start, "deploy": cmd_deploy,
         "db-up": cmd_db_up, "iot-up": cmd_iot_up, "keep": cmd_keep}[args.command](args)
    except (lab.LabError, EOFError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
        print(f"STOP: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
