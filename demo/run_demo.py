"""一键运行全部业务演示场景，并把控制台输出留存到 demo/generated/run_log.txt。

场景::

    A 主流程   单声道配乐 + 6 镜头主清单（含跳过镜头、总长超 1 秒）
    B 三镜头   3 条有效镜头，验证三张预览图（需求 5）
    C 双声道   明确拒绝并说明原因（需求 1）
    D 损坏 WAV 读取失败，输出目录无新增预览图（需求 7）
    E 不可写   输出路径被同名文件占用导致不可写，JSON 仍可读（需求 9）
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

# 允许直接 python demo/run_demo.py 运行：把仓库根目录加入 sys.path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from beat_align.cli import main as cli_main  # noqa: E402
from beat_align.errors import BeatAlignError  # noqa: E402
from beat_align.pipeline import run_pipeline  # noqa: E402
from demo.generate_demo_data import generate  # noqa: E402

GEN_DIR = os.path.join(ROOT, "demo", "generated")


def _section(title: str) -> str:
    return "\n" + "#" * 68 + f"\n# {title}\n" + "#" * 68 + "\n"


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli_main(argv)
    return code, out.getvalue(), err.getvalue()


def main() -> int:
    paths = generate(os.path.join(ROOT, "demo"))
    os.makedirs(GEN_DIR, exist_ok=True)
    log_parts: list[str] = []

    # --- 场景 A：主流程 ----------------------------------------------------
    log_parts.append(_section("场景 A 主流程：6 镜头（含跳过），总长超配乐 1 秒"))
    code, out, err = run_cli(
        [
            "--wav", paths["mono"],
            "--shots", paths["shots_main"],
            "--output-dir", os.path.join(GEN_DIR, "a_main", "previews"),
            "--report", os.path.join(GEN_DIR, "a_main", "rhythm_report.json"),
        ]
    )
    log_parts.append(f"退出码: {code}\n{out}{err}")

    # --- 场景 B：三镜头三张预览 -------------------------------------------
    log_parts.append(_section("场景 B：3 条有效镜头 -> 3 张切换预览图"))
    code, out, err = run_cli(
        [
            "--wav", paths["mono"],
            "--shots", paths["shots_three"],
            "--output-dir", os.path.join(GEN_DIR, "b_three", "previews"),
            "--report", os.path.join(GEN_DIR, "b_three", "rhythm_report.json"),
        ]
    )
    log_parts.append(f"退出码: {code}\n{out}{err}")

    # --- 场景 C：双声道被拒绝 ---------------------------------------------
    log_parts.append(_section("场景 C：双声道 WAV 被明确拒绝（需求 1）"))
    code, out, err = run_cli(
        [
            "--wav", paths["stereo"],
            "--shots", paths["shots_three"],
            "--output-dir", os.path.join(GEN_DIR, "c_stereo", "previews"),
        ]
    )
    log_parts.append(f"退出码: {code}（预期 2）\n{out}{err}")

    # --- 场景 D：损坏 WAV，不得产出预览 -----------------------------------
    d_dir = os.path.join(GEN_DIR, "d_corrupt")
    os.makedirs(d_dir, exist_ok=True)
    before = set(os.listdir(d_dir))
    log_parts.append(_section("场景 D：损坏 WAV 读取失败且无新增预览（需求 7）"))
    code, out, err = run_cli(
        [
            "--wav", paths["corrupt"],
            "--shots", paths["shots_three"],
            "--output-dir", os.path.join(d_dir, "previews"),
            "--report", os.path.join(d_dir, "rhythm_report.json"),
        ]
    )
    after = set(os.listdir(d_dir))
    log_parts.append(f"退出码: {code}（预期 2）\n{out}{err}")
    log_parts.append(f"运行前后输出目录内容差异: {sorted(after - before)}（预期为空）\n")

    # --- 场景 E：输出目录不可写，JSON 仍可读 ------------------------------
    e_dir = os.path.join(GEN_DIR, "e_readonly")
    os.makedirs(e_dir, exist_ok=True)
    blocked_dir = os.path.join(e_dir, "previews")
    # Windows 只读位对目录不可靠；用同名文件占用路径，确保 mkdir 一定失败
    with open(blocked_dir, "wb") as handle:
        handle.write(b"blocked")
    report_path = os.path.join(e_dir, "rhythm_report.json")
    log_parts.append(_section("场景 E：预览目录不可写，核查 JSON 仍完整（需求 9）"))
    try:
        result = run_pipeline(
            wav_path=paths["mono"],
            shots_path=paths["shots_main"],
            output_dir=blocked_dir,
            report_path=report_path,
        )
    except BeatAlignError as exc:  # pragma: no cover - 防御
        log_parts.append(f"未预期地抛出异常: {exc}\n")
    else:
        log_parts.append(
            f"preview_error: {result.preview_error}\n"
            f"JSON 路径: {result.report_path}\n"
            f"JSON 条目: completed={result.report['summary']['completed_count']}, "
            f"incomplete={result.report['summary']['incomplete_count']}\n"
        )
        log_parts.append("提示: 跨平台只读目录的单元测试见 tests/test_all_requirements.py 场景 9。\n")

    full_log = "".join(log_parts)
    log_path = os.path.join(GEN_DIR, "run_log.txt")
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write(full_log)
    print(full_log)
    print(f"完整日志已写入: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
