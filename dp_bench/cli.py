"""依存句法分析工作台命令行入口。

典型用法（Windows PowerShell / cmd 同样适用）::

    python -m dp_bench.cli run --samples data/sentences.txt \
        --model data/model.json --config data/config.json \
        --snapshot-dir snapshots/run1 --report run/report.json
    python -m dp_bench.cli run ... --mode serial
    python -m dp_bench.cli verify --report-a run/serial.json \
        --report-b run/parallel.json
    python -m dp_bench.cli inject --injections data/injections.json \
        --snapshot-dir snapshots/inj --report run/injections.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .budget import BudgetLedger
from .engine import ParseEngine, ShutdownController
from .errors import BenchError, E_CONFIG_MISMATCH
from .loader import load_config, load_model, load_samples, validate_config
from .model import sha256_hex
from .snapshot import SnapshotStore

EXIT_OK = 0
EXIT_VALIDATION = 2
EXIT_MISMATCH = 3


def _write_report(path: str | Path, report: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.flush()
    tmp.replace(path)


def _restore_completed(engine: ParseEngine, store: SnapshotStore,
                       completed_ids: list[str]) -> None:
    for sid in completed_ids:
        payload = store.load_result(sid)
        if payload is not None:
            engine.completed[sid] = payload


def cmd_run(args) -> int:
    config = load_config(args.config)
    model = load_model(args.model)
    sentences = load_samples(args.samples)

    config_hash = sha256_hex(config)
    model_hash = model.content_hash()
    store = SnapshotStore(args.snapshot_dir, config_hash, model_hash)

    try:
        snap = store.resume_state()
    except BenchError as exc:
        print(f"{E_CONFIG_MISMATCH}: {exc.message}", file=sys.stderr)
        return EXIT_MISMATCH

    shutdown = ShutdownController()
    if not args.no_signal:
        shutdown.install()
    engine = ParseEngine(model, config, sentences,
                         snapshot_store=store, shutdown=shutdown)

    resume_info = {"resumed_from": None, "snapshot_completed": 0}
    if snap is not None and not args.fresh:
        done_ids = [sid for sid in engine.sentence_ids
                    if sid in snap["completed_sentence_ids"]]
        _restore_completed(engine, store, done_ids)
        pre_consumed = {sid: engine.completed[sid]["steps_used"]
                        for sid in done_ids}
        engine.ledger = BudgetLedger(
            engine.sentence_ids, config["global_step_budget"],
            config["per_sentence_quota"], pre_consumed=pre_consumed)
        engine.snapshot_version = snap["version"]
        resume_info = {"resumed_from": snap["stage"],
                       "snapshot_completed": len(done_ids)}

    if args.mode == "serial":
        config = dict(config)
        config["max_workers"] = 1
        config["batch_size"] = 1
        engine.config = config

    report = engine.run()
    report["resume"] = resume_info
    report["run_mode"] = args.mode
    _write_report(args.report, report)

    if shutdown.requested:
        print(f"收到可处理退出信号 {shutdown.signal_name}，"
              "在途句已落盘，退出码 0")
    return EXIT_OK


def cmd_verify(args) -> int:
    with open(args.report_a, encoding="utf-8") as fh:
        report_a = json.load(fh)
    with open(args.report_b, encoding="utf-8") as fh:
        report_b = json.load(fh)
    ignore = {"observability", "resume", "run_mode", "shutdown_signal",
              "config_hash"}
    # 串行模式仅覆盖调度并发参数，确定性结果与并行一致；比较前规范化
    for report in (report_a, report_b):
        if isinstance(report.get("config"), dict):
            report["config"] = {
                **report["config"], "max_workers": "<mode>",
                "batch_size": "<mode>"}
    diffs = []
    for key in sorted(set(report_a) | set(report_b)):
        if key in ignore:
            continue
        if report_a.get(key) != report_b.get(key):
            diffs.append(key)
    summary = {
        "field_identical": not diffs,
        "ignored_nondeterministic_fields": sorted(ignore),
        "different_fields": diffs,
        "digest_a": report_a.get("consistency",
                                 {}).get("determinism_digest"),
        "digest_b": report_b.get("consistency",
                                 {}).get("determinism_digest"),
    }
    if args.out:
        _write_report(args.out, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return EXIT_OK if not diffs else EXIT_VALIDATION


def _snapshot_signature(directory: Path):
    if not directory.exists():
        return None
    return sorted((p.name, p.stat().st_size)
                  for p in directory.rglob("*") if p.is_file())


def _run_injection(item: dict) -> None:
    kind = item["kind"]
    payload = item["payload"]
    if kind == "config":
        validate_config(payload)
    elif kind == "model":
        from .model import TransitionModel
        TransitionModel(payload)
    elif kind == "sample":
        from .loader import parse_sample_line
        for line_no, line in enumerate(payload["lines"], start=1):
            parse_sample_line(line, line_no)
    else:
        raise BenchError(f"未知注入类型 {kind}", phase="inject")


def cmd_inject(args) -> int:
    with open(args.injections, encoding="utf-8") as fh:
        spec = json.load(fh)
    checks = []
    snapshot_root = Path(args.snapshot_dir)
    for item in spec["injections"]:
        injection_id = item["injection_id"]
        expected = item["expected_code"]
        snapshot_dir = snapshot_root / injection_id
        before = _snapshot_signature(snapshot_dir)
        actual = None
        try:
            _run_injection(item)
        except BenchError as exc:
            actual = exc.code
        untouched = before == _snapshot_signature(snapshot_dir)
        checks.append({
            "injection_id": injection_id,
            "kind": item["kind"],
            "description": item["description"],
            "expected_code": expected,
            "actual_code": actual,
            "hit": actual == expected,
            "snapshot_untouched": untouched,
        })
    summary = {
        "injection_summary": {
            "hit": sum(1 for c in checks if c["hit"]),
            "total": len(checks),
        },
        "injection_checks": checks,
    }
    _write_report(args.report, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return EXIT_OK if all(c["hit"] for c in checks) else EXIT_VALIDATION


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dp_bench", description="中文依存句法分析并行预算工作台")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="执行解析（支持串行/并行与恢复）")
    run.add_argument("--samples", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--snapshot-dir", required=True)
    run.add_argument("--report", required=True)
    run.add_argument("--mode", choices=["parallel", "serial"],
                     default="parallel")
    run.add_argument("--fresh", action="store_true",
                     help="忽略已有快照强制全新运行")
    run.add_argument("--no-signal", action="store_true",
                     help="不安装信号处理器（测试用）")
    run.set_defaults(func=cmd_run)

    verify = sub.add_parser("verify", help="比较两份报告的确定性字段")
    verify.add_argument("--report-a", required=True)
    verify.add_argument("--report-b", required=True)
    verify.add_argument("--out")
    verify.set_defaults(func=cmd_verify)

    inject = sub.add_parser("inject", help="执行异常注入校验")
    inject.add_argument("--injections", required=True)
    inject.add_argument("--snapshot-dir", required=True)
    inject.add_argument("--report", required=True)
    inject.set_defaults(func=cmd_inject)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BenchError as exc:
        print(json.dumps(exc.failure_record(), ensure_ascii=False),
              file=sys.stderr)
        return EXIT_VALIDATION


if __name__ == "__main__":
    sys.exit(main())
