from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
from typing import Any

from .engine import LedgerEngine, load_documents
from .errors import BusinessError, error
from .models import parse_bundles


def _read_json(path: str | Path, label: str) -> Any:
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise error(
            "INPUT_FILE_UNREADABLE",
            f"无法读取 {label} 文件: {target}",
            details={"path": str(target), "reason": str(exc)},
        ) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise error(
            "INVALID_JSON",
            f"{label} 文件不是有效 JSON",
            details={
                "path": str(target),
                "line": exc.lineno,
                "column": exc.colno,
                "reason": exc.msg,
            },
        ) from exc


def _emit(value: Any) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )


def _engine(args: argparse.Namespace) -> LedgerEngine:
    return LedgerEngine(args.db)


def _load(args: argparse.Namespace) -> int:
    tenants_doc = _read_json(args.tenants, "tenants")
    services_doc = _read_json(args.services, "services")
    applications_doc = _read_json(args.applications, "applications")
    parse_bundles(tenants_doc, services_doc, applications_doc)
    engine = _engine(args)
    result = load_documents(
        engine,
        tenants_doc,
        services_doc,
        applications_doc,
        reset=args.reset,
        at_ms=args.at_ms,
    )
    _emit(result)
    return 0


def _plan_round(
    engine: LedgerEngine,
    *,
    round_id: str,
    round_start_ms: int,
    round_budget_ms: int,
) -> tuple[list[str], list[tuple[str, str]]]:
    staged = engine.list_staged_applications()
    status = engine.status()
    service_map = {s["name"]: s for s in status["services"]}
    ordered = sorted(
        staged, key=lambda row: (int(row["requested_at_ms"]), int(row["sequence"]))
    )
    selected: list[str] = []
    abandoned: list[tuple[str, str]] = []
    service_counts: dict[str, int] = {}
    for app in ordered:
        app_id = app["application_id"]
        requested_at = int(app["requested_at_ms"])
        if (
            requested_at < round_start_ms
            or requested_at - round_start_ms > round_budget_ms
        ):
            abandoned.append((app_id, "ROUND_BUDGET_EXHAUSTED"))
            continue
        service = app["service"]
        if service not in service_map:
            abandoned.append((app_id, "SERVICE_UNKNOWN"))
            continue
        limit = int(service_map[service]["requests_per_round"])
        used = service_counts.get(service, 0)
        if used >= limit:
            abandoned.append((app_id, "SERVICE_ROUND_BATCH_LIMIT"))
            continue
        service_counts[service] = used + 1
        selected.append(app_id)
    return selected, abandoned


def _submit(args: argparse.Namespace) -> int:
    engine = _engine(args)
    if args.all and args.application:
        raise error(
            "CLI_ARGUMENT_CONFLICT",
            "--all 与 --application 不能同时使用",
        )
    if not args.all and not args.application:
        raise error(
            "CLI_MISSING_ARGUMENT",
            "必须指定 --all 或至少一个 --application",
        )
    if args.workers < 1:
        raise error("CLI_INVALID_ARGUMENT", "--workers 必须大于 0")

    selected = list(args.application or [])
    abandoned: list[tuple[str, str]] = []
    round_id = args.round_id
    round_start = args.round_start_ms
    round_budget = args.round_budget_ms
    if args.all:
        staged = engine.list_staged_applications()
        if round_start is None:
            round_start = (
                min(int(a["requested_at_ms"]) for a in staged) if staged else 0
            )
        if round_budget is None:
            round_budget = 2**62
        if round_id is None:
            round_id = f"round:{round_start}:{round_budget}"
        selected, abandoned = _plan_round(
            engine,
            round_id=round_id,
            round_start_ms=round_start,
            round_budget_ms=round_budget,
        )

    abandon_results: list[dict[str, Any]] = []
    for app_id, reason_code in abandoned:
        abandon_results.append(
            engine.abandon_application(
                app_id,
                reason_code=reason_code,
                at_ms=round_start,
                round_id=round_id,
                round_start_ms=round_start,
                round_budget_ms=round_budget,
            )
        )

    def run_one(app_id: str) -> dict[str, Any]:
        return engine.submit(
            app_id,
            now_ms=args.now_ms,
            round_id=round_id,
            round_start_ms=round_start,
            round_budget_ms=round_budget,
        )

    if args.workers == 1:
        results = [run_one(app_id) for app_id in selected]
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(run_one, app_id) for app_id in selected]
            results = [future.result() for future in futures]
    _emit(
        {
            "ok": True,
            "round_id": round_id,
            "round_start_ms": round_start,
            "round_budget_ms": round_budget,
            "workers": args.workers,
            "selected": selected,
            "abandoned": abandon_results,
            "results": results,
        }
    )
    return 0


def _release(args: argparse.Namespace) -> int:
    trigger = args.trigger.upper()
    if trigger not in {"MANUAL", "CANCEL", "EXPIRY"}:
        raise error(
            "CLI_INVALID_ARGUMENT",
            "--trigger 必须为 MANUAL、CANCEL 或 EXPIRY",
        )
    result = _engine(args).release(
        args.application,
        at_ms=args.now_ms,
        trigger_type=trigger,
    )
    _emit(result)
    return 0


def _resume(args: argparse.Namespace) -> int:
    result = _engine(args).resume(now_ms=args.now_ms)
    _emit(result)
    return 0 if result["ok"] else 1


def _status(args: argparse.Namespace) -> int:
    _emit(_engine(args).status())
    return 0


def _verify(args: argparse.Namespace) -> int:
    result = _engine(args).verify()
    _emit(result)
    return 0 if result["ok"] else 1


def _serve(args: argparse.Namespace) -> int:
    from .web import create_app

    app = create_app(
        db_path=args.db,
        samples_dir=args.samples_dir,
    )
    app.run(
        host="127.0.0.1",
        port=args.port,
        debug=False,
        use_reloader=False,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quota-ledger",
        description="本地多租户资源配额账本与跨服务预留仲裁引擎",
    )
    parser.add_argument(
        "--db",
        default="data/ledger.db",
        help="SQLite 数据库路径（默认 data/ledger.db）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    load = sub.add_parser("load", help="校验并装载确定性地租户/服务/申请样本")
    load.add_argument("--tenants", required=True)
    load.add_argument("--services", required=True)
    load.add_argument("--applications", required=True)
    load.add_argument("--reset", action="store_true", help="清空账本后装载")
    load.add_argument("--at-ms", type=int, default=0)
    load.set_defaults(func=_load)

    submit = sub.add_parser("submit", help="提交单个/全部暂存申请")
    submit.add_argument("--application", action="append", default=[])
    submit.add_argument("--all", action="store_true")
    submit.add_argument("--now-ms", type=int)
    submit.add_argument("--round-id")
    submit.add_argument("--round-start-ms", type=int)
    submit.add_argument("--round-budget-ms", type=int)
    submit.add_argument("--workers", type=int, default=1)
    submit.set_defaults(func=_submit)

    release = sub.add_parser("release", help="撤销或结算一个预留")
    release.add_argument("--application", required=True)
    release.add_argument("--now-ms", type=int)
    release.add_argument("--trigger", default="MANUAL")
    release.set_defaults(func=_release)

    resume = sub.add_parser("resume", help="恢复半提交操作并推进逻辑时钟")
    resume.add_argument("--now-ms", type=int)
    resume.set_defaults(func=_resume)

    status = sub.add_parser("status", help="稳定排序输出全量账本状态")
    status.set_defaults(func=_status)

    verify = sub.add_parser("verify", help="执行守恒、幂等、借调和平账检查")
    verify.set_defaults(func=_verify)

    serve = sub.add_parser("serve", help="在 127.0.0.1 启动 Flask 控制台")
    serve.add_argument("--port", type=int, default=5000)
    serve.add_argument("--samples-dir", default="samples")
    serve.set_defaults(func=_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BusinessError as exc:
        _emit(exc.to_dict())
        return 1
    except KeyboardInterrupt:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "INTERRUPTED",
                    "message": "操作被中断",
                    "details": {},
                },
            }
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
