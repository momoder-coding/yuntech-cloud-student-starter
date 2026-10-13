#!/usr/bin/env python3
"""以 scripts/lab.py 的 clean_env/run_aws 執行一次 AWS CLI 呼叫（課程規則）。

用法：  python3 deploy/awsx.py ec2 describe-vpcs --query ... --output text
輸出：  AWS CLI 的原始輸出到 stdout；失敗時只印錯誤代碼到 stderr，離開碼非 0。
特性：  每次呼叫前先 verify()，確認帳號／區域與 Learner Lab 一致；
        環境變數中的 AWS_* 一律由 lab.clean_env() 清除，不會被導到別的帳號。
"""
import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import lab  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print("用法: python3 deploy/awsx.py <aws 子命令與參數>", file=sys.stderr)
        return 2
    # verify() 的「Identity OK」訊息一律走 stderr，避免污染 stdout 的取值（$(...)）
    with contextlib.redirect_stdout(sys.stderr):
        ctx = lab.verify()
    out = lab.run_aws(sys.argv[1:], ctx["region"], raw=True)
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except lab.LabError as exc:
        print("STOP: " + str(exc), file=sys.stderr)
        sys.exit(1)
