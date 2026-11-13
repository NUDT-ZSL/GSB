"""端到端演示与验收脚本：确定性样本 + 全部 11 项验收点自动断言。

运行::

    python -I scripts/run_demo.py

脚本会在 demo/ 下生成输入样本，构造 6 组指令场景，以子进程方式调用 CLI
（模拟真实用户操作），再用 wave/array 读回输出逐项断言，并打印验收清单。
退出码 0 表示全部验收通过，非 0 表示存在失败断言。
"""

from __future__ import annotations

import array
import json
import math
import os
import subprocess
import sys
import wave

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from scripts.generate_samples import SR, generate  # noqa: E402

INPUT_DIR = os.path.join(PROJECT_ROOT, "demo", "input")
OUT_DIR = os.path.join(PROJECT_ROOT, "demo", "output")

PASS_COUNT = 0
FAIL_COUNT = 0
FAILURES = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS_COUNT, FAIL_COUNT
    if condition:
        PASS_COUNT += 1
        print(f"  [PASS] {name}")
    else:
        FAIL_COUNT += 1
        FAILURES.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


def run_cli(instructions_path: str, out_wav: str, report_path: str,
            work_dir: str, fresh: bool = False) -> int:
    cmd = [
        sys.executable, "-I", os.path.join("scripts", "run_cli.py"),
        "--instructions", instructions_path,
        "--output", out_wav,
        "--report", report_path,
        "--work-dir", work_dir,
        "--base-dir", INPUT_DIR,
    ]
    if fresh:
        cmd.append("--fresh")
    proc = subprocess.run(
        cmd, cwd=PROJECT_ROOT, capture_output=True
    )
    # Windows 中文控制台默认 GBK 输出；按系统编码解码，失败不致命
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    if proc.stdout:
        text = proc.stdout.decode(encoding, errors="replace")
        print("    " + text.strip().replace("\n", "\n    "))
    if proc.stderr:
        text = proc.stderr.decode(encoding, errors="replace")
        print("    [stderr] " + text.strip())
    return proc.returncode


def load_report(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_wav(path: str):
    with wave.open(path, "rb") as wf:
        params = wf.getparams()
        raw = wf.readframes(wf.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    return params, samples


def expected_voice_sample(frame_index: int) -> int:
    """generate_samples 中 voice_mono.wav 的确定性公式（用于内容核对）。"""
    t = frame_index / SR
    envelope = 0.85 + 0.15 * math.sin(2 * math.pi * 0.5 * t)
    value = sum(math.sin(2 * math.pi * f * t) for f in (220.0, 330.0, 440.0))
    value = value / 3 * 12000 * envelope
    return max(-32768, min(32767, int(round(value))))


def write_instructions(scenario_dir: str, name: str, instructions: list) -> str:
    path = os.path.join(scenario_dir, f"{name}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(instructions, fh, ensure_ascii=False, indent=2)
    return path


def find_instr(report: dict, index: int) -> dict:
    return report["instructions"][index]


# --------------------------------------------------------------------- #
# 场景 A：三种指令 + 多类错误混合（验收点 1/3/4/6/8/10/11）
# --------------------------------------------------------------------- #
def scenario_a() -> None:
    print("\n=== 场景 A：裁剪→拼接→音量 全流程，混入越界/缺失/非法增益 ===")
    sd = os.path.join(OUT_DIR, "scenario_a")
    os.makedirs(sd, exist_ok=True)
    instructions = [
        # 0 裁剪 2.0~7.0s（5s），顺序依赖的起点
        {"type": "crop", "source": "voice_mono.wav",
         "params": {"start": 2.0, "end": 7.0}},
        # 1 在 1.0s 处插入 3s 广告 → 8s
        {"type": "splice", "source": "ad_mono.wav", "params": {"at": 1.0}},
        # 2 合法音量：整条时间线 -6dB
        {"type": "volume", "source": None, "params": {"gain_db": -6.0}},
        # 3 裁剪越界：voice 仅 10s，请求 8~12s
        {"type": "crop", "source": "voice_mono.wav",
         "params": {"start": 8.0, "end": 12.0}},
        # 4 插入点越界：时间线 8s，请求在 20s 插入
        {"type": "splice", "source": "ad_mono.wav", "params": {"at": 20.0}},
        # 5 增益越界：+90dB
        {"type": "volume", "source": None, "params": {"gain_db": 90.0}},
        # 6 引用始终不存在的文件
        {"type": "crop", "source": "ghost_missing.wav",
         "params": {"start": 0.0, "end": 1.0}},
    ]
    instr_path = write_instructions(sd, "instructions", instructions)
    out_wav = os.path.join(sd, "episode_a.wav")
    report_path = os.path.join(sd, "report_a.json")
    code = run_cli(instr_path, out_wav, report_path, sd, fresh=True)
    report = load_report(report_path)

    check("A-退出码 2（部分失败但产出可用）", code == 2, f"got {code}")
    check("A-overall_status=incomplete", report["overall_status"] == "incomplete")
    check("A-汇总 3 成功/4 失败",
          report["summary"]["succeeded"] == 3 and report["summary"]["failed"] == 4,
          str(report["summary"]))

    for idx, expect_status in [(0, "success"), (1, "success"), (2, "success"),
                               (3, "failed"), (4, "failed"), (5, "failed"),
                               (6, "failed")]:
        check(f"A-指令{idx}状态={expect_status}",
              find_instr(report, idx)["status"] == expect_status)

    check("A-指令3错误码 TIME_OUT_OF_RANGE",
          find_instr(report, 3)["error"]["code"] == "TIME_OUT_OF_RANGE")
    check("A-指令4错误码 INSERT_OUT_OF_RANGE",
          find_instr(report, 4)["error"]["code"] == "INSERT_OUT_OF_RANGE")
    check("A-指令5错误码 GAIN_OUT_OF_RANGE",
          find_instr(report, 5)["error"]["code"] == "GAIN_OUT_OF_RANGE")
    check("A-指令6错误码 SOURCE_NOT_FOUND 且可重试",
          find_instr(report, 6)["error"]["code"] == "SOURCE_NOT_FOUND"
          and find_instr(report, 6)["error"]["retryable"] is True)
    check("A-每条失败都有人类可读消息",
          all(i["error"] and i["error"]["message"]
              for i in report["instructions"] if i["status"] == "failed"))
    check("A-不可用来源清单包含 ghost_missing.wav",
          any(s["source"] == "ghost_missing.wav"
              for s in report["unavailable_sources"]),
          str(report["unavailable_sources"]))

    # 输出时长：5s + 3s = 8s（失败指令不改变时间线）
    expected_frames = int(round(8.0 * SR))
    check("A-输出时长 8.0s（帧级）",
          report["summary"]["total_frames"] == expected_frames,
          f"got {report['summary']['total_frames']}")

    params, samples = read_wav(out_wav)
    check("A-输出单声道/16bit/44100Hz",
          params.nchannels == 1 and params.sampwidth == 2
          and params.framerate == SR)
    check("A-wave 读回帧数与报告一致",
          params.nframes == report["summary"]["total_frames"])

    # 顺序执行内容核对：[0,1s) 为裁剪后 voice 的前 1s（再经 -6dB）
    factor = 10 ** (-6 / 20)
    ok_content = True
    for frame in (0, 100, 44099):
        expect = int(round(expected_voice_sample(2 * SR + frame) * factor))
        expect = max(-32768, min(32767, expect))
        if samples[frame] != expect:
            ok_content = False
            break
    check("A-时间线开头内容=裁剪voice经-6dB（顺序依赖正确）", ok_content)

    # [1s,4s) 应为广告内容（幅度特征区别于 voice）
    ad_peak = max(abs(samples[i]) for i in range(SR, 4 * SR, 97))
    check("A-广告段已插入 1s 处（非零音频）", ad_peak > 1000, f"peak={ad_peak}")

    # -6dB 后幅度不超过原始上限 * factor，且不超 16-bit 最大值
    check("A-全部样本不超 16-bit 最大幅度",
          all(-32768 <= v <= 32767 for v in samples))
    check("A-报告时长字段与实际 WAV 一致",
          abs(report["summary"]["total_duration_seconds"]
              - params.nframes / SR) < 1e-9)


# --------------------------------------------------------------------- #
# 场景 B：立体声来源被跳过（验收点 2）
# --------------------------------------------------------------------- #
def scenario_b() -> None:
    print("\n=== 场景 B：立体声 WAV 被跳过，单声道正常处理 ===")
    sd = os.path.join(OUT_DIR, "scenario_b")
    os.makedirs(sd, exist_ok=True)
    instructions = [
        {"type": "crop", "source": "bg_stereo.wav",
         "params": {"start": 0.0, "end": 1.0}},
        {"type": "crop", "source": "voice_mono.wav",
         "params": {"start": 0.0, "end": 1.5}},
    ]
    instr_path = write_instructions(sd, "instructions", instructions)
    out_wav = os.path.join(sd, "episode_b.wav")
    report_path = os.path.join(sd, "report_b.json")
    code = run_cli(instr_path, out_wav, report_path, sd, fresh=True)
    report = load_report(report_path)

    check("B-退出码 2", code == 2, f"got {code}")
    check("B-立体声指令失败且错误码 INVALID_WAV_FORMAT",
          find_instr(report, 0)["status"] == "failed"
          and find_instr(report, 0)["error"]["code"] == "INVALID_WAV_FORMAT"
          and find_instr(report, 0)["error"]["retryable"] is False)
    check("B-单声道指令成功", find_instr(report, 1)["status"] == "success")
    check("B-不可用来源登记 bg_stereo.wav",
          any(s["source"] == "bg_stereo.wav" for s in report["unavailable_sources"]))
    check("B-输出为 1.5s 单声道内容",
          report["summary"]["total_frames"] == int(round(1.5 * SR)))
    if os.path.exists(out_wav):
        params, _ = read_wav(out_wav)
        check("B-输出 WAV 参数合法",
              params.nchannels == 1 and params.sampwidth == 2
              and params.framerate == SR)


# --------------------------------------------------------------------- #
# 场景 C：插入点越界，有效插入正常（验收点 4）
# --------------------------------------------------------------------- #
def scenario_c() -> None:
    print("\n=== 场景 C：有效插入成功 + 无效时间点跳过 ===")
    sd = os.path.join(OUT_DIR, "scenario_c")
    os.makedirs(sd, exist_ok=True)
    instructions = [
        {"type": "crop", "source": "voice_mono.wav",
         "params": {"start": 0.0, "end": 5.0}},
        {"type": "splice", "source": "ad_mono.wav", "params": {"at": 5.0}},
        {"type": "splice", "source": "ad_mono.wav", "params": {"at": 12.0}},
        {"type": "splice", "source": "ad_mono.wav", "params": {"at": 0.0}},
    ]
    instr_path = write_instructions(sd, "instructions", instructions)
    out_wav = os.path.join(sd, "episode_c.wav")
    report_path = os.path.join(sd, "report_c.json")
    code = run_cli(instr_path, out_wav, report_path, sd, fresh=True)
    report = load_report(report_path)

    check("C-尾部追加(at=时长)成功", find_instr(report, 1)["status"] == "success")
    check("C-at=12 越界失败 INSERT_OUT_OF_RANGE",
          find_instr(report, 2)["status"] == "failed"
          and find_instr(report, 2)["error"]["code"] == "INSERT_OUT_OF_RANGE")
    check("C-头部插入(at=0)成功", find_instr(report, 3)["status"] == "success")
    # 5s + 3s（尾插）+ 3s（头插）= 11s；越界指令无副作用
    check("C-最终时长 11.0s",
          report["summary"]["total_frames"] == int(round(11.0 * SR)),
          str(report["summary"]["total_frames"]))
    check("C-退出码 2", code == 2, f"got {code}")


# --------------------------------------------------------------------- #
# 场景 D：增益边界与钳位防削波（验收点 5/9）
# --------------------------------------------------------------------- #
def scenario_d() -> None:
    print("\n=== 场景 D：±60dB 边界合法、±60 外拒绝、大音量样本钳位 ===")
    sd = os.path.join(OUT_DIR, "scenario_d")
    os.makedirs(sd, exist_ok=True)
    instructions = [
        # 大音量来源（峰值约 30000），+60dB 必然触发钳位但不溢出
        {"type": "crop", "source": "jingle_mono.wav",
         "params": {"start": 0.0, "end": 2.0}},
        {"type": "volume", "source": None, "params": {"gain_db": 60.0}},
        {"type": "volume", "source": None, "params": {"gain_db": -60.0}},
        {"type": "volume", "source": None, "params": {"gain_db": -60.5}},
        {"type": "volume", "source": None, "params": {"gain_db": 61.0}},
        # 独立验证负增益静音化：不受前面钳位影响
        {"type": "crop", "source": "jingle_mono.wav",
         "params": {"start": 0.0, "end": 1.0}},
        {"type": "volume", "source": None, "params": {"gain_db": -60.0}},
    ]
    instr_path = write_instructions(sd, "instructions", instructions)
    out_wav = os.path.join(sd, "episode_d.wav")
    report_path = os.path.join(sd, "report_d.json")
    code = run_cli(instr_path, out_wav, report_path, sd, fresh=True)
    report = load_report(report_path)

    check("D-+60dB 边界被接受", find_instr(report, 1)["status"] == "success")
    check("D--60dB 边界被接受", find_instr(report, 2)["status"] == "success")
    check("D--60.5dB 被拒绝 GAIN_OUT_OF_RANGE",
          find_instr(report, 3)["status"] == "failed"
          and find_instr(report, 3)["error"]["code"] == "GAIN_OUT_OF_RANGE")
    check("D-+61dB 被拒绝 GAIN_OUT_OF_RANGE",
          find_instr(report, 4)["status"] == "failed"
          and find_instr(report, 4)["error"]["code"] == "GAIN_OUT_OF_RANGE")
    check("D-+60dB 指令 detail 记录钳位样本数",
          find_instr(report, 1)["status"] == "success"
          and "钳位" in find_instr(report, 1)["detail"])

    params, samples = read_wav(out_wav)
    check("D-输出样本全部在 [-32768,32767]",
          all(SAMPLE_LO <= v <= SAMPLE_HI for v in samples))
    # 最终时间线为 jingle 1s 经 -60dB（系数 0.001）：近静音且无溢出
    check("D-负增益后峰值衰减到 33 以内（幅度缩放正确）",
          max(abs(v) for v in samples) <= 33,
          f"peak={max(abs(v) for v in samples)}")
    check("D-输出 WAV 单声道 16bit 44100Hz",
          params.nchannels == 1 and params.sampwidth == 2
          and params.framerate == SR)
    check("D-时长为 1.0s（最后一次 crop 重建时间线，失败增益无副作用）",
          params.nframes == int(round(1.0 * SR)))
    check("D-退出码 2", code == 2, f"got {code}")


SAMPLE_LO, SAMPLE_HI = -32768, 32767


# --------------------------------------------------------------------- #
# 场景 E：临时性失败 + 断点续跑（验收点 7）
# --------------------------------------------------------------------- #
def scenario_e() -> None:
    print("\n=== 场景 E：文件暂时不可读 → 首次失败，恢复后续跑仅补做该指令 ===")
    sd = os.path.join(OUT_DIR, "scenario_e")
    os.makedirs(sd, exist_ok=True)
    ad_original = os.path.join(INPUT_DIR, "ad_mono.wav")
    ad_hidden = os.path.join(INPUT_DIR, "ad_mono.wav.hidden")

    instructions = [
        {"type": "crop", "source": "voice_mono.wav",
         "params": {"start": 1.0, "end": 4.0}},                 # 3s
        {"type": "splice", "source": "ad_mono.wav",
         "params": {"at": 3.0}},                                # +3s → 6s
        {"type": "volume", "source": None,
         "params": {"gain_db": -10.0}},                         # 合法
    ]
    instr_path = write_instructions(sd, "instructions", instructions)
    out_wav = os.path.join(sd, "episode_e.wav")
    report_path = os.path.join(sd, "report_e.json")

    # --- 首次运行：临时移走 ad_mono.wav，指令 1 失败 ---
    os.replace(ad_original, ad_hidden)
    try:
        code1 = run_cli(instr_path, out_wav, report_path, sd, fresh=True)
    finally:
        # 无论断言如何都恢复文件
        os.replace(ad_hidden, ad_original)
    report1 = load_report(report_path)

    check("E-首跑退出码 2", code1 == 2, f"got {code1}")
    check("E-首跑: 指令0/2 成功，指令1 失败 SOURCE_NOT_READABLE/NotFound",
          find_instr(report1, 0)["status"] == "success"
          and find_instr(report1, 2)["status"] == "success"
          and find_instr(report1, 1)["status"] == "failed"
          and find_instr(report1, 1)["error"]["code"] in
          ("SOURCE_NOT_FOUND", "SOURCE_NOT_READABLE"))
    check("E-首跑时间线 3.0s（失败拼接未污染时间线）",
          report1["summary"]["total_frames"] == int(round(3.0 * SR)))
    check("E-检查点已落盘", os.path.exists(os.path.join(sd, "checkpoint.json")))
    _, samples_run1 = read_wav(out_wav)

    # --- 第二次运行：文件恢复，续跑（不带 --fresh）---
    code2 = run_cli(instr_path, out_wav, report_path, sd, fresh=False)
    report2 = load_report(report_path)
    _, samples_run2 = read_wav(out_wav)

    check("E-续跑退出码 0（全部成功）", code2 == 0, f"got {code2}")
    check("E-报告标记 resumed_from_checkpoint=true",
          report2["resumed_from_checkpoint"] is True)
    check("E-指令0/2 标记 resumed_skipped（未重复应用）",
          find_instr(report2, 0)["resumed_skipped"] is True
          and find_instr(report2, 2)["resumed_skipped"] is True)
    check("E-指令1 本次新应用（非 resumed）",
          find_instr(report2, 1)["status"] == "success"
          and find_instr(report2, 1)["resumed_skipped"] is False)
    check("E-续跑后时长 6.0s",
          report2["summary"]["total_frames"] == int(round(6.0 * SR)))
    check("E-输出内容确实变化（拼接被补做）",
          len(samples_run2) == len(samples_run1) + 3 * SR)

    # 前 3s 内容必须与首跑逐样本一致：已成功指令未被重复应用/污染
    check("E-前 3s 与首跑逐样本一致（成功部分不被污染）",
          samples_run2[: 3 * SR] == samples_run1)
    check("E-不可用来源清单已清空（来源恢复后重新可用）",
          report2["unavailable_sources"] == [])


# --------------------------------------------------------------------- #
# 场景 F：全部失败时显式无输出，不把缺失当空结果（验收点 6/11）
# --------------------------------------------------------------------- #
def scenario_f() -> None:
    print("\n=== 场景 F：全部指令失败 → 报告显式声明无输出 ===")
    sd = os.path.join(OUT_DIR, "scenario_f")
    os.makedirs(sd, exist_ok=True)
    instructions = [
        {"type": "crop", "source": "nope1.wav",
         "params": {"start": 0.0, "end": 1.0}},
        {"type": "crop", "source": "nope2.wav",
         "params": {"start": 0.0, "end": 1.0}},
        {"type": "unknown_op", "source": "x.wav", "params": {}},
    ]
    instr_path = write_instructions(sd, "instructions", instructions)
    out_wav = os.path.join(sd, "episode_f.wav")
    report_path = os.path.join(sd, "report_f.json")
    code = run_cli(instr_path, out_wav, report_path, sd, fresh=True)
    report = load_report(report_path)

    check("F-退出码 3（完全失败）", code == 3, f"got {code}")
    check("F-overall_status=failed", report["overall_status"] == "failed")
    check("F-输出显式 exists=false 并带 reason",
          report["output"]["exists"] is False and bool(report["output"]["reason"]))
    check("F-未生成 WAV 文件", not os.path.exists(out_wav))
    check("F-未知指令错误码 UNKNOWN_INSTRUCTION_TYPE",
          find_instr(report, 2)["error"]["code"] == "UNKNOWN_INSTRUCTION_TYPE")
    check("F-两个不可用来源均被列出",
          {s["source"] for s in report["unavailable_sources"]}
          == {"nope1.wav", "nope2.wav"})


def main() -> int:
    print("步骤 1：生成确定性输入样本 ...")
    generate(INPUT_DIR)

    scenario_a()
    scenario_b()
    scenario_c()
    scenario_d()
    scenario_e()
    scenario_f()

    print("\n" + "=" * 60)
    print(f"验收结果：通过 {PASS_COUNT} 项，失败 {FAIL_COUNT} 项")
    if FAILURES:
        print("失败项：")
        for item in FAILURES:
            print("  - " + item)
        return 1
    print("全部验收通过 [ALL CHECKS PASSED]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
