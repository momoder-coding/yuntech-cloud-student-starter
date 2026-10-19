#!/usr/bin/env python3
"""W2：私有 bucket、合成文字、短效分享與精確回收。

用法：python3 tools/w02_private_s3.py --group g06 --owner m01
執行前印出將建立／刪除的清單並等確認；短效 URL 只留在記憶體，不印出、不寫檔。
"""
import argparse
import hashlib
import json
import re
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import lab  # noqa: E402

LOCAL = ROOT / ".local"
RESOURCES = LOCAL / "w02-resources.json"
KEY = "inspection/w02-note.txt"
PRESIGN_SECONDS = 15


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def s3_error_code(body):
    match = re.search(rb"<Code>([A-Za-z]+)</Code>", body or b"")
    return match.group(1).decode() if match else "-"


def http_get(url):
    """回傳 (HTTP 狀態, bytes)；不印出 URL。"""
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def save_resources(data):
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    lab.atomic_write(RESOURCES, json.dumps(data, indent=2) + "\n")


def put_and_check(region, bucket, text, workdir, label):
    src, dst = Path(workdir) / f"{label}.txt", Path(workdir) / f"{label}.out"
    src.write_bytes(text.encode("utf-8"))
    put = lab.run_aws(["s3api", "put-object", "--bucket", bucket, "--key", KEY, "--body", str(src),
                       "--content-type", "text/plain; charset=utf-8"], region)
    lab.run_aws(["s3api", "get-object", "--bucket", bucket, "--key", KEY, str(dst)], region)
    same = src.read_bytes() == dst.read_bytes()
    log(f"{label}: put ETag={put.get('ETag')} bytes={len(src.read_bytes())} "
        f"sha256 local={sha(src.read_bytes())[:16]} downloaded={sha(dst.read_bytes())[:16]} identical={same}")
    if not same:
        raise lab.LabError("Downloaded bytes differ from uploaded bytes.")
    return src.read_bytes()


def cleanup(region, bucket):
    log(f"cleanup: delete only s3://{bucket}/{KEY}")
    lab.run_aws(["s3api", "delete-object", "--bucket", bucket, "--key", KEY], region)
    left = lab.run_aws(["s3api", "list-objects-v2", "--bucket", bucket, "--query", "KeyCount"], region) or 0
    log(f"objects left in bucket: {left}")
    if left:
        raise lab.LabError("Bucket still has objects that this lab did not create; not deleting it.")
    lab.run_aws(["s3api", "delete-bucket", "--bucket", bucket], region)
    try:
        lab.run_aws(["s3api", "head-bucket", "--bucket", bucket], region)
        raise lab.LabError("Bucket still exists after delete.")
    except lab.LabError as exc:
        if "404" not in str(exc) and "NoSuchBucket" not in str(exc):
            raise
        log(f"readback head-bucket: 404 Not Found -> bucket {bucket} no longer exists")


def run(args):
    ctx = lab.verify()
    region = ctx["region"]
    bucket = f"yuntech-{args.group}-{args.owner}-w02-{secrets.token_hex(3)}".lower()
    tags = {"course": lab.COURSE, "week": "w02", "group": args.group, "owner": args.owner}
    print("Plan (W2):")
    print(f"  create  S3 bucket {bucket} in {region}, tags {tags}")
    print("  set     public access block: all four = true")
    print(f"  put     1 synthetic text object {KEY} (twice: create + update)")
    print(f"  share   1 presigned GET URL, {PRESIGN_SECONDS}s, kept in memory only")
    print(f"  delete  object {KEY}, then bucket {bucket}; read back 404")
    print("  cost    S3 standard storage of a few bytes for a few minutes (~0)")
    if input("Type yes to proceed: ").strip() != "yes":
        raise lab.LabError("Cancelled; no changes made.")

    lab.run_aws(["s3api", "create-bucket", "--bucket", bucket], region)
    save_resources({"bucket": bucket, "region": region, "created_at": now(), "deleted": False})
    log(f"created bucket {bucket}")
    try:
        tagset = json.dumps({"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]})
        lab.run_aws(["s3api", "put-bucket-tagging", "--bucket", bucket, "--tagging", tagset], region)
        lab.run_aws(["s3api", "put-public-access-block", "--bucket", bucket, "--public-access-block-configuration",
                     "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"],
                    region)
        pab = lab.run_aws(["s3api", "get-public-access-block", "--bucket", bucket], region)
        log("public access block readback: " + json.dumps(pab["PublicAccessBlockConfiguration"]))
        got = lab.run_aws(["s3api", "get-bucket-tagging", "--bucket", bucket], region)
        log("tag readback: " + json.dumps({t["Key"]: t["Value"] for t in got["TagSet"]}, sort_keys=True))

        with tempfile.TemporaryDirectory(prefix="w02-") as workdir:
            put_and_check(region, bucket, f"inspection note v1 device={args.group}-d01 status=ok at {now()}\n",
                          workdir, "v1")
            v2 = put_and_check(region, bucket, f"inspection note v2 device={args.group}-d01 status=anomaly "
                               f"(updated) at {now()}\n", workdir, "v2")

        status, body = http_get(f"https://{bucket}.s3.amazonaws.com/{KEY}")
        log(f"anonymous GET (no signature): HTTP {status} code={s3_error_code(body)}")

        url = lab.run_aws(["s3", "presign", f"s3://{bucket}/{KEY}", "--expires-in", str(PRESIGN_SECONDS)],
                          region, raw=True)
        issued = time.monotonic()
        status, body = http_get(url)
        log(f"presigned GET within {PRESIGN_SECONDS}s: HTTP {status} bytes_identical_to_v2={body == v2}")
        time.sleep(max(0.0, PRESIGN_SECONDS + 5 - (time.monotonic() - issued)))
        status, body = http_get(url)
        log(f"presigned GET after expiry (+{PRESIGN_SECONDS + 5}s): HTTP {status} code={s3_error_code(body)}")
        del url
    finally:
        cleanup(region, bucket)
        save_resources({"bucket": bucket, "region": region, "deleted": True, "deleted_at": now()})


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--group", required=True, help="組名，例如 g06")
    parser.add_argument("--owner", required=True, help="組內代號，例如 m01（不用學號）")
    args = parser.parse_args()
    for value in (args.group, args.owner):
        if not re.fullmatch(r"[a-z0-9]{1,10}", value):
            parser.error("group/owner must be 1-10 lowercase letters or digits")
    try:
        run(args)
    except (lab.LabError, EOFError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
