"""Interactive command line session and replay entry point.

Runs on Windows terminals without any network access. The same command handlers
back the interactive prompt and the deterministic replay engine, so replaying
an operation sequence from a clean workspace reproduces identical pixels and
diagnostic report content.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import (DEFAULT_CONFIG_DIR, load_quant_rules,
                     load_render_config, load_version_map)
from .errors import IEditorError
from .replay import ReplayEngine
from .snapshot import read_progress

HELP_TEXT = """\
可用命令:
  load <工程路径>                 加载 v1(.ied) 或 v2(.json) 工程
  switch <1|2> <输出路径>         切换工程版本并写盘、刷新快照
  mask <x> <y> <w> <h> <0|255>    编辑掩码矩形区域
  weight <R|G|B> <0..1>           调整分通道浮点权重(仅 v2)
  intensity <档位>                调整整型强度档位
  render <输出目录>               执行确定性修复 + 风格迁移渲染
  diagnose <工程A> <工程B> <报告> 跨版本一致性诊断 JSON 报告
  snapshot                        校验当前工程快照
  fingerprint                     打印统一结构指纹
  report                          打印最近一次诊断报告
  failures                        打印失败清单
  replay <序列文件> [resume]      回放编辑序列(resume=从进度断点继续)
  help                            显示本帮助
  exit                            退出
"""


class InteractiveSession:
    def __init__(self, root: Path, config_dir: Path):
        self.root = Path(root).resolve()
        self.config = load_render_config(config_dir / "render_config.json")
        self.version_map = load_version_map(config_dir / "version_map.json")
        self.quant_rules = load_quant_rules(config_dir / "quant_rules.json")
        self.engine = ReplayEngine(
            work_dir=self.root / "workspace_demo" / "workspace_run",
            config=self.config, version_map=self.version_map,
            quant_rules=self.quant_rules, root_dir=self.root)
        self.engine.install_signal_handlers()

    def _resolve(self, raw: str) -> Path:
        work_dir = (self.root / "workspace_demo" / "workspace_run").resolve()
        expanded = raw.replace("${WORK_DIR}", str(work_dir))
        path = Path(expanded)
        if path.is_absolute():
            return path
        candidate = (self.root / path).resolve()
        return candidate

    def _relative(self, path: Path) -> str:
        path = Path(path).resolve()
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def execute_line(self, line: str) -> bool:
        parts = line.strip().split()
        if not parts:
            return True
        cmd, args = parts[0], parts[1:]
        try:
            if cmd == "exit":
                return False
            if cmd == "help":
                print(HELP_TEXT)
            elif cmd == "load":
                op = {"op": "load", "path": self._relative(self._resolve(args[0]))}
                self._run_one(op)
            elif cmd == "switch":
                op = {"op": "switch_version",
                      "target_version": int(args[0]),
                      "out_path": self._relative(self._resolve(args[1]))}
                self._run_one(op)
            elif cmd == "mask":
                x, y, w, h, value = (int(v) for v in args[:5])
                self._run_one({"op": "edit_mask_rect", "x": x, "y": y,
                               "width": w, "height": h, "value": value})
            elif cmd == "weight":
                self._run_one({"op": "set_channel_weight",
                               "channel": args[0],
                               "weight": float(args[1])})
            elif cmd == "intensity":
                self._run_one({"op": "set_intensity",
                               "intensity": int(args[0])})
            elif cmd == "render":
                self._run_one({"op": "render",
                               "output_dir": self._relative(
                                   self._resolve(args[0]))})
            elif cmd == "diagnose":
                self._run_one({"op": "diagnose",
                               "project_a": self._relative(
                                   self._resolve(args[0])),
                               "project_b": self._relative(
                                   self._resolve(args[1])),
                               "report_path": self._relative(
                                   self._resolve(args[2]))})
            elif cmd == "snapshot":
                self._run_one({"op": "verify_snapshot"})
            elif cmd == "fingerprint":
                project = self.engine.session.project
                if project is None:
                    print("尚未加载工程")
                else:
                    from .fingerprint import structure_fingerprint
                    print(structure_fingerprint(project))
            elif cmd == "report":
                report = self.engine.session.last_report
                print(json.dumps(report, ensure_ascii=False,
                                 indent=2, sort_keys=True)
                      if report else "暂无诊断报告")
            elif cmd == "failures":
                print(json.dumps(
                    self.engine.session.failure_list.to_dict_list(),
                    ensure_ascii=False, indent=2, sort_keys=True))
            elif cmd == "replay":
                self._replay(args)
            else:
                print(f"未知命令: {cmd} (输入 help 查看用法)")
        except IEditorError as exc:
            self.engine.session.failure_list.add(
                "interactive", exc,
                source_path=str(self.engine.session.project_file or ""),
                location=exc.context)
            print(f"[失败] {exc.code}: {exc}")
        except (IndexError, ValueError) as exc:
            print(f"[参数错误] {exc} (输入 help 查看用法)")
        return True

    def _run_one(self, op: Dict[str, Any]) -> None:
        result = self.engine.apply(op)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))

    def _replay(self, args: List[str]) -> None:
        sequence_path = self._resolve(args[0])
        operations = json.loads(Path(sequence_path).read_text(
            encoding="utf-8"))
        resume_after = -1
        if len(args) > 1 and args[1] == "resume":
            progress = read_progress(
                self.engine.session.work_dir, "sample_a")
            resume_after = int(progress["last_completed_op_seq"])
        summary = self.engine.run_sequence(operations, resume_after)
        print(json.dumps(summary, ensure_ascii=False, indent=2,
                         sort_keys=True))


def run_replay_batch(root: Path, sequence: Path, work_dir: Path,
                     config_dir: Path, resume: bool) -> Dict[str, Any]:
    config = load_render_config(config_dir / "render_config.json")
    version_map = load_version_map(config_dir / "version_map.json")
    quant_rules = load_quant_rules(config_dir / "quant_rules.json")
    engine = ReplayEngine(work_dir=Path(work_dir), config=config,
                          version_map=version_map, quant_rules=quant_rules,
                          root_dir=root)
    engine.install_signal_handlers()
    operations = json.loads(Path(sequence).read_text(encoding="utf-8"))
    resume_after = -1
    if resume:
        progress = read_progress(Path(work_dir), "sample_a")
        resume_after = int(progress["last_completed_op_seq"])
    return engine.run_sequence(operations, resume_after)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="本地确定性图像修复与风格迁移领域编辑器")
    parser.add_argument("--root", default=str(Path.cwd()),
                        help="工作区根目录(默认当前目录)")
    parser.add_argument("--config-dir", default=None,
                        help="配置目录(默认 <root>/config)")
    parser.add_argument("--replay", default=None,
                        help="非交互回放指定编辑序列 JSON")
    parser.add_argument("--work-dir", default=None,
                        help="回放工作目录")
    parser.add_argument("--resume", action="store_true",
                        help="从进度记录断点之后继续回放")
    ns = parser.parse_args(argv)
    root = Path(ns.root).resolve()
    config_dir = Path(ns.config_dir) if ns.config_dir else root / "config"

    if ns.replay:
        work_dir = Path(ns.work_dir) if ns.work_dir else (
            root / "workspace_demo" / "workspace_replay")
        summary = run_replay_batch(root, Path(ns.replay), work_dir,
                                   config_dir, ns.resume)
        print(json.dumps(summary, ensure_ascii=False, indent=2,
                         sort_keys=True))
        # Replay always completes: graceful stop still yields exit code 0.
        return 0

    session = InteractiveSession(root, config_dir)
    print("本地确定性图像修复/风格迁移编辑器 (输入 help 查看命令, exit 退出)")
    for line in sys.stdin:
        if not session.execute_line(line):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
