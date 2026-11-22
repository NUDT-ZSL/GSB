"""本地可交互专业应用 (命令行交互界面).

展示: 当前生成阶段 / 块就绪进度 / 首块实际观测耗时 / 块网格选取与
裁决依据 / 快照版本 / 一致性校验结论 / 最近业务错误码.
支持切换块网格并触发演示运行.
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional

from .config import GenerationConfig
from .demo import load_samples, run_demo
from .errors import GenError
from .service import GenerationService
from .snapshot import KINDS, SnapshotStore

BANNER = "genapp - 本地确定性多模态图文条件生成 (仅 NumPy)"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


class InteractiveApp:
    def __init__(self, sample_dir: str, workspace: str) -> None:
        self.sample_dir = sample_dir
        self.workspace = workspace
        os.makedirs(workspace, exist_ok=True)
        self.cfg: GenerationConfig
        self.cfg, self.tokens, self.anchor_ref, self.anchor_strength = load_samples(
            sample_dir
        )
        self.store = SnapshotStore(os.path.join(workspace, "snapshots"))
        self.service = GenerationService(self.cfg, self.store, self.tokens)
        self.grid_override: Optional[str] = None

    # ---- 展示 ----

    def status(self) -> str:
        svc = self.service
        lines = [BANNER, "-" * 60]
        lines.append(f"生成阶段      : {svc.stage}")
        ready = svc.ready_blocks()
        total = svc.plan["blocks"] if svc.plan else "-"
        lines.append(
            f"块就绪进度    : {len(ready['ready'])}/{total} "
            f"(manifest v{ready['version']})"
        )
        if svc.last_result is not None:
            res = svc.last_result
            lines.append(f"首块观测耗时  : {res.first_block_ms:.3f} ms")
            lines.append(f"整图总耗时    : {res.total_ms:.3f} ms")
            lines.append(f"图像摘要      : sha256={_sha(res.image)}")
            lines.append(f"一致性校验    : {res.consistency}")
        if svc.plan is not None:
            plan = svc.plan
            lines.append(f"块网格        : {plan['grid']} (blocks={plan['blocks']})")
            lines.append(f"裁决依据      : {plan['rationale']}")
            lines.append(
                f"估算首块耗时  : {plan['est_first_block_ms']} ms "
                f"/ 预算 {plan['latency_budget_ms']} ms"
            )
            lines.append(
                f"校验档位      : tier {plan['verify_tier']} "
                f"({plan['verify_tier_name']})"
            )
            lines.append(f"降级类别      : {plan['degradation']}")
        versions = {k: self.store.current_version(k) for k in KINDS}
        lines.append(f"快照版本      : {versions}")
        lines.append(f"最近错误码    : {svc.last_error}")
        return "\n".join(lines)

    # ---- 命令 ----

    def run_once(self) -> str:
        try:
            result = self.service.run(
                grid=self.grid_override,
                anchor_ref=self.anchor_ref,
                anchor_strength=self.anchor_strength,
            )
        except GenError as exc:
            return f"生成失败: {exc.to_dict()}"
        anchor = result.anchor_report or {}
        dominant = (anchor.get("dominant_block") or {}).get("block_id")
        return (
            f"生成完成: grid={result.plan['grid']} "
            f"first_block={result.first_block_ms:.3f}ms "
            f"consistency={result.consistency['verdict']} "
            f"image_sha256={_sha(result.image)} "
            f"dominant_dev_block={dominant}"
        )

    def set_grid(self, grid: Optional[str]) -> str:
        if grid is not None and grid not in self.cfg.grid_candidates:
            return f"未知网格 {grid}, 候选: {list(self.cfg.grid_candidates)}"
        self.grid_override = grid
        return f"块网格切换为: {grid or '(自动裁决)'}"

    def preview(self, block_id: int) -> str:
        try:
            data = self.service.preview_block(block_id)
        except KeyError:
            return f"块 {block_id} 未就绪"
        return f"块 {block_id}: {len(data)} 字节, sha256={_sha(data)}"

    def demo(self) -> str:
        report = run_demo(self.sample_dir, os.path.join(self.workspace, "demo"))
        return f"演示结束: ok={report['ok']} image_sha256={report['image_sha256']}"


HELP = """命令:
  status            展示当前状态 (阶段/进度/耗时/裁决/快照/校验/错误码)
  run               触发一次生成 (中心块优先, 图生图锚定)
  grid <RxC|auto>   切换块网格 (如 grid 2x2, grid auto)
  preview <id>      查询指定块预览字节摘要
  ready             列出已就绪块清单
  demo              运行可重复演示 (切网格重跑并逐像素比对)
  help              显示本帮助
  quit              退出
"""


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sample_dir = os.path.join(root, "samples")
    workspace = os.path.join(root, "workspace")
    app = InteractiveApp(sample_dir, workspace)
    print(BANNER)
    print(HELP)
    while True:
        try:
            line = input("genapp> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("quit", "exit"):
            break
        elif cmd == "status":
            print(app.status())
        elif cmd == "run":
            print(app.run_once())
        elif cmd == "grid":
            grid = args[0] if args else "auto"
            print(app.set_grid(None if grid == "auto" else grid))
        elif cmd == "preview":
            print(app.preview(int(args[0])) if args else "用法: preview <id>")
        elif cmd == "ready":
            print(app.service.ready_blocks())
        elif cmd == "demo":
            print(app.demo())
        elif cmd == "help":
            print(HELP)
        else:
            print(f"未知命令: {cmd}")


if __name__ == "__main__":
    main()
