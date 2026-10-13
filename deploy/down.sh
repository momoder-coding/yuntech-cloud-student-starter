#!/usr/bin/env bash
# W3 T4：只回收 .local/resources.json 裡屬於本組（course/week/group/owner 標籤核對通過）的資源。
# down.sh --stop 只停止主機、不刪除；down.sh（預設）終止主機並刪除 root EBS、ENI、SG、key pair。
# 不依名稱搜尋、不大量刪除。所有 AWS 呼叫都經 deploy/awsx.py。
set -euo pipefail

cd "$(dirname "$0")/.."

RES=.local/resources.json
EVID=.local/w03-evidence
WEEK=w03
COURSE=yuntech-115-1
MODE=terminate
[[ ${1:-} == --stop ]] && MODE=stop
[[ -f $RES ]] || { echo "STOP: 找不到 $RES" >&2; exit 1; }

awsx() { python3 deploy/awsx.py "$@"; }
read_key() { python3 -c 'import json,sys; print(json.loads(open(sys.argv[1]).read()).get(sys.argv[2],""))' "$RES" "$1"; }
utcnow() { date -u +%Y-%m-%dT%H:%M:%SZ; }
lookup() { python3 deploy/awsx.py "$@" >/dev/null 2>&1; }

IID=$(read_key w03_instance_id); SG_ID=$(read_key w03_sg_id)
KP_NAME=$(read_key w03_key_name); VOL_ID=$(read_key w03_volume_id); ENI_ID=$(read_key w03_eni_id)
[[ -n $IID ]] || { echo "STOP: $RES 沒有 w03_instance_id，沒有可回收的主機" >&2; exit 1; }
GROUP=$(read_key w03_group); OWNER=$(read_key w03_owner)
[[ -f .local/w03.env ]] && . .local/w03.env

# ── 標籤核對：不是自己的資源不碰 ─────────────────────────
TAGS=$(awsx ec2 describe-instances --instance-ids "$IID" \
  --query 'Reservations[0].Instances[0].Tags' --output json)
python3 - "$TAGS" "$COURSE" "$WEEK" "${GROUP:-}" "${OWNER:-}" <<'PY' || { echo "STOP: 標籤不符，這台不是本課程本週的資源，停止" >&2; exit 1; }
import json, sys
tags = {t["Key"]: t["Value"] for t in json.loads(sys.argv[1])}
want = {"course": sys.argv[2], "week": sys.argv[3]}
if sys.argv[4]: want["group"] = sys.argv[4]
if sys.argv[5]: want["owner"] = sys.argv[5]
bad = {k: (tags.get(k), v) for k, v in want.items() if tags.get(k) != v}
print("標籤核對：", "通過" if not bad else f"不通過（欄位：目前值, 預期值）{bad}")
sys.exit(0 if not bad else 1)
PY

if [[ $MODE == stop ]]; then
  cat <<PLAN

── 即將執行：停止（不刪除）──────────────────────────
  instance $IID   SG $SG_ID   key pair $KP_NAME
  停止後公開位址會改變，資源保留給下一週；磁碟仍可能計費。
PLAN
  read -r -p "確認請輸入 STOP：" a; [[ $a == STOP ]] || { echo "取消"; exit 1; }
  awsx ec2 stop-instances --instance-ids "$IID" --query 'StoppingInstances[0].CurrentState.Name' --output text
  state=""
  for _ in $(seq 1 120); do
    state=$(awsx ec2 describe-instances --instance-ids "$IID" \
            --query 'Reservations[0].Instances[0].State.Name' --output text)
    if [[ $state == stopped ]]; then break; fi
    sleep 5
  done
  echo "讀回：instance $IID = $state（$(utcnow)）"
  [[ $state == stopped ]] || { echo "STOP: 未到 stopped" >&2; exit 1; }
  exit 0
fi

cat <<PLAN

── 即將刪除 ──────────────────────────────────────────
  1. instance   $IID      → terminate（根磁碟、ENI 隨之刪除）
  2. root EBS   $VOL_ID   → 隨終止刪除，仍讀回
  3. ENI        $ENI_ID   → 隨終止刪除，仍讀回
  4. SG         $SG_ID    → 確認 ENI 清除後以 ID 刪除
  5. key pair   $KP_NAME  → 以名稱刪除（私鑰留在本機 $HOME/.ssh/）
  不刪除：預設 VPC／子網／路由表／IGW／default SG
PLAN
read -r -p "確認請輸入 DELETE：" a; [[ $a == DELETE ]] || { echo "取消，未刪除任何資源"; exit 1; }

mkdir -p "$EVID"
awsx ec2 terminate-instances --instance-ids "$IID" --query 'TerminatingInstances[0].CurrentState.Name' --output text
state=""
for _ in $(seq 1 180); do
  state=$(awsx ec2 describe-instances --instance-ids "$IID" \
          --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || echo gone)
  if [[ $state == terminated || $state == gone ]]; then break; fi
  sleep 5
done
echo "[1] instance 終止讀回：$state（$(utcnow)）"
[[ $state == terminated || $state == gone ]] || { echo "STOP: 未到 terminated" >&2; exit 1; }

for i in $(seq 1 18); do
  lookup ec2 describe-volumes --volume-ids "$VOL_ID" || break
  sleep 10
done
if lookup ec2 describe-volumes --volume-ids "$VOL_ID"; then
  echo "[2] root EBS $VOL_ID 仍存在 ← 需要處理" >&2; VOL_GONE=no
else
  echo "[2] root EBS $VOL_ID 查無（$(utcnow)）"; VOL_GONE=yes
fi

if lookup ec2 describe-network-interfaces --network-interface-ids "$ENI_ID"; then
  echo "STOP: ENI $ENI_ID 還在，先不刪 SG" >&2; exit 1
fi
echo "[3] ENI $ENI_ID 查無（$(utcnow)）"

awsx ec2 delete-security-group --group-id "$SG_ID" >/dev/null
if lookup ec2 describe-security-groups --group-ids "$SG_ID"; then
  echo "[4] SG 仍在 ← 需要處理" >&2; SG_GONE=no
else
  echo "[4] SG $SG_ID 查無（$(utcnow)）"; SG_GONE=yes
fi

awsx ec2 delete-key-pair --key-name "$KP_NAME" >/dev/null
if lookup ec2 describe-key-pairs --key-names "$KP_NAME"; then
  echo "[5] key pair 仍在 ← 需要處理" >&2; KP_GONE=no
else
  echo "[5] key pair $KP_NAME 查無（$(utcnow)）"; KP_GONE=yes
fi

python3 - "$EVID/destroyed.json" "$IID" "$VOL_GONE" "$SG_GONE" "$KP_GONE" <<'PY'
import json, sys, datetime
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({
    "utc": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "instance_id": sys.argv[2], "instance_state": "terminated",
    "root_ebs_not_found": sys.argv[3] == "yes",
    "security_group_not_found": sys.argv[4] == "yes",
    "key_pair_not_found": sys.argv[5] == "yes",
}, indent=2) + "\n", encoding="utf-8")
PY
echo "回收紀錄寫入 $EVID/destroyed.json（main 的 terminated 紀錄會暫留一陣子，屬正常）"
