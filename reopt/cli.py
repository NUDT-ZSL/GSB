"""命令行入口：load / run / inject / resume / status / verify。

所有业务异常以错误码作为进程退出码；非法输入不写入任何持久化状态。
"""

from __future__ import annotations

import argparse
import json
import sys

from . import datagen, executor
from .errors import BusinessError
from .runner import Runner, parse_injections


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def cmd_load(args) -> dict:
    runner = Runner.load(
        args.state,
        _read_json(args.tables),
        _read_json(args.plan),
        _read_json(args.batches),
        _read_json(args.config),
    )
    runner.store.close()
    return {"status": "loaded", "state": args.state}


def cmd_run(args) -> dict:
    runner = Runner.open(args.state)
    try:
        summary = runner.run(until_seq=args.until)
    finally:
        runner.store.close()
    return {"status": "ok", **summary}


def cmd_inject(args) -> dict:
    injections = parse_injections(_read_json(args.manifest))
    runner = Runner.open(args.state)
    try:
        summary = runner.run(until_seq=args.until, injections=injections)
    finally:
        runner.store.close()
    return {"status": "ok", "injections_applied": len(injections), **summary}


def cmd_resume(args) -> dict:
    runner = Runner.open(args.state)
    try:
        summary = runner.run(until_seq=args.until)
    finally:
        runner.store.close()
    return {"status": "resumed", **summary}


def cmd_status(args) -> dict:
    runner = Runner.open(args.state)
    store = runner.store
    try:
        return {
            "watermark": store.watermark(),
            "logical_clock": store.logical_clock(),
            "form_version": store.form_version(),
            "current_form": store.current_form(),
            "committed_batches": [
                {"seq": b["seq"], "form_version": b["form_version"],
                 "digest": b["digest"], "result_digest": b["result_digest"]}
                for b in store.committed_batches()
            ],
            "triggers": store.triggers(),
            "switches": store.switches(),
            "precision_flags": store.precision_flags(),
            "ledger_entry_count": len(store.ledger_entries()),
        }
    finally:
        store.close()


def cmd_verify(args) -> dict:
    """重放确定性生成与真值计算，校验已提交摘要、形态版本与最终结果。"""
    runner = Runner.open(args.state)
    store = runner.store
    failures: list[str] = []
    try:
        committed = store.committed_batches()
        for batch in committed:
            seq = batch["seq"]
            for name, table in runner.tables.items():
                columns = runner._generate(name, seq)
                order = tuple(c.name for c in table.columns)
                digest = datagen.batch_digest(name, seq, columns, order)
                if digest != batch["table_digests"][name]:
                    failures.append(f"批次 {seq} 表 {name} 摘要与重放不一致")
        if committed:
            last_seq = committed[-1]["seq"]
            base = {}
            for name, table in runner.tables.items():
                batches = [runner._generate(name, b["seq"]) for b in committed]
                base[name] = executor.build_base_relation(table, batches)
            plan = runner.current_plan()
            truth = executor.truth_join(plan, base)
            truth_digest = executor.result_digest(truth)
            if truth_digest != committed[-1]["result_digest"]:
                failures.append(f"批次 {last_seq} 最终结果与真值摘要不一致")
        versions = [sw["to_version"] for sw in store.switches()]
        if versions != sorted(versions) or len(set(versions)) != len(versions):
            failures.append("形态版本序列非严格递增，存在版本回退")
        ok = not failures
        return {"status": "verified" if ok else "failed", "failures": failures,
                "checked_batches": len(committed)}
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reopt", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("load", help="校验并装载样本到状态目录")
    p.add_argument("--state", required=True)
    p.add_argument("--tables", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--batches", required=True)
    p.add_argument("--config", required=True)
    p.set_defaults(func=cmd_load)

    for name, func, helptext in (
        ("run", cmd_run, "推进流式连接管线"),
        ("inject", cmd_inject, "按注入清单运行并映射业务错误码"),
        ("resume", cmd_resume, "强制终止后的恢复重放"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--state", required=True)
        p.add_argument("--until", type=int, default=None, help="只推进到该批次序号")
        if name == "inject":
            p.add_argument("--manifest", required=True, help="注入清单 JSON 路径")
        p.set_defaults(func=func)

    p = sub.add_parser("status", help="输出水位、形态版本与裁决记录")
    p.add_argument("--state", required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("verify", help="重放确定性生成并校验已提交状态")
    p.add_argument("--state", required=True)
    p.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except BusinessError as exc:
        _emit({"status": "error", "code": exc.code, "message": exc.message})
        return exc.code if exc.code < 256 else 1
    _emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
