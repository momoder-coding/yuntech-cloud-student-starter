#!/usr/bin/env bash
# W5：在主機所在 VPC 建 2 個私有子網（不同 AZ，明確關聯只有 local 路由的新路由表）、DB 子網群組、
# SG-db（只有 5432，來源＝主機的 SG）與 RDS PostgreSQL（db.t3.micro、20 GiB gp3、不公開、加密、單一 AZ）。
# 密碼寫進 .local/db.env（600），不顯示、不上命令列；每項 ID 寫進 .local/resources.json，重跑時跳過已建立的項目。
set -euo pipefail
exec python3 "$(dirname "$0")/ops.py" db-up "$@"
