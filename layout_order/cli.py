"""命令行入口：生成确定性样本并运行完整基线与规模变化追踪。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from .config import AppConfig
from .errors import ALL_CODES, LayoutOrderError
from .generator import (
    generate_baseline_dataset,
    generate_scale_sweep_dataset,
)
from .geometry import load_jsonl
from .pipeline import cross_check_page, process_page, summarize
from .snapshots import CacheLedger, SnapshotStore, canonical_json


def _load_config(path: str | None) -> AppConfig:
    if path is None:
        config = AppConfig()
        config.validate()
        return config
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return AppConfig.from_dict(data)


def _run_dataset(
    dataset_path: str,
    config: AppConfig,
    snapshot_store: SnapshotStore,
    cache_ledger: CacheLedger,
) -> dict:
    pages = load_jsonl(dataset_path)
    page_reports: list[dict] = []
    checks: dict[str, dict] = {}
    dataset_code = "OK"
    for page in pages:
        report = process_page(page, config, snapshot_store, cache_ledger)
        checks[page.page_id] = cross_check_page(page, report, config)
        if report["code"] != "OK":
            dataset_code = report["code"]
        page_reports.append(report)
    return {
        "dataset": os.path.basename(dataset_path),
        "code": dataset_code,
        "summary": summarize(page_reports),
        "pages": page_reports,
        "cross_checks": checks,
        "cache_ledger": cache_ledger.summary(),
    }


def build_report(
    baseline_path: str,
    sweep_path: str,
    config: AppConfig,
    snapshot_dir: str,
    cache_dir: str,
) -> dict:
    # 缓存台账记录“本次运行”的复用/重算；每次实验先重置缓存目录，保证报告在
    # 相同输入与配置下逐字节可复现。快照目录保留跨运行的版本化演进语义。
    if os.path.isdir(cache_dir):
        shutil.rmtree(cache_dir)
    snapshot_store = SnapshotStore(snapshot_dir)
    baseline_ledger = CacheLedger(os.path.join(cache_dir, "baseline"))
    sweep_ledger = CacheLedger(os.path.join(cache_dir, "sweep"))

    baseline = _run_dataset(baseline_path, config, snapshot_store, baseline_ledger)
    sweep = _run_dataset(sweep_path, config, snapshot_store, sweep_ledger)

    overall_code = "OK"
    for section in (baseline, sweep):
        if section["code"] != "OK":
            overall_code = section["code"]

    return {
        "report_schema_version": 1,
        "code": overall_code,
        "config": config.to_dict(),
        "baseline": baseline,
        "scale_sweep": sweep,
    }


def write_report(report: dict, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(report))
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="layout-order",
        description="文档图像版面阅读顺序分析：确定性样本、求解取舍与可核对报告",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    gen_parser = subparsers.add_parser("generate", help="生成确定性样本 JSONL")
    gen_parser.add_argument("--baseline-out", default="data/baseline.jsonl")
    gen_parser.add_argument("--sweep-out", default="data/scale_sweep.jsonl")

    run_parser = subparsers.add_parser("run", help="运行完整实验并输出 JSON 报告")
    run_parser.add_argument("--baseline", default="data/baseline.jsonl")
    run_parser.add_argument("--sweep", default="data/scale_sweep.jsonl")
    run_parser.add_argument("--config", default=None)
    run_parser.add_argument("--snapshot-dir", default="snapshots")
    run_parser.add_argument("--cache-dir", default="cache")
    run_parser.add_argument("--report", default="reports/experiment_report.json")

    all_parser = subparsers.add_parser(
        "all", help="生成样本并运行完整实验（默认入口）"
    )
    all_parser.add_argument("--baseline", default="data/baseline.jsonl")
    all_parser.add_argument("--sweep", default="data/scale_sweep.jsonl")
    all_parser.add_argument("--config", default=None)
    all_parser.add_argument("--snapshot-dir", default="snapshots")
    all_parser.add_argument("--cache-dir", default="cache")
    all_parser.add_argument("--report", default="reports/experiment_report.json")

    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            generate_baseline_dataset(args.baseline_out)
            generate_scale_sweep_dataset(args.sweep_out)
            print(
                canonical_json(
                    {
                        "code": "OK",
                        "baseline": args.baseline_out,
                        "scale_sweep": args.sweep_out,
                    }
                )
            )
            return 0

        config = _load_config(getattr(args, "config", None))
        if args.command == "all":
            generate_baseline_dataset(args.baseline)
            generate_scale_sweep_dataset(args.sweep)
        report = build_report(
            args.baseline,
            args.sweep,
            config,
            args.snapshot_dir,
            args.cache_dir,
        )
        write_report(report, args.report)
        print(
            canonical_json(
                {
                    "code": report["code"],
                    "report": args.report,
                    "baseline_summary": report["baseline"]["summary"],
                    "sweep_summary": report["scale_sweep"]["summary"],
                }
            )
        )
        return 0 if report["code"] == "OK" else 2
    except LayoutOrderError as exc:
        if exc.code not in ALL_CODES:
            raise
        error_payload = {"code": exc.code, "message": exc.message, "details": exc.details}
        print(canonical_json(error_payload), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
