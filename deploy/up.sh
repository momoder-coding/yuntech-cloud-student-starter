#!/usr/bin/env bash
# W3：建立 1 個 SG、1 個匯入的 key pair、1 台主機，部署指定 commit（預設 HEAD）。
# 參數：[--commit SHA] [--group g06 --owner m01 --source-cidr x.x.x.x/32]，或寫在 .local/config.json。
set -euo pipefail
exec python3 "$(dirname "$0")/ops.py" up "$@"
