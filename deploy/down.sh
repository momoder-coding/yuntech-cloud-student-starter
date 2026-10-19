#!/usr/bin/env bash
# W3：只回收 .local/resources.json 記錄的 ID（先核對標籤）；--stop 只停止主機、不刪除。
set -euo pipefail
exec python3 "$(dirname "$0")/ops.py" down "$@"
