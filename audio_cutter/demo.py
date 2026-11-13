"""一键复跑：确定性生成样本 -> 覆盖全部 15 条验收项。

用法（在项目根目录）：
    python -m audio_cutter.demo

仅使用标准库；通过子进程调用 CLI，与真实命令行使用方式一致。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import wave
from typing import Any, Dict, List, Optional, Tuple

from . import samplegen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "demo_workspace")
CLI = [sys.executable, "-I", os.path.join(ROOT, "cli.py")]

results: List[Tuple[str, bool, str]] = []


def check(cid: str, ok: bool, detail: str = "") -> None:
    results.append((cid, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {cid} {detail}")


def run_cli(*args: str, cwd: Optional[str] = None
            ) -> Tuple[int, str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        CLI + list(args), cwd=cwd or ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode, proc.stdout, proc.stderr


def md5_of(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_tree(folder: str) -> Dict[str, str]:
    out = {}
    for name in sorted(os.listdir(folder)):
        p = os.path.join(folder, name)
        if os.path.isfile(p):
            out[name] = md5_of(p)
    return out


def read_report(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_report(report: Dict[str, Any]) -> str:
    """归一化报告：抹掉 overwritten 与绝对路径后序列化，用于确定性比对。"""
    import copy
    r = copy.deepcopy(report)
    r.get("input_summary", {}).pop("input_wav", None)
    r.get("input_summary", {}).pop("clips_json", None)
    for e in r.get("successes", []):
        e.pop("overwritten", None)
    return json.dumps(r, sort_keys=True, ensure_ascii=False)


def wav_info(path: str) -> Dict[str, Any]:
    with wave.open(path, "rb") as wf:
        return {
            "channels": wf.getnchannels(),
            "sample_width": wf.getsampwidth(),
            "sample_rate": wf.getframerate(),
            "frames": wf.getnframes(),
        }


def list_wavs(folder: str) -> List[str]:
    if not os.path.isdir(folder):
        return []
    return [f for f in os.listdir(folder) if f.lower().endswith(".wav")]


def main() -> int:
    if os.path.isdir(WORK):
        # 清理可能残留的只读文件
        def on_rm_error(func, path, _exc):
            os.chmod(path, stat.S_IWRITE)
            func(path)
        shutil.rmtree(WORK, onerror=on_rm_error)
    os.makedirs(WORK, exist_ok=True)

    # ---------------- 任务 15：确定性样本生成 ----------------
    paths = samplegen.build_all(WORK)
    check("15a", all(os.path.exists(p) for p in paths.values()),
          "样本与剪辑点全部生成")

    out_valid = os.path.join(WORK, "out_valid_48k")
    out_mono = os.path.join(WORK, "out_valid_44100")
    out_invalid = os.path.join(WORK, "out_invalid")
    out_badfmt = os.path.join(WORK, "out_badfmt")
    out_io = os.path.join(WORK, "out_io_fault")
    report_path = os.path.join(out_valid, "report.json")

    # ---------------- 任务 1：越界/倒置片段，整体失败且无 WAV ----------------
    os.makedirs(out_invalid, exist_ok=True)
    code, out, err = run_cli(
        "--input", paths["stereo_48000"],
        "--clips", paths["invalid_clips"],
        "--output-dir", out_invalid)
    rpt_bad = read_report(os.path.join(out_invalid, "report.json"))
    ids_named = {f["id"] for f in rpt_bad["failures"]}
    check("1a", code != 0, f"非法剪辑点非零退出 code={code}")
    check("1b", {"c101", "c102"}.issubset(ids_named),
          f"报告点名失败片段 {sorted(ids_named)}")
    check("1c", list_wavs(out_invalid) == [],
          "校验失败不产出任何 WAV")
    check("1d", any("超出音频时长" in f["reason"] for f in rpt_bad["failures"])
          and any("不小于终点" in f["reason"] for f in rpt_bad["failures"]),
          "失败原因具体可读")

    # ---------------- 任务 11：非法 WAV 格式终止且无输出 ----------------
    for key, expect in (("invalid_8bit", "8 位"), ("invalid_32k", "32000Hz")):
        os.makedirs(out_badfmt + "_" + key, exist_ok=True)
        code2, _o, err2 = run_cli(
            "--input", paths[key],
            "--clips", paths["valid_clips"],
            "--output-dir", out_badfmt + "_" + key)
        check(f"11-{key}", code2 != 0 and expect in err2
              and list_wavs(out_badfmt + "_" + key) == [],
              f"{expect} 被拒绝、非零退出、无 WAV；stderr: {err2.strip()[:80]}")

    # ---------------- 任务 10：缺参数打印用法、退出码 1/2 ----------------
    code3, _o, err3 = run_cli("--input", paths["mono_44100"])
    check("10", code3 in (1, 2) and "usage" in err3.lower(),
          f"缺必填参数退出码 {code3} 且打印用法")

    # ---------------- 正常流程：48k 立体声 ----------------
    code4, out4, _e = run_cli(
        "--input", paths["stereo_48000"],
        "--clips", paths["valid_clips"],
        "--output-dir", out_valid)
    check("cli-ok", code4 in (0, 5), f"正常流程退出码 {code4}")
    rpt = read_report(report_path)
    succ = {e["id"]: e for e in rpt["successes"]}
    fails = rpt["failures"]

    # ---------------- 任务 6：裁后为空片段不影响其他片段 ----------------
    check("6a", "c007" in {f["id"] for f in fails}
          and set(succ) == {"c001", "c002", "c003", "c004", "c005", "c006"},
          f"c007 失败，其余 6 段成功")
    check("6b", any("为空" in f["reason"] for f in fails),
          "失败原因标注裁切后为空")

    # ---------------- 任务 2/13：WAV 头、采样率、声道、帧数时长 ----------------
    src_info = wav_info(paths["stereo_48000"])
    c001_info = wav_info(os.path.join(out_valid, "c001.wav"))
    check("2a", c001_info["sample_rate"] == src_info["sample_rate"] == 48000
          and c001_info["channels"] == src_info["channels"] == 2
          and c001_info["sample_width"] == 2,
          "48k 立体声输出头与输入一致、无格式转换")
    expected_dur = 1.00 - 0.20
    actual_dur = c001_info["frames"] / c001_info["sample_rate"]
    check("13", abs(actual_dur - expected_dur) < 0.001
          and abs(actual_dur - succ["c001"]["processed_duration_seconds"])
          < 0.001,
          f"wave 可读，帧数时长 {actual_dur:.4f}s 与报告一致（<1ms）")

    # 44.1kHz 单声道同样验证
    run_cli("--input", paths["mono_44100"],
            "--clips", paths["valid_clips"], "--output-dir", out_mono)
    rpt_mono = read_report(os.path.join(out_mono, "report.json"))
    m_info = wav_info(os.path.join(out_mono, "c002.wav"))
    check("2b", m_info["sample_rate"] == 44100 and m_info["channels"] == 1
          and m_info["sample_width"] == 2,
          "44.1k 单声道输出头与输入一致")
    mono_dur = m_info["frames"] / 44100
    check("2c", abs(mono_dur - 0.80) < 0.001,
          f"44.1k 片段时长 {mono_dur:.4f}s 符合预期 0.80s")

    # ---------------- 任务 5：RMS 归一化与增益限制 ----------------
    normal_ok = all(
        abs(succ[c]["processed_rms_dbfs"] - (-20.0)) <= 0.5
        for c in ("c001", "c002", "c003", "c006")
    )
    check("5a", normal_ok,
          "正常片段处理后 RMS 落在 -20±0.5 dBFS: "
          + ", ".join(f"{c}={succ[c]['processed_rms_dbfs']:.2f}"
                      for c in ("c001", "c002", "c003", "c006")))
    quiet = succ["c005"]
    check("5b", quiet["gain"] <= 10.0 + 1e-12 and quiet["gain_clamped"]
          and any("上限 10.0" in n for n in quiet["notes"]),
          f"极弱片段增益 {quiet['gain']:.3f} 未超 10 倍且报告记录")
    # 增益下限 0.1：强音源 RMS≈-2.9dB，目标 -24dB 时所需增益 ≈0.088 < 0.1
    out_lower = os.path.join(WORK, "out_gain_lower")
    run_cli("--input", paths["stereo_48000"],
            "--clips", paths["valid_clips"], "--output-dir", out_lower,
            "--target-dbfs", "-24")
    rpt_lower = read_report(os.path.join(out_lower, "report.json"))
    loud = next(e for e in rpt_lower["successes"] if e["id"] == "c003")
    check("5c", loud["gain"] >= 0.1 - 1e-12 and loud["gain_clamped"]
          and any("下限 0.1" in n for n in loud["notes"]),
          f"强音片段(-24dB目标)增益 {loud['gain']:.3f} 不低于 0.1 且报告记录")

    # ---------------- 任务 4：淡入淡出可配置、超长自动缩短 ----------------
    c004 = succ["c004"]
    check("4a", c004["fade_in_seconds"] <= 0.0075 + 1e-12
          and c004["fade_out_seconds"] <= 0.0075 + 1e-12,
          f"0.015s 片段实际淡入 {c004['fade_in_seconds'] * 1000:.2f}ms"
          f"（≤7.5ms）")
    check("4b", any("一半" in n for n in c004["notes"]),
          "报告含淡入淡出缩短提示")
    check("4c", c004["fade_applied"] is True
          and succ["c001"]["fade_in_frames"] == int(0.01 * 48000),
          "默认 0.01s 淡入淡出生效")
    # 可配置：自定义 0.02s
    out_fade = os.path.join(WORK, "out_custom_fade")
    run_cli("--input", paths["stereo_48000"],
            "--clips", paths["valid_clips"], "--output-dir", out_fade,
            "--fade", "0.02", "--target-dbfs", "-18")
    rpt_fade = read_report(os.path.join(out_fade, "report.json"))
    f001 = next(e for e in rpt_fade["successes"] if e["id"] == "c001")
    check("4d", f001["fade_in_seconds"] == 0.02
          and rpt_fade["input_summary"]["target_dbfs"] == -18.0,
          "CLI 可配置淡变 0.02s 与目标响度 -18dBFS")

    # ---------------- 任务 9：立体声共用增益，声道电平差稳定 ----------------
    pre_l, pre_r = succ["c001"]["original_channel_rms_dbfs"]
    post_l, post_r = succ["c001"]["processed_channel_rms_dbfs"]
    diff_change = abs((post_l - post_r) - (pre_l - pre_r))
    check("9", diff_change < 0.1,
          f"左右电平差变化 {diff_change:.4f} dB（<0.1dB）")

    # ---------------- 任务 3：目录自动创建、同名覆盖并记录 ----------------
    check("3a", os.path.isdir(out_valid), "缺失输出目录已自动创建")
    pre_place = os.path.join(out_valid, "c002.wav")
    with open(pre_place, "wb") as f:
        f.write(b"STALE-CONTENT-MARKER")
    code5, out5, _e = run_cli(
        "--input", paths["stereo_48000"],
        "--clips", paths["valid_clips"], "--output-dir", out_valid)
    rpt2 = read_report(report_path)
    c002_entry = next(e for e in rpt2["successes"] if e["id"] == "c002")
    check("3b", c002_entry["overwritten"] is True
          and md5_of(pre_place) != hashlib.md5(b"STALE-CONTENT-MARKER")
          .hexdigest(),
          "预置同名文件被覆盖，报告记录 overwritten=true")

    # ---------------- 任务 7：报告字段完整、数值吻合 ----------------
    required_fields = {
        "id", "original_duration_seconds", "processed_duration_seconds",
        "original_rms_dbfs", "processed_rms_dbfs", "gain",
        "fade_applied", "fade_in_seconds", "fade_out_seconds",
    }
    check("7", required_fields.issubset(c002_entry.keys()),
          f"成功条目含全部约定字段（共 {len(c002_entry)} 个字段）")

    # ---------------- 任务 8：报告结构与失败列表升序 ----------------
    check("8a", set(rpt2.keys()) >= {"input_summary", "successes",
                                     "failures", "success_count",
                                     "failure_count"},
          "报告含输入摘要/成功列表/失败列表")
    fail_ids = [f["id"] for f in rpt2["failures"]]
    check("8b", fail_ids == sorted(fail_ids),
          f"失败列表按标识升序 {fail_ids}")

    # ---------------- 任务 12：控制台逐片段信息与摘要 ----------------
    check("12a", "[完成] 片段 c001" in out5 and "[失败] 片段 c007" in out5,
          "控制台逐片段输出完成/失败信息")
    check("12b", f"成功 6 个，失败 1 个" in out5
          and os.path.abspath(out_valid) in out5,
          "结束摘要数字与报告一致并打印输出目录")

    # ---------------- 任务 14：I/O 故障中断、保留已产出、报告记原因 ----------------
    os.makedirs(out_io, exist_ok=True)
    victim = os.path.join(out_io, "c001.wav")
    with open(victim, "wb") as f:
        f.write(b"read-only-placeholder")
    os.chmod(victim, stat.S_IREAD)  # 只读文件 -> 写入触发 PermissionError
    code6, _o6, err6 = run_cli(
        "--input", paths["stereo_48000"],
        "--clips", paths["valid_clips"], "--output-dir", out_io)
    os.chmod(victim, stat.S_IWRITE)  # 恢复权限便于清理
    rpt_io_path = os.path.join(out_io, "report.json")
    rpt_io = read_report(rpt_io_path) if os.path.exists(rpt_io_path) \
        else read_report(os.path.join(WORK, "report.json.interrupted.json"))
    check("14a", code6 != 0, f"I/O 故障非零退出 code={code6}")
    check("14b", rpt_io["status"] == "interrupted"
          and rpt_io["interrupted_reason"] and "c001" in
          rpt_io["interrupted_reason"],
          f"报告记录中断原因: {rpt_io['interrupted_reason'][:60]}")
    check("14c", rpt_io["success_count"] == 0
          and os.path.exists(victim),
          "中断前状态与已产出文件保留（c001 为首个片段，占位文件保留）")

    # ---------------- 任务 15：重复运行确定性（MD5 一致） ----------------
    # WAV 由确定性信号与定点 DSP 生成，重复运行字节必须完全一致。
    # 报告本身不含时间戳；唯一随运行变化的是 overwritten 标记（第二次
    # 运行确实发生了覆盖，属正确业务语义），比对时归一化该字段。
    out_rerun = os.path.join(WORK, "out_rerun")
    run_cli("--input", paths["stereo_48000"],
            "--clips", paths["valid_clips"], "--output-dir", out_rerun)
    wav_hash_1 = {k: v for k, v in md5_tree(out_rerun).items()
                  if k.endswith(".wav")}
    rpt_norm_1 = _normalize_report(read_report(
        os.path.join(out_rerun, "report.json")))
    run_cli("--input", paths["stereo_48000"],
            "--clips", paths["valid_clips"], "--output-dir", out_rerun)
    wav_hash_2 = {k: v for k, v in md5_tree(out_rerun).items()
                  if k.endswith(".wav")}
    rpt_norm_2 = _normalize_report(read_report(
        os.path.join(out_rerun, "report.json")))
    # 与另一个独立目录的 WAV 交叉验证（路径无关的字节一致性）
    wav_cross = all(md5_tree(out_valid).get(k) == v
                    for k, v in wav_hash_2.items())
    check("15b", wav_hash_1 == wav_hash_2 and wav_cross
          and rpt_norm_1 == rpt_norm_2,
          "两次运行 WAV 字节 MD5 完全一致，报告除覆盖标记外内容稳定")

    # ---------------- 汇总 ----------------
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print("=" * 60)
    print(f"验收汇总: {passed}/{total} 通过")
    if passed != total:
        print("未通过项:")
        for cid, ok, detail in results:
            if not ok:
                print(f"  - {cid}: {detail}")
        return 1
    print("全部验收通过。工作目录:", WORK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
