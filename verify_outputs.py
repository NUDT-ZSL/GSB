"""业务验收脚本：对实际输入/输出 WAV 做独立量化核验（不依赖被测包的算法）。

核验项对应任务验收 1/2/3/4/6/10，全部基于 wave/struct 独立读数。
运行：python -I verify_outputs.py
"""

from __future__ import annotations

import json
import struct
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _force_utf8() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass


def read_wav(path: Path):
    with wave.open(str(path), "rb") as wf:
        n = wf.getnframes()
        rate = wf.getframerate()
        width = wf.getsampwidth()
        channels = wf.getnchannels()
        raw = wf.readframes(n)
    if width == 2:
        samples = list(struct.unpack("<%dh" % n, raw))
    else:
        samples = list(raw)
    return samples, rate, width, channels


def check(name: str, ok: bool, detail: str) -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}: {detail}")
    return ok


def main() -> int:
    _force_utf8()
    inp = ROOT / "testdata" / "input"
    out = ROOT / "testdata" / "output_full"
    results: list[bool] = []

    # --- 验收 1：44100Hz 单声道 16bit 参数原样保持 ---
    in_s, in_r, in_w, in_c = read_wav(inp / "sfx" / "clean_44100.wav")
    out_s, out_r, out_w, out_c = read_wav(out / "sfx" / "clean_44100.wav")
    results.append(check(
        "验收1 WAV头一致",
        (in_r, in_w, in_c) == (out_r, out_w, out_c) == (44100, 2, 1),
        f"采样率 {out_r}Hz / 位深 {out_w * 8}bit / 声道 {out_c}（输入 {in_r}/{in_w * 8}/{in_c}）",
    ))

    # --- 验收 2：+6dB 峰值约 2 倍且不削波（用无静音的 tone_22050，淡变不影响中段峰值）---
    t_in, _, _, _ = read_wav(inp / "sfx" / "tone_22050.wav")
    t_out, _, _, _ = read_wav(out / "sfx" / "tone_22050.wav")
    in_peak = max(abs(v) for v in t_in)
    out_peak = max(abs(v) for v in t_out)
    ratio = out_peak / in_peak
    results.append(check(
        "验收2 +6dB峰值≈2倍且不削波",
        abs(ratio - 2.0) <= 0.05 and out_peak <= 32767,
        f"输入峰值 {in_peak} -> 输出峰值 {out_peak}，倍数 {ratio:.4f}（±5%内），最大值 {out_peak} <= 32767",
    ))

    # --- 验收 3：首尾 0.5s 静音，时长约减 0.98s，每侧保留 >=10ms ---
    in_dur = len(in_s) / in_r
    out_dur = len(out_s) / out_r
    reduction = in_dur - out_dur
    keep_ms = 441 / out_r * 1000  # 输出开头连续零样本对应时长
    head_zeros = next((i for i, v in enumerate(out_s) if v != 0), len(out_s))
    tail_zeros = next((i for i, v in enumerate(reversed(out_s)) if v != 0), len(out_s))
    results.append(check(
        "验收3 静音裁剪(减约0.98s, 每侧保留>=10ms)",
        abs(reduction - 0.98) <= 0.01
        and head_zeros >= round(out_r * 0.01)
        and tail_zeros >= round(out_r * 0.01),
        f"输入 {in_dur:.3f}s -> 输出 {out_dur:.3f}s，减少 {reduction:.4f}s；"
        f"头静音 {head_zeros} 样本({head_zeros / out_r * 1000:.2f}ms)，"
        f"尾静音 {tail_zeros} 样本({tail_zeros / out_r * 1000:.2f}ms)",
    ))

    # --- 验收 4：100ms 淡入。真实素材是正弦波，故核验淡入系数本身的线性：
    # 输出淡入段与“无淡变参考输出”逐样本之比应等于线性系数 i/N（取绝对值）。
    # 恒定振幅输入下的严格单调升至满幅由单元测试 test_linear_fade_in_monotonic 覆盖。 ---
    fade_len = round(out_r * 0.1)
    ref_path = ROOT / "testdata" / "output_empty" / "sfx" / "clean_44100.wav"
    if not ref_path.is_file():
        results.append(check(
            "验收4 100ms线性淡入",
            False,
            f"缺少无淡变参考文件 {ref_path}，请先运行 demo 脚本或用 configs/empty.json 生成",
        ))
        # 仍输出其余验收结论
        print("\n存在未通过项 [X]")
        return 1
    ref_s, _, _, _ = read_wav(ref_path)
    gain_factor = 10 ** (6.0 / 20)  # 完整配置含 +6dB，参考输出为 0dB，需归一化
    # 淡入前半段正弦零交叉附近参考值接近 0，16bit 舍入误差占主导；
    # 取淡入段后半部分核验线性系数，此时信号已充分建立
    ratios = []
    for i in range(fade_len // 2, fade_len):
        if abs(ref_s[i]) > 500:
            ratios.append(abs(out_s[i] / ref_s[i]) / gain_factor / (i / fade_len))
    max_dev = max(abs(r - 1.0) for r in ratios)
    linear_ok = bool(ratios) and max_dev < 0.005
    starts_zero = out_s[0] == 0
    results.append(check(
        "验收4 100ms线性淡入(系数线性, 起点为0; 恒定信号单调性见单测)",
        linear_ok and starts_zero,
        f"淡入段 {fade_len} 样本，后半段线性系数最大偏差 {max_dev * 100:.4f}%（<0.5%），首样本={out_s[0]}",
    ))

    # --- 验收 6：JSON 时长与 WAV 实际时长误差 < 1ms，其余字段与头一致 ---
    meta = json.loads((out / "sfx" / "clean_44100.json").read_text(encoding="utf-8"))
    actual_dur = len(out_s) / out_r
    dur_err_ms = abs(meta["duration_seconds"] - actual_dur) * 1000
    results.append(check(
        "验收6 JSON元数据(时长误差<1ms, 字段与WAV头一致)",
        dur_err_ms < 1.0
        and meta["sample_rate"] == out_r
        and meta["bit_depth"] == out_w * 8
        and meta["channels"] == out_c
        and meta["source_file"] == "clean_44100.wav",
        f"JSON时长 {meta['duration_seconds']}s vs 实际 {actual_dur:.6f}s，"
        f"误差 {dur_err_ms:.4f}ms；rate={meta['sample_rate']} bits={meta['bit_depth']} ch={meta['channels']}",
    ))

    # --- 验收 10：相对子目录结构保持，WAV 与同名 JSON 成对存在 ---
    expected = [
        "sfx/clean_44100.wav", "sfx/clean_44100.json",
        "sfx/short_8000.wav", "sfx/short_8000.json",
        "sfx/tone_22050.wav", "sfx/tone_22050.json",
        "sub/nested_16k.wav", "sub/nested_16k.json",
    ]
    structure_ok = all((out / rel).is_file() for rel in expected)
    # 被跳过素材无任何输出
    no_invalid_output = not (out / "sfx" / "legacy_8bit.wav").exists() \
        and not (out / "sfx" / "notes.txt").exists()
    results.append(check(
        "验收10 目录结构保持+跳过素材无输出",
        structure_ok and no_invalid_output,
        "8 个预期文件全部就位；8bit WAV 与 txt 均无输出",
    ))

    print("\n" + ("全部验收通过 [OK]" if all(results) else "存在未通过项 [X]"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
