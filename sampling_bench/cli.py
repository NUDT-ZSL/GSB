"""本地可交互诊断工作台命令行入口。

用法：

* ``python -m sampling_bench.cli generate --data-dir DIR --num-requests N``
* ``python -m sampling_bench.cli demo --base-dir DIR``
* ``python -m sampling_bench.cli repl --data-dir DIR --work-dir DIR``

REPL 提供请求列表、采样参数、逐 step 截断、预算账本、调度决策、恢复报告、
最近错误码与自检的浏览，以及预算超限与强制终止恢复演示。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .dataset import dataset_summary, generate_dataset, load_logits, load_requests, load_vocab
from .demo import run_demo
from .engine import BenchEngine, InterruptedRun, RunConfig
from .sampler import sample_request
from .selfcheck import run_selfcheck


def _print_json(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def cmd_generate(args: argparse.Namespace) -> int:
    paths = generate_dataset(
        args.data_dir,
        num_requests=args.num_requests,
        vocab_size=args.vocab_size,
        seed=args.seed,
        max_length=args.max_length,
    )
    _print_json(paths)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    paths = run_demo(args.base_dir, num_requests=args.num_requests, seed=args.seed)
    report = json.loads(Path(paths["report"]).read_text(encoding="utf-8"))
    _print_json(report)
    return 0


class ReplSession:
    def __init__(self, data_dir: str, work_dir: str, config: RunConfig):
        self.data_dir = data_dir
        self.work_dir = work_dir
        self.config = config
        self.engine = BenchEngine(data_dir, work_dir, config)
        self.artifacts = None

    def render_menu(self) -> str:
        return (
            "\n".join(
                [
                    "诊断工作台命令：",
                    "  1) requests       请求列表",
                    "  2) params <rid>   逐请求采样参数",
                    "  3) steps <rid>    逐 step 候选截断与抽取",
                    "  4) ledger         预算账本余量与事件",
                    "  5) decisions      调度决策依据与准入状态",
                    "  6) recovery       恢复报告",
                    "  7) errors         最近业务错误码",
                    "  8) selfcheck      跨模块一致性自检",
                    "  9) run            执行一次完整批量解码",
                    " 10) demo-budget    触发预算超限演示",
                    " 11) demo-crash     触发强制终止恢复演示",
                    " 12) snapshots      查看快照版本与校验和",
                    "  q) 退出",
                ]
            )
        )

    def execute(self, line: str) -> str:
        parts = line.strip().split()
        if not parts:
            return ""
        command, *args = parts
        handler = {
            "requests": self._requests,
            "params": self._params,
            "steps": self._steps,
            "ledger": self._ledger,
            "decisions": self._decisions,
            "recovery": self._recovery,
            "errors": self._errors,
            "selfcheck": self._selfcheck,
            "run": self._run,
            "demo-budget": self._demo_budget,
            "demo-crash": self._demo_crash,
            "snapshots": self._snapshots,
        }.get(command)
        if handler is None:
            return f"未知命令: {command}"
        return handler(args)

    def _ensure_run(self) -> str | None:
        if self.artifacts is None:
            return "尚未执行 run，请先运行 `run`。"
        return None

    def _requests(self, _args) -> str:
        requests = load_requests(self.data_dir)
        return json.dumps(
            [
                {
                    "request_id": req.request_id,
                    "priority": req.priority,
                    "target_length": req.target_length,
                    "prompt_len": len(req.prompt_tokens),
                    "seed": req.seed,
                }
                for req in requests
            ],
            ensure_ascii=False,
            indent=2,
        )

    def _params(self, args) -> str:
        if not args:
            return "用法: params <request_id>"
        request = self._find_request(args[0])
        if request is None:
            return f"未找到请求 {args[0]}"
        return json.dumps(
            {
                "request_id": request.request_id,
                "sampling_params": request.sampling_params.to_dict(),
                "seed": request.seed,
                "target_length": request.target_length,
            },
            ensure_ascii=False,
            indent=2,
        )

    def _steps(self, args) -> str:
        if not args:
            return "用法: steps <request_id>"
        request = self._find_request(args[0])
        if request is None:
            return f"未找到请求 {args[0]}"
        vocab = load_vocab(self.data_dir)
        logits = load_logits(self.data_dir, request.request_id)
        result = sample_request(request, vocab, logits)
        return json.dumps(
            [step.to_dict() for step in result.steps],
            ensure_ascii=False,
            indent=2,
        )

    def _ledger(self, _args) -> str:
        if self._ensure_run():
            return self._ensure_run()
        return json.dumps(self.artifacts.ledger.to_dict(), ensure_ascii=False, indent=2)

    def _decisions(self, _args) -> str:
        if self._ensure_run():
            return self._ensure_run()
        return json.dumps(self.artifacts.plan.to_dict(), ensure_ascii=False, indent=2)

    def _recovery(self, _args) -> str:
        if self._ensure_run():
            return self._ensure_run()
        return json.dumps(self.artifacts.recovery.to_dict(), ensure_ascii=False, indent=2)

    def _errors(self, _args) -> str:
        return json.dumps(self.engine.recent_errors, ensure_ascii=False, indent=2)

    def _selfcheck(self, _args) -> str:
        message = self._ensure_run()
        if message:
            return message
        report = self._build_selfcheck()
        return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)

    def _build_selfcheck(self):
        valid_requests = self.engine.validate().valid_requests
        effective = {
            decision.request_id: decision.target_length
            for decision in self.artifacts.plan.selected.decisions
            if decision.admitted
        }
        return run_selfcheck(
            valid_requests=valid_requests,
            admitted_ids=self.artifacts.plan.selected.admitted_ids,
            rejected_ids=[
                decision.request_id
                for decision in self.artifacts.plan.selected.rejected
            ],
            results=self.artifacts.results,
            ledger=self.artifacts.ledger,
            effective_lengths=effective,
        )

    def _run(self, _args) -> str:
        self.artifacts = self.engine.run(fresh=True)
        return (
            f"运行完成：准入 {len(self.artifacts.plan.selected.admitted_ids)} 个，"
            f"采样 {len(self.artifacts.results)} 个，"
            f"快照版本 {self.artifacts.snapshot_versions}。"
        )

    def _demo_budget(self, _args) -> str:
        tight = RunConfig(
            max_workers=2,
            batch_size=2,
            step_budget=22,
            cache_budget=18,
            slot_budget=2,
            wave_budget=2,
        )
        engine = BenchEngine(self.data_dir, str(Path(self.work_dir) / "budget"), tight)
        artifacts = engine.run(fresh=True)
        return json.dumps(
            {
                "admitted": artifacts.plan.selected.admitted_ids,
                "rejected": [d.to_dict() for d in artifacts.plan.selected.rejected],
                "observed_codes_all_profiles": sorted(
                    {
                        d.code
                        for ev in artifacts.plan.evaluations
                        for d in ev.rejected
                    }
                ),
                "balances": artifacts.ledger.balances(),
            },
            ensure_ascii=False,
            indent=2,
        )

    def _demo_crash(self, _args) -> str:
        crash_dir = Path(self.work_dir) / "crash"
        engine = BenchEngine(self.data_dir, str(crash_dir), self.config)
        try:
            engine.run(fresh=True, crash_after_committed=4)
        except InterruptedRun:
            pass
        resumed = BenchEngine(self.data_dir, str(crash_dir), self.config)
        self.artifacts = resumed.run(fresh=False)
        return json.dumps(
            {
                "recovery": self.artifacts.recovery.to_dict(),
                "sampled": [r.request_id for r in self.artifacts.results],
            },
            ensure_ascii=False,
            indent=2,
        )

    def _snapshots(self, _args) -> str:
        payload = {}
        for name in ("samples", "ledger", "decisions"):
            try:
                snapshot = self.engine.snapshots.read_current(name)
                payload[name] = {"version": snapshot.version, "checksum": snapshot.checksum}
            except Exception as exc:  # noqa: BLE001 - 工作台展示层兜底
                payload[name] = {"error": str(exc)}
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _find_request(self, request_id: str):
        for request in load_requests(self.data_dir):
            if request.request_id == request_id:
                return request
        return None


def cmd_repl(args: argparse.Namespace) -> int:
    config = RunConfig(
        max_workers=args.max_workers,
        batch_size=args.batch_size,
        step_budget=args.step_budget,
        cache_budget=args.cache_budget,
        slot_budget=args.slot_budget,
        wave_budget=args.wave_budget,
    )
    session = ReplSession(args.data_dir, args.work_dir, config)
    print(json.dumps(dataset_summary(args.data_dir), ensure_ascii=False, indent=2))
    print(session.render_menu())
    while True:
        try:
            line = input("bench> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line.strip() in {"q", "quit", "exit"}:
            return 0
        if line.strip():
            print(session.execute(line))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LLM 采样诊断工作台")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="生成确定性数据集")
    gen.add_argument("--data-dir", required=True)
    gen.add_argument("--num-requests", type=int, default=12)
    gen.add_argument("--vocab-size", type=int, default=32)
    gen.add_argument("--seed", type=int, default=20261124)
    gen.add_argument("--max-length", type=int, default=8)
    gen.set_defaults(func=cmd_generate)

    demo = sub.add_parser("demo", help="运行预算超限+强制终止恢复演示")
    demo.add_argument("--base-dir", required=True)
    demo.add_argument("--num-requests", type=int, default=12)
    demo.add_argument("--seed", type=int, default=20261124)
    demo.set_defaults(func=cmd_demo)

    repl = sub.add_parser("repl", help="启动交互式诊断工作台")
    repl.add_argument("--data-dir", required=True)
    repl.add_argument("--work-dir", required=True)
    repl.add_argument("--max-workers", type=int, default=2)
    repl.add_argument("--batch-size", type=int, default=2)
    repl.add_argument("--step-budget", type=int, default=10_000)
    repl.add_argument("--cache-budget", type=int, default=10_000)
    repl.add_argument("--slot-budget", type=int, default=4)
    repl.add_argument("--wave-budget", type=int, default=1_000)
    repl.set_defaults(func=cmd_repl)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
