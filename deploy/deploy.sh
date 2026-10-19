#!/usr/bin/env bash
# W4 起：把「已 commit」的版本部署到同一台主機（經 SSH 執行打包器產生的安裝腳本），
# 再把 .local/app.env（W5 起加 .local/db.env）經 SSH 標準輸入放到 /etc/inspection/app.env（root、600），重啟服務。
# 執行前印出目標主機與 commit 並等確認；最後核對 /health 的 version 與 auth_configured。
set -euo pipefail
exec python3 "$(dirname "$0")/ops.py" deploy "$@"
