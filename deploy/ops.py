#!/usr/bin/env python3
"""g06-m01 的建立／部署／回收腳本本體；由 deploy/*.sh 呼叫。

- 只處理 .local/resources.json 記錄的 ID；刪除前核對標籤，不依名稱搜尋。
- 每建立一項立刻寫入 .local/resources.json（.local/ 不進 Git）。
- 執行前印出將建立／刪除的清單並等輸入 yes；不印出憑證、權杖、私鑰或密碼。
- AWS 呼叫一律經 scripts/lab.py 的 run_aws（clean_env、learnerlab profile）。
"""
import argparse
import json
import re
import subprocess
import sys
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
    return subprocess.run(["ssh", "-i", str(SSH_KEY), "-o", "StrictHostKeyChecking=accept-new",
                           "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"ec2-user@{host}", command],
                          input=stdin, capture_output=True, timeout=timeout)


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
    if inst["State"]["Name"] == "stopped":
        confirm([f"Plan (start): START instance {res['instance_id']} (stopped); no new resources"])
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
    warn_dirty()
    sha, user_data, size = build_user_data(args.commit)
    confirm([
        "Plan (deploy.sh) — update the SAME host, no new resources:",
        f"  target   {res['instance_id']} at {ip}",
        f"  commit   {sha} (user data {size} bytes, committed files only)",
        f"  secrets  .local/{' + .local/'.join(secret_files)} -> /etc/inspection/app.env (root, 600) via SSH stdin",
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
    for p in (up, down, start, deploy):
        p.add_argument("--group")
        p.add_argument("--owner")
        p.add_argument("--source-cidr", dest="source_cidr")
    args = parser.parse_args()
    try:
        {"up": cmd_up, "down": cmd_down, "start": cmd_start, "deploy": cmd_deploy}[args.command](args)
    except (lab.LabError, EOFError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
        print(f"STOP: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
