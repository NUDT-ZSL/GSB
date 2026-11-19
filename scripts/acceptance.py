"""验收脚本：确定性样本全量验证 + 固定种子重跑哈希比对。

用法: python scripts/acceptance.py
流程:
  1. 生成确定性样本（JSONL + npz 缓存）
  2. 清单校验
  3. 全量运行四阶段流水线（含四类注入 case）
  4. 跨变体裁决（classic vs interp/ntk/blockwise/等价声明）
  5. 独立验证器复核
  6. 固定种子重跑，比对两次完整输出哈希
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rope_workbench import samples, validate
from rope_workbench.report import result_hash
from rope_workbench.workbench import Workbench


def main():
    cases, manifest = samples.generate_all("data", "cache")
    print(f"[1] 样本清单 {manifest}: {len(cases)} cases")

    errors = validate.validate_manifest(cases)
    print(f"[2] 清单校验错误: {len(errors)}")

    wb = Workbench()
    wb.load()
    report = wb.run_all()

    print("[3] 各 case 状态:")
    for c in report["cases"]:
        print(f"    {c['case_id']:24s} status={c['status']:9s} "
              f"max_block_dev={c['max_block_dev']:.3e} "
              f"inj={c['injections']} deg={c['degradations']} "
              f"hash={c['hash'][:12]}")

    print("[4] 跨变体裁决:")
    for vr in report["variant_reports"]:
        n_err = len(vr["errors"])
        print(f"    {vr['variant_a']} -> {vr['variant_b']}: "
              f"max_angle_dev={vr['angle_dev_spectrum']['max_abs']:.3e} "
              f"errors={n_err}")
        for e in vr["errors"]:
            print("      ", e["code"], e["message"])

    print(f"[5] 独立验证器问题数: {len(report['verify_issues'])}")
    for i in report["verify_issues"]:
        print("      ", i["code"], i["case_id"], i["message"])

    print("[6] 固定种子重跑哈希比对:")
    ok = True
    for c in wb.cases:
        r1 = wb.run(c["case_id"])
        r2 = wb.run(c["case_id"])
        h1, h2 = result_hash(r1), result_hash(r2)
        match = h1 == h2
        ok = ok and match
        print(f"    {c['case_id']:24s} {'MATCH' if match else 'MISMATCH'} "
              f"{h1[:16]}")
    print(f"run_hash={report['run_hash']}")
    print("ACCEPTANCE:", "PASS" if ok and not report["verify_issues"] else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
