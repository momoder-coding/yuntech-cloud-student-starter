#!/usr/bin/env bash
# W4：把「已 commit」的版本部署到原本那台主機（不開新主機）。
# 規格：① 只部署 commit 版本 ② SSH 執行安裝腳本、再經 stdin 放秘密檔、再重啟
#       ③ 結束時 /health 的 version 等於本次 commit 且 auth_configured=true
#       ④ 執行前印出目標與 commit 並等確認
set -euo pipefail

cd "$(dirname "$0")/.."

COMMIT=${COMMIT:-$(git rev-parse HEAD)}
ENV_FILE=.local/app.env
RES=.local/resources.json
KEY=${KEY:-$HOME/.ssh/g06-m01-w03-ed25519}
CONFIRM_WORD=DEPLOY
TRIES=6

[[ -f $RES ]] || { echo "STOP: 找不到 $RES" >&2; exit 1; }
[[ -f $ENV_FILE ]] || { echo "STOP: 找不到 $ENV_FILE（先產生權杖）" >&2; exit 1; }
mode=$(stat -c '%a' "$ENV_FILE")
[[ $mode == 600 ]] || { echo "STOP: $ENV_FILE 權限是 $mode，必須是 600" >&2; exit 1; }
[[ -f $KEY ]] || { echo "STOP: 找不到私鑰 $KEY" >&2; exit 1; }

if [[ -n $(git status --porcelain -- app deploy) ]]; then
  echo "STOP: app/ 或 deploy/ 有未 commit 的修改，部署只取 commit 版本" >&2
  git status --porcelain -- app deploy >&2
  exit 1
fi

IID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["w03_instance_id"])' "$RES")
HOST=$(python3 deploy/awsx.py ec2 describe-instances --instance-ids "$IID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
[[ -n $HOST && $HOST != None ]] || { echo "STOP: 查不到公開位址（主機是 stopped？）" >&2; exit 1; }
# 替代通道：本環境無法對外連 22，可用 deploy/ssm-tunnel.sh start 後指定
#   SSH_HOST=127.0.0.1 SSH_PORT=2222 bash deploy/deploy.sh
SSH_HOST=${SSH_HOST:-$HOST}
SSH_PORT=${SSH_PORT:-22}
if [[ $SSH_PORT != 22 || $SSH_HOST != "$HOST" ]]; then
  echo "目標主機 $IID（位址 $HOST，部署前即時查得；經替代通道 $SSH_HOST:$SSH_PORT 連線）"
else
  echo "目標主機 $IID（位址 $HOST，部署前即時查得）"
fi

cat <<PLAN

── 即將部署 ──────────────────────────────────────────
  commit        $COMMIT
  打包          deploy/make-user-data.sh（只含該 commit 的 app/、deploy/nginx.conf）
  在主機上      ① 執行安裝腳本（重啟服務）② 寫入 $ENV_FILE 到 /etc/inspection/app.env（root 600）
                ③ 再重啟一次 inspection，服務才會讀到新的秘密
  驗證          /health 的 version 等於上述 commit 且 auth_configured=true
  不含秘密      user data 不含權杖；權杖只經 SSH 標準輸入傳送，不進命令列、不進日誌
PLAN
read -r -p "確認請輸入 $CONFIRM_WORD：" a
[[ $a == $CONFIRM_WORD ]] || { echo "取消，未部署"; exit 1; }

SSH_OPTS=(-i "$KEY" -p "$SSH_PORT" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 -o BatchMode=yes)
remote() {  # remote <命令>  以 root 在主機上執行；連線失敗時重試（出口 IP 會輪替）
  local cmd=$1 attempt
  for attempt in $(seq 1 "$TRIES"); do
    if ssh "${SSH_OPTS[@]}" "ec2-user@$SSH_HOST" "$cmd"; then return 0; fi
    echo "  第 $attempt 次 SSH 未通，重試…" >&2
    sleep 3
  done
  echo "STOP: SSH 連續 $TRIES 次失敗（見 .local/w03-evidence/access-findings.md）" >&2
  return 1
}

echo "[1/4] 打包 user data"
rm -f .local/w04-user-data.sh
bash deploy/make-user-data.sh "$COMMIT" .local/w04-user-data.sh
echo "[2/4] 在主機上執行安裝腳本"
remote 'sudo bash -s' < .local/w04-user-data.sh
echo "[3/4] 放秘密檔並重啟服務"
remote 'sudo bash -c "umask 077; cat > /etc/inspection/app.env; chown root:root /etc/inspection/app.env; chmod 600 /etc/inspection/app.env"' < "$ENV_FILE"
remote 'sudo systemctl restart inspection && systemctl is-active inspection'

echo "[4/4] 驗證 /health（主機自身檢視為準；外部檢視只作紀錄，因出口會輪替）"
body=$(remote 'curl -s --max-time 8 http://127.0.0.1/health')
line=$(python3 - "$body" "$COMMIT" <<'PY'
import json, sys
try:
    data = json.loads(sys.argv[1])
except json.JSONDecodeError:
    print("unparsable → 不符"); raise SystemExit(0)
ok = data.get("version") == sys.argv[2] and data.get("auth_configured") is True
print(f"version={data.get('version')} auth_configured={data.get('auth_configured')} "
      f"→ {'符合' if ok else '不符'}")
PY
)
echo "  主機自身（經 SSH 打 127.0.0.1）：$line"
[[ $line == *"→ 符合" ]] || { echo "STOP: 主機上檢視的 version 或 auth_configured 不符" >&2; exit 1; }

echo "  外部檢視（最多 10 次，成功即止）"
for attempt in $(seq 1 10); do
  ext=$(curl -s --max-time 8 "http://$HOST/health" || true)
  if [[ -n $ext ]]; then
    summary=$(printf '%s' "$ext" | python3 -c 'import json,sys
d = json.load(sys.stdin)
print("version=%s auth_configured=%s" % (d.get("version"), d.get("auth_configured")))' 2>/dev/null || echo unparsable)
    echo "    第 $attempt 次：$summary"
    break
  fi
  sleep 2
done
exit 0
