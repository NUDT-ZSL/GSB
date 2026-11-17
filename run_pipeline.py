"""管线运行入口：本地启动校验服务并执行请求清单，输出 JSON 报告。

用法：
    python3 run_pipeline.py --samples samples/default --workdir out
        [--workers 4] [--queue-limit N] [--hard-timeout-ms N] [--no-resume]
    python3 run_pipeline.py --generate-samples samples/default
"""

from __future__ import annotations

import argparse
import json
import sys

from pipeline import samplegen
from pipeline.engine import EngineConfig, run_pipeline
from pipeline.service import LocalPolicyService


def _load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    parser = argparse.ArgumentParser(description="安全策略交互一致性验证管线")
    parser.add_argument("--samples", default="samples/default", help="样本目录")
    parser.add_argument("--workdir", default="out", help="工作目录（检查点与报告）")
    parser.add_argument("--workers", type=int, default=4, help="并发度")
    parser.add_argument("--queue-limit", type=int, default=None,
                        help="待处理请求队列上限")
    parser.add_argument("--hard-timeout-ms", type=int, default=None,
                        help="单请求真实耗时硬上限（毫秒）")
    parser.add_argument("--no-resume", action="store_true",
                        help="不从检查点恢复")
    parser.add_argument("--generate-samples", metavar="DIR", default=None,
                        help="只生成确定性样本后退出")
    args = parser.parse_args(argv)

    if args.generate_samples:
        samplegen.generate_samples(args.generate_samples)
        print("样本已生成: %s" % args.generate_samples)
        return 0

    decl = _load("%s/policy.json" % args.samples)
    manifest = _load("%s/requests.json" % args.samples)
    svc_cfg = _load("%s/service_config.json" % args.samples)

    service = LocalPolicyService(
        delay_tiers_ms=svc_cfg.get("delay_tiers_ms"),
        order_mode=svc_cfg.get("order_mode", "normal"),
        order_step_ms=svc_cfg.get("order_step_ms", 5),
        serve_versions=svc_cfg.get("serve_versions"),
        corrupt=svc_cfg.get("corrupt"),
        summary_version=svc_cfg.get("summary_version", "svc-1"),
        seed=svc_cfg.get("seed", 27),
    )
    service.load_declaration(decl)
    service.load_declaration(_load("%s/policy_v0.json" % args.samples))

    config = EngineConfig(
        workers=args.workers,
        hard_timeout_ms=args.hard_timeout_ms,
        queue_limit=args.queue_limit,
        resume=not args.no_resume,
    )
    report = run_pipeline(decl, manifest, service, config, args.workdir)

    counts = report["counts"]
    print("报告已写入: %s/report.json" % args.workdir)
    print("总计 %(total)d 条：OK %(ok)d，超时 %(timeout)d，"
          "摘要不符 %(mismatch)d，陈旧拒绝 %(stale)d，"
          "队列拒绝 %(queue_rejected)d" % counts)
    for rec in report["requests"]:
        print("  %s [%s] status=%s timed_out=%s latency=%.1fms deadline=%dms"
              % (rec["request_id"], rec["group_id"], rec["status"],
                 rec["timed_out"], rec["latency_ms"], rec["deadline_ms"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
