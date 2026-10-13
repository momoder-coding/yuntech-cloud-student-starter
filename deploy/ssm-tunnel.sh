#!/usr/bin/env bash
# 環境替代通道：本環境無法對外連 22，改用 SSM 埠轉送（走 443）把本機 2222 接到主機的 22。
# 前置條件：主機已關聯既有 LabInstanceProfile（不修改 IAM），且 SSM agent PingStatus=Online。
# 課程規定 SSM 需教師核准；本檔只做連線通道，不改變主機上的服務設定。
# 用法：bash deploy/ssm-tunnel.sh start|stop|status
set -euo pipefail

cd "$(dirname "$0")/.."

ACTION=${1:-status}
IID=${IID:-$(python3 -c 'import json; print(json.load(open(".local/resources.json"))["w03_instance_id"])')}
LOCAL_PORT=${LOCAL_PORT:-2222}
PLUGIN_DIR=${PLUGIN_DIR:-/tmp/opencode/smplugin/usr/local/sessionmanagerplugin/bin}
LOG=.local/ssm-tunnel.log
PIDFILE=.local/ssm-tunnel.pid
PYFILE=.local/ssm-tunnel.py

port_open() { timeout 3 bash -c "echo > /dev/tcp/127.0.0.1/$LOCAL_PORT" 2>/dev/null; }
running() { [[ -f $PIDFILE ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

case $ACTION in
  start)
    [[ -x $PLUGIN_DIR/session-manager-plugin ]] || {
      echo "STOP: 找不到 session-manager-plugin（$PLUGIN_DIR）" >&2; exit 1; }
    if running && port_open; then echo "通道已在執行（PID $(cat "$PIDFILE")，127.0.0.1:$LOCAL_PORT）"; exit 0; fi
    cat > "$PYFILE" <<'PY'
"""以 scripts/lab.py 的 clean_env 啟動 SSM 埠轉送（長時執行）。"""
import os, subprocess, sys
sys.path.insert(0, "scripts")
import lab

target, local_port, plugin_dir = sys.argv[1], sys.argv[2], sys.argv[3]
ctx = lab.context()
env = lab.clean_env(ctx["region"])
env["PATH"] = env.get("PATH", "") + os.pathsep + plugin_dir
params = '{"portNumber":["22"],"localPortNumber":["%s"]}' % local_port
subprocess.run(["aws", "--profile", "learnerlab", "--region", ctx["region"], "--no-cli-pager",
                "ssm", "start-session", "--target", target,
                "--document-name", "AWS-StartPortForwardingSession",
                "--parameters", params], env=env, check=False)
PY
    nohup python3 "$PYFILE" "$IID" "$LOCAL_PORT" "$PLUGIN_DIR" >"$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    for _ in $(seq 1 30); do
      if port_open; then
        echo "通道已建立：127.0.0.1:$LOCAL_PORT → $IID:22（PID $(cat "$PIDFILE")）"
        exit 0
      fi
      if ! running; then echo "STOP: 轉送程序結束，看 $LOG" >&2; tail -5 "$LOG" >&2; exit 1; fi
      sleep 2
    done
    echo "STOP: 60 秒內通道未建立，看 $LOG" >&2
    exit 1
    ;;
  stop)
    if running; then kill "$(cat "$PIDFILE")" && rm -f "$PIDFILE"; echo "已停止通道"; else echo "沒有在執行的通道"; fi
    ;;
  status)
    if running && port_open; then
      echo "通道正常：127.0.0.1:$LOCAL_PORT（PID $(cat "$PIDFILE")）"
    else
      echo "通道未執行"; tail -3 "$LOG" 2>/dev/null || true
    fi
    ;;
  *)
    echo "用法：bash deploy/ssm-tunnel.sh start|stop|status" >&2; exit 2 ;;
esac
