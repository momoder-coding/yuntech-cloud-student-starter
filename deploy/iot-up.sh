#!/usr/bin/env bash
# W6 規格 A：取得 IoT 端點、建立閘道與 consumer 兩張憑證（私鑰直接寫進 .local/iot/，不顯示）、
# 兩份資源精確的政策（不用 *；Subscribe 用 topicfilter/）、附加政策、下載 Amazon Root CA 1。
# ID 寫進 .local/resources.json；重跑時已有的項目一律跳過，不建第二張憑證、不覆蓋私鑰。
set -euo pipefail
exec python3 "$(dirname "$0")/ops.py" iot-up "$@"
