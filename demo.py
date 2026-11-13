"""业务演示：构造确定性样本，跑通正常/半合法/失败三种场景并打印结果。

运行：python demo.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from tests.helpers import decode, write_wav  # noqa: E402

DEMO_DIR = ROOT / "demo-output"


def banner(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def run_cli(source: Path, manifest: Path, output: Path, report: Path) -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "clipforge",
         "--source", str(source), "--manifest", str(manifest),
         "--output", str(output), "--report", str(report)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.stderr:
        print(proc.stderr.rstrip())
    return proc.returncode


def summarize(report: Path, output: Path) -> None:
    data = json.loads(report.read_text(encoding="utf-8"))
    print(f"\n  status            : {data['status']}")
    print(f"  summary           : {data['summary']}")
    for clip in data["clips"]:
        detail = ""
        if clip["status"] == "processed":
            detail = (f"帧 [{clip['start_frame']}, {clip['end_frame']}) "
                      f"输出偏移 {clip['output_offset_frame']} gain={clip['gain']}")
        else:
            detail = clip["reasons"][0]["message"]
        print(f"    {clip['label']:>4} {clip['status']:<12} {detail}")
    if output.exists():
        with wave.open(str(output), "rb") as wf:
            print(f"\n  成片 {output.name}: {wf.getnframes()} 帧 / "
                  f"{wf.getnframes() / wf.getframerate():.3f} 秒, "
                  f"{wf.getnchannels()} 声道, {wf.getsampwidth() * 8} bit, "
                  f"{wf.getframerate()} Hz")


def main() -> int:
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)
    DEMO_DIR.mkdir(parents=True)

    banner("准备确定性样本：2 声道 / 16 bit / 44.1kHz，共 3 秒")
    source = DEMO_DIR / "source-stereo.wav"
    write_wav(source, sample_rate=44100, channels=2, sample_width=2, frames=132300)
    print(f"  已生成 {source.relative_to(ROOT)}")

    banner("场景 A：清单全部合法（含 gain 缺省 / 0.5 / 1.5）")
    manifest_a = DEMO_DIR / "manifest-valid.json"
    manifest_a.write_text(json.dumps([
        {"start": 0.0, "end": 0.75},
        {"start": 1.25, "end": 1.75, "gain": 0.5},
        {"start": 2.5, "end": 3.0, "gain": 1.5},
    ], indent=2), encoding="utf-8")
    report_a = DEMO_DIR / "report-valid.json"
    out_a = DEMO_DIR / "edit-valid.wav"
    code_a = run_cli(source, manifest_a, out_a, report_a)
    summarize(report_a, out_a)

    banner("场景 B：合法与非法混合（gain 越界 / 越界区间 / 倒置区间）")
    manifest_b = DEMO_DIR / "manifest-mixed.json"
    manifest_b.write_text(json.dumps([
        {"start": 0.0, "end": 0.5},
        {"start": 0.8, "end": 1.3, "gain": 5.0},
        {"start": 1.5, "end": 2.0, "gain": 2.0},
        {"start": 3.2, "end": 4.0},
        {"start": 2.6, "end": 2.4},
        {"start": 2.0, "end": 2.5},
    ], indent=2), encoding="utf-8")
    report_b = DEMO_DIR / "report-mixed.json"
    out_b = DEMO_DIR / "edit-mixed.wav"
    code_b = run_cli(source, manifest_b, out_b, report_b)
    summarize(report_b, out_b)

    # 对照：仅合法项，验证成片逐字节一致。
    only_valid_manifest = DEMO_DIR / "manifest-mixed-valid-only.json"
    only_valid_manifest.write_text(json.dumps([
        {"start": 0.0, "end": 0.5},
        {"start": 1.5, "end": 2.0, "gain": 2.0},
        {"start": 2.0, "end": 2.5},
    ], indent=2), encoding="utf-8")
    out_clean = DEMO_DIR / "edit-mixed-valid-only.wav"
    run_cli(source, only_valid_manifest, out_clean, DEMO_DIR / "report-valid-only.json")
    identical = out_b.read_bytes() == out_clean.read_bytes()
    print(f"\n  成片与仅合法清单结果逐字节一致：{identical}")

    banner("场景 C：源文件缺失（应非零退出且不产生成片）")
    report_c = DEMO_DIR / "report-missing.json"
    out_c = DEMO_DIR / "edit-missing.wav"
    code_c = run_cli(DEMO_DIR / "no-such-source.wav", manifest_a, out_c, report_c)
    print(f"\n  退出码 {code_c}，成片存在：{out_c.exists()}，"
          f"报告存在：{report_c.exists()}")

    banner("演示结果")
    print(f"  A 退出码 {code_a}（0=成功）")
    print(f"  B 退出码 {code_b}（0=成功，非法项被逐项报告）")
    print(f"  C 退出码 {code_c}（非 0=失败）")
    print(f"  输出目录：{DEMO_DIR.relative_to(ROOT)}")
    return 0 if (code_a == 0 and code_b == 0 and code_c != 0 and identical) else 1


if __name__ == "__main__":
    raise SystemExit(main())
