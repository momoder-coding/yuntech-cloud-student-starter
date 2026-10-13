#!/usr/bin/env bash
# W3 T2：建立本週範圍（1 個 SG、1 個 key pair、1 台 t3.micro）並部署雛型。
# 只建立這些；每建立一項立刻記進 .local/resources.json；結束條件＝/health 200 且 version 等於 commit。
# 所有 AWS 呼叫都經 deploy/awsx.py（scripts/lab.py 的 clean_env/run_aws）。
set -euo pipefail

cd "$(dirname "$0")/.."

COURSE=yuntech-115-1
WEEK=w03
CONF=.local/w03.env
RES=.local/resources.json
EVID=.local/w03-evidence
CONFIRM_WORD=CREATE

[[ -f $CONF ]] && . "$CONF"
: "${GROUP:?請在 .local/w03.env 設定 GROUP=}"
: "${OWNER:?請在 .local/w03.env 設定 OWNER=}"
CODE=${CODE:-$GROUP-$OWNER}
COMMIT=${COMMIT:-$(git rev-parse HEAD)}

mkdir -p "$EVID"
awsx() { python3 deploy/awsx.py "$@"; }

record() {
  python3 - "$RES" "$1" "$2" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True)
data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
data[sys.argv[2]] = sys.argv[3]
p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
}

utcnow() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ── 前置檢查 ─────────────────────────────────────────────
# 打包器（deploy/make_user_data.py）只取該 commit 的 app/ 與 deploy/nginx.conf，
# 所以只要求會被打包的路徑乾淨；其他檔案（例如本機的 opencode.json 設定）髒掉只警告。
if [[ -n $(git status --porcelain -- app deploy) ]]; then
  echo "STOP: app/ 或 deploy/ 有未 commit 的修改；未 commit 的內容不會進 user data，先 commit 再跑。" >&2
  git status --porcelain -- app deploy >&2
  exit 1
fi
other=$(git status --porcelain -- . ":(exclude)app" ":(exclude)deploy" || true)
if [[ -n $other ]]; then
  echo "提醒：以下檔案有未 commit 的修改，不在打包範圍內（僅警告，不影響部署內容）："
  printf '%s\n' "$other"
fi
if [[ -f $RES ]] && python3 - "$RES" <<'PY'
import json, sys
from pathlib import Path
d = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
sys.exit(0 if d.get("w03_instance_id") else 1)
PY
then
  echo "STOP: .local/resources.json 已經有 w03_instance_id，不要建第二台。要更換主機請先跑 down.sh" >&2
  exit 1
fi

VPC=$(awsx ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SUBNET=${SUBNET:-$(awsx ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" Name=default-for-az,Values=true \
  --query 'sort_by(Subnets,&AvailabilityZone)[0].SubnetId' --output text)}
# kernel-6.1 是 AL2023 的預設核心。不加這個條件時，多個版本的 CreationDate 可能同分，
# sort_by 會任意挑到 kernel-6.12/6.18，造成每次部署的 AMI 不可重現。要換核心請在 .local/w03.env 設 AMI。
AMI=${AMI:-$(awsx ec2 describe-images --owners amazon \
  --filters Name=name,Values='al2023-ami-2023*-kernel-6.1-x86_64' Name=state,Values=available Name=architecture,Values=x86_64 \
  --query 'Images|sort_by(@,&CreationDate)[-1].ImageId' --output text)}
CIDR=${CIDR:-$(curl -s https://checkip.amazonaws.com | tr -d '\n')/32}
SG_NAME=${CODE}-w03-sg
KP_NAME=${CODE}-w03
KEY_PATH=$HOME/.ssh/${CODE}-w03-ed25519

cat <<PLAN

── 即將建立（W3，$WEEK，代號 $CODE）────────────────────────
  既有、僅引用不修改：VPC $VPC、子網 $SUBNET
  1. Security group  $SG_NAME（入站 TCP 22、80，來源 $CIDR）
  2. Key pair        $KP_NAME（本機 $KEY_PATH，只匯入公鑰）
  3. EC2 instance    t3.micro、AMI $AMI、root EBS gp3 8GiB 加密、終止時刪除、IMDSv2 required
  部署 commit        $COMMIT
  標籤               course=$COURSE week=$WEEK group=$GROUP owner=$OWNER
  費用               約 US$0.05／數小時（依當期 EC2、EBS、公開 IPv4 計價頁）
  回收               deploy/down.sh --stop（保留）或 deploy/down.sh（終止並刪除 SG、key pair）
PLAN
read -r -p "確認無誤請輸入 $CONFIRM_WORD：" answer
[[ $answer == "$CONFIRM_WORD" ]] || { echo "取消，未建立任何資源"; exit 1; }

# ── 1. Security group ────────────────────────────────────
SG_ID=$(awsx ec2 create-security-group --group-name "$SG_NAME" \
  --description "W3 course SG $CODE (course=$COURSE)" --vpc-id "$VPC" \
  --tag-specifications "ResourceType=security-group,Tags=[{Key=Name,Value=$SG_NAME},{Key=course,Value=$COURSE},{Key=week,Value=$WEEK},{Key=group,Value=$GROUP},{Key=owner,Value=$OWNER}]" \
  --query 'GroupId' --output text)
record w03_sg_id "$SG_ID"; record w03_sg_name "$SG_NAME"
awsx ec2 authorize-security-group-ingress --group-id "$SG_ID" --ip-permissions \
  "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$CIDR,Description=Codespace-ssh}]" \
  "IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=$CIDR,Description=Codespace-http}]" >/dev/null
awsx ec2 describe-security-groups --group-ids "$SG_ID" \
  --query 'SecurityGroups[0].IpPermissions[].[IpProtocol,FromPort,ToPort,IpRanges[0].CidrIp]' --output json \
  > "$EVID/sg-rules.json"
echo "[1] SG $SG_ID 已建立，規則寫入 $EVID/sg-rules.json"

# ── 2. Key pair（私鑰只留本機，不讀不印）──────────────────
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
if [[ ! -f $KEY_PATH ]]; then
  ssh-keygen -t ed25519 -N '' -C "$CODE w03" -f "$KEY_PATH" >/dev/null
fi
chmod 600 "$KEY_PATH"
awsx ec2 import-key-pair --key-name "$KP_NAME" --public-key-material "fileb://$KEY_PATH.pub" \
  --query 'KeyName' --output text >/dev/null
KP_ID=$(awsx ec2 describe-key-pairs --key-names "$KP_NAME" --query 'KeyPairs[0].KeyPairId' --output text)
record w03_key_name "$KP_NAME"; record w03_key_pair_id "$KP_ID"
echo "[2] Key pair $KP_NAME（$KP_ID）已匯入（只匯入公鑰）"

# ── 3. 打包 user data ＋ 建立主機 ────────────────────────
rm -f .local/w03-user-data.sh
bash deploy/make-user-data.sh "$COMMIT" .local/w03-user-data.sh >/dev/null
UD_SIZE=$(stat -c %s .local/w03-user-data.sh)
(( UD_SIZE < 16384 )) || { echo "STOP: user data $UD_SIZE bytes 超過 16 KiB" >&2; exit 1; }
echo "[3] user data 已打包：$UD_SIZE bytes（傳給 CLI 的是原始腳本，base64 由 botocore 做一次）"

IID=$(awsx ec2 run-instances --image-id "$AMI" --instance-type t3.micro --count 1 \
  --subnet-id "$SUBNET" --security-group-ids "$SG_ID" --key-name "$KP_NAME" \
  --metadata-options HttpTokens=required,HttpEndpoint=enabled \
  --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":8,"VolumeType":"gp3","Encrypted":true,"DeleteOnTermination":true}}]' \
  --user-data "file://$PWD/.local/w03-user-data.sh" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$CODE-w03},{Key=course,Value=$COURSE},{Key=week,Value=$WEEK},{Key=group,Value=$GROUP},{Key=owner,Value=$OWNER}]" \
  --query 'Instances[0].InstanceId' --output text)
record w03_instance_id "$IID"
record w03_group "$GROUP"
record w03_owner "$OWNER"
record w03_deploy_commit "$COMMIT"
record w03_ami "$AMI"
record w03_subnet "$SUBNET"
record w03_created_utc "$(utcnow)"
echo "[3] instance $IID 已建立（running 時間另行記錄）"

state=""
for _ in $(seq 1 180); do
  state=$(awsx ec2 describe-instances --instance-ids "$IID" \
          --query 'Reservations[0].Instances[0].State.Name' --output text)
  if [[ $state == running ]]; then break; fi
  sleep 5
done
[[ $state == running ]] || { echo "STOP: 主機未到 running（目前 $state）" >&2; exit 1; }
T_RUNNING=$(utcnow)
VOL_ID=$(awsx ec2 describe-instances --instance-ids "$IID" --query 'Reservations[0].Instances[0].BlockDeviceMappings[0].Ebs.VolumeId' --output text)
ENI_ID=$(awsx ec2 describe-instances --instance-ids "$IID" --query 'Reservations[0].Instances[0].NetworkInterfaces[0].NetworkInterfaceId' --output text)
PUBIP=$(awsx ec2 describe-instances --instance-ids "$IID" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
record w03_volume_id "$VOL_ID"; record w03_eni_id "$ENI_ID"; record w03_public_ip "$PUBIP"
echo "[3] running at $T_RUNNING；root EBS $VOL_ID；ENI $ENI_ID；公開位址 $PUBIP"

# ── 4. 服務還沒好時的第一次 curl（真實失敗樣本）──────────
set +e
EARLY_CODE=$(curl -sS --max-time 8 -o "$EVID/early-curl-body.txt" -w '%{http_code}' "http://$PUBIP/health" 2>"$EVID/early-curl-err.txt")
EARLY_RC=$?
set -e
{ echo "UTC $(utcnow)"; echo "curl exit=$EARLY_RC http=$EARLY_CODE"; \
  echo "stderr: $(cat "$EVID/early-curl-err.txt")"; } | tee "$EVID/early-curl.txt"
echo "[4] 首次 curl：exit=$EARLY_RC http=$EARLY_CODE（此時失敗是正常的）"

# ── 5. 等待狀態檢查 2/2 與 /health 200 ───────────────────
deadline=$((SECONDS + 900))
while :; do
  st=$(awsx ec2 describe-instance-status --instance-ids "$IID" \
       --query 'InstanceStatuses[0].InstanceStatus.Status' --output text 2>/dev/null || echo none)
  [[ $st == ok ]] && break
  if (( SECONDS >= deadline )); then echo "STOP: 等不到狀態檢查 2/2" >&2; exit 1; fi
  sleep 10
done
T_STATUS=$(utcnow)
echo "[5] 狀態檢查 2/2 at $T_STATUS"

while :; do
  body=$(curl -s --max-time 8 "http://$PUBIP/health" || true)
  ver=$(python3 -c 'import json,sys; d=sys.stdin.read().strip(); print(json.loads(d).get("version","") if d else "")' <<<"$body")
  if [[ $ver == "$COMMIT" ]]; then
    T_HEALTH=$(utcnow)
    printf '%s\n' "$body" > "$EVID/health.json"
    break
  fi
  if (( SECONDS >= deadline )); then
    echo "STOP: 等不到 /health version=$COMMIT（最後看到 version='$ver'）" >&2
    exit 1
  fi
  sleep 10
done
echo "[5] /health 200 且 version=$COMMIT at $T_HEALTH"

# ── 6. 五層紀錄與主機內觀察（SSH 失敗不影響建立結果）────
python3 - "$EVID/timings.json" "$T_RUNNING" "$T_STATUS" "$T_HEALTH" "$COMMIT" <<'PY'
import json, sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({
    "instance_running_utc": sys.argv[2],
    "status_checks_2_of_2_utc": sys.argv[3],
    "health_200_version_commit_utc": sys.argv[4],
    "deploy_commit": sys.argv[5],
}, indent=2) + "\n", encoding="utf-8")
PY
echo "[6] 五層時間寫入 $EVID/timings.json（cloud-init／監聽埠的觀察見下方 SSH 區塊）"

if ssh -i "$KEY_PATH" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 \
     -o BatchMode=yes "ec2-user@$PUBIP" \
     'echo "host=$(hostname) az=$(curl -s http://169.254.169.254/latest/meta-data/placement/availability-zone)"; \
      cloud-init status --wait; systemctl is-active nginx inspection; \
      sudo ss -lntp | grep -E ":80|:8080"' 2>&1 | tee "$EVID/ssh-observations.txt"; then
  echo "[6] SSH 觀察寫入 $EVID/ssh-observations.txt"
else
  echo "[6] SSH 觀察失敗（不影響主機與服務；可稍後再試）"
fi

cat <<DONE

── 完成 ────────────────────────────────────────────────
  instance   $IID（$PUBIP）
  SG         $SG_ID
  key pair   $KP_NAME（私鑰 $KEY_PATH，僅本機）
  root EBS   $VOL_ID      ENI $ENI_ID
  commit     $COMMIT
  證據       $EVID/
  下一步     依 W3 T3 做 (a) SG 移除/加回 80、(b) 停止 inspection 服務
  下課前     deploy/down.sh --stop（保留主機，W4 續用）
DONE
