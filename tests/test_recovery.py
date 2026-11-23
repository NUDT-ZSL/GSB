"""业务要求 3：强制终止恢复、KeyboardInterrupt 优雅退出与逐字节一致。

强制终止通过独立子进程注入 SIGTERM（POSIX）模拟，Windows 下等价为
TerminateProcess；进程不做 Python 清理，只承诺基于已提交快照恢复。
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from rope_attn.generate_tasks import write_tasks
from tests._helpers import TempWorkspace


def _run_demo(tasks_path: Path, cache: Path, state: Path,
              out: Path, extra=None) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable, "-m", "rope_attn.demo",
        "--tasks", str(tasks_path),
        "--cache", str(cache),
        "--state", str(state),
        "--out", str(out),
        "--workers", "1",
    ]
    if extra:
        cmd.extend(extra)
    return subprocess.run(
        cmd, capture_output=True, text=True, cwd=Path(__file__).parents[1]
    )


class RecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = TempWorkspace()
        self.tasks_path = self.workspace.root / "tasks.jsonl"
        write_tasks(str(self.tasks_path))

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def test_hard_kill_resume_matches_uninterrupted(self):
        cache_a = self.workspace.root / "cache_full"
        state_a = self.workspace.root / "state_full"
        out_a = self.workspace.root / "full.json"
        full = _run_demo(self.tasks_path, cache_a, state_a, out_a)
        self.assertEqual(full.returncode, 0, full.stderr)
        full_report = json.loads(out_a.read_text(encoding="utf-8"))

        cache_b = self.workspace.root / "cache_kill"
        state_b = self.workspace.root / "state_kill"
        out_kill = self.workspace.root / "killed.json"
        killed = _run_demo(
            self.tasks_path, cache_b, state_b, out_kill,
            extra=["--kill-after-stage", "2"],
        )
        # 强杀路径：子进程被 SIGTERM 终止，返回码非 0。
        self.assertNotEqual(killed.returncode, 0, killed.stdout)
        snapshots = list((state_b / "snapshots").glob("*.json"))
        self.assertEqual(len(snapshots), 2)
        # 未提交的进度侧车不影响恢复，但可能残留；恢复后必须仍正确。

        out_resume = self.workspace.root / "resume.json"
        resumed = _run_demo(
            self.tasks_path, cache_b, state_b, out_resume
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        resume_report = json.loads(out_resume.read_text(encoding="utf-8"))
        self.assertEqual(resume_report["status"], "completed")
        self.assertIsNotNone(
            resume_report["pipeline"]["resumed_from"]
        )
        self.assertEqual(
            resume_report["pipeline"]["resumed_from"]["version"], 2
        )
        # 恢复后完整输出摘要与不间断运行逐字节一致。
        self.assertEqual(
            resume_report["pipeline"]["output_digest"],
            full_report["pipeline"]["output_digest"],
        )
        # 恢复运行不应再产生未提交阶段的缓存 miss 重算（快照覆盖节点全命中）。
        self.assertEqual(
            resume_report["pipeline"]["stage_counts"]["angles"], 0
        )
        self.assertEqual(
            resume_report["pipeline"]["stage_counts"]["rotate"], 0
        )

    def test_keyboard_interrupt_graceful_exit_report(self):
        cache = self.workspace.root / "cache_int"
        state = self.workspace.root / "state_int"
        out = self.workspace.root / "interrupt.json"
        proc = _run_demo(
            self.tasks_path, cache, state, out,
            extra=["--interrupt-after-stage", "2"],
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "interrupted")
        self.assertTrue(
            report["pipeline"]["exit_report"]["graceful"]
        )
        self.assertIn("E_INTERRUPTED_GRACEFUL", report["business_codes"])

        # 中断后续跑同样能达到与完整运行一致的摘要。
        cache_full = self.workspace.root / "cache_full2"
        state_full = self.workspace.root / "state_full2"
        out_full = self.workspace.root / "full2.json"
        full = _run_demo(
            self.tasks_path, cache_full, state_full, out_full
        )
        self.assertEqual(full.returncode, 0, full.stderr)
        full_digest = json.loads(
            out_full.read_text(encoding="utf-8")
        )["pipeline"]["output_digest"]

        out_resume = self.workspace.root / "resume2.json"
        resume = _run_demo(self.tasks_path, cache, state, out_resume)
        self.assertEqual(resume.returncode, 0, resume.stderr)
        resume_digest = json.loads(
            out_resume.read_text(encoding="utf-8")
        )["pipeline"]["output_digest"]
        self.assertEqual(resume_digest, full_digest)


if __name__ == "__main__":
    unittest.main()
