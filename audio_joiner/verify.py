# -*- coding: utf-8 -*-
"""
验收脚本：覆盖任务 14 条验收点（纯标准库，确定性本地验证）

做法：
  1. 在系统临时目录新建一次性工作区，调用 make_samples 生成样本与场景
  2. 以子进程方式运行 joiner.py（python -I），逐条比对退出码 / 文件 /
     报告内容 / WAV 头字段 / 样本级数据
  3. 全部通过退出码 0，任一失败退出码 1 并打印差异

用法：
  python -I verify.py
"""

import array
import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
JOINER = os.path.join(HERE, "joiner.py")
MAKE = os.path.join(HERE, "make_samples.py")
RATE = 44100

sys.path.insert(0, HERE)
import make_samples  # noqa: E402  仅复用确定性样本生成函数

results = []


def check(point, title, ok, detail=""):
    results.append((point, title, bool(ok), detail))
    flag = "PASS" if ok else "FAIL"
    line = "[%s] 验收%s %s" % (flag, point, title)
    if detail:
        line += " —— " + detail
    print(line)


def run_joiner(cwd, *extra_args):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, "-I", JOINER, *extra_args]
    return subprocess.run(cmd, cwd=cwd, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def read_wav(path):
    with wave.open(path, "rb") as wf:
        params = {
            "channels": wf.getnchannels(),
            "sampwidth": wf.getsampwidth(),
            "rate": wf.getframerate(),
            "frames": wf.getnframes(),
        }
        samples = array.array("h")
        samples.frombytes(wf.readframes(wf.getnframes()))
    return params, samples


def source_samples(ws, name):
    _, s = read_wav(os.path.join(ws, "wav", name))
    return s


def load_report(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    ws_root = tempfile.mkdtemp(prefix="audio_joiner_verify_")
    try:
        ws = os.path.join(ws_root, "workspace")
        make_samples.build(ws)

        # ---------- 验收 1：默认场景生成 output.wav 与 report.json ----------
        r = run_joiner(ws)
        out_wav = os.path.join(ws, "output.wav")
        out_rep = os.path.join(ws, "report.json")
        rep = load_report(out_rep)
        check(1, "默认场景生成 WAV 与报告，片段数一致",
              r.returncode == 0 and os.path.exists(out_wav)
              and os.path.exists(out_rep) and len(rep["clips"]) == 2,
              "报告片段数=%d" % len(rep["clips"]))

        # ---------- 验收 2：48kHz 立体声片段跳过并写明格式不匹配 ----------
        d2 = os.path.join(ws_root, "t2"); os.makedirs(d2)
        r2 = run_joiner(ws, os.path.join(ws, "edit_bad_format.json"),
                        os.path.join(d2, "o.wav"),
                        os.path.join(d2, "r.json"))
        rep2 = load_report(os.path.join(d2, "r.json"))
        statuses2 = [c["status"] for c in rep2["clips"]]
        bad_clip = rep2["clips"][1]
        check(2, "格式不符片段跳过且写明原因，其余正常合成",
              r2.returncode == 0 and os.path.exists(os.path.join(d2, "o.wav"))
              and statuses2 == ["成功", "跳过", "成功"]
              and "格式不匹配" in bad_clip["reason"]
              and "48000" in bad_clip["reason"] and "声道" in bad_clip["reason"],
              bad_clip["reason"])

        # ---------- 验收 3：缺失文件跳过，报告含路径与原因 ----------
        d3 = os.path.join(ws_root, "t3"); os.makedirs(d3)
        r3 = run_joiner(ws, os.path.join(ws, "edit_missing.json"),
                        os.path.join(d3, "o.wav"),
                        os.path.join(d3, "r.json"))
        rep3 = load_report(os.path.join(d3, "r.json"))
        miss = rep3["clips"][1]
        check(3, "缺失片段跳过，报告含该路径及原因，其余继续合成",
              r3.returncode == 0 and miss["status"] == "跳过"
              and "not_exist.wav" in miss["reason"]
              and "不存在" in miss["reason"]
              and [c["status"] for c in rep3["clips"]] == ["成功", "跳过", "成功"],
              miss["reason"])

        # ---------- 验收 4：淡化 0.8s 超出承受范围 -> 不淡化 ----------
        d4 = os.path.join(ws_root, "t4"); os.makedirs(d4)
        r4 = run_joiner(ws, os.path.join(ws, "edit_fade_too_big.json"),
                        os.path.join(d4, "o.wav"),
                        os.path.join(d4, "r.json"))
        rep4 = load_report(os.path.join(d4, "r.json"))
        _, out4 = read_wav(os.path.join(d4, "o.wav"))
        a = source_samples(ws, "clip_a.wav")
        b = source_samples(ws, "clip_b.wav")
        # 无淡化时重叠区应为两段样本原样相加（AMP=12000，相加不超过 16-bit）
        overlap_sum_ok = all(
            out4[35280 + j] == a[35280 + j] + b[j] for j in range(8820))
        note4 = rep4["clips"][1]["note"]
        check(4, "淡化超出范围时不做淡化且报告可识别",
              r4.returncode == 0 and overlap_sum_ok
              and rep4["clips"][1]["applied_fade_seconds"] == 0.0
              and "未做淡化" in note4, note4)

        # ---------- 验收 5：淡化 0.2s -> 1.8s，平滑且非淡化区一致 ----------
        _, out5 = read_wav(out_wav)
        pre_ok = all(out5[i] == a[i] for i in range(35280))          # 非重叠前段
        post_ok = all(out5[44100 + i] == b[8820 + i] for i in range(35280))
        max_delta_overlap = max(
            abs(out5[i] - out5[i - 1]) for i in range(35281, 44100))
        max_delta_plain = max(abs(a[i] - a[i - 1]) for i in range(1, 44100))
        # 边界不引入额外跳变：实际跳变与源信号在该处的自然步长之差
        # 只允许淡化斜率残差（AMP/(f+1)≈2）量级的误差
        start_jump = abs(out5[35280] - out5[35279])
        start_natural = abs(a[35280] - a[35279])
        end_jump = abs(out5[44100] - out5[44099])
        end_natural = abs(b[8820] - b[8819])
        smooth_ok = max_delta_overlap <= max(2000, max_delta_plain * 4) \
            and abs(start_jump - start_natural) <= 5 \
            and abs(end_jump - end_natural) <= 5
        faded_differs = any(out5[35280 + j] != a[35280 + j] + b[j]
                            for j in range(8820))  # 确实应用了淡化而非原样叠加
        check(5, "淡化0.2s合成1.8s、重叠区平滑、非淡化区与源一致",
              len(out5) == 79380 and pre_ok and post_ok and smooth_ok
              and faded_differs,
              "帧数=%d 重叠区最大相邻差=%d 普通区=%d 起点边界跳变=%d(自然%d) "
              "终点边界跳变=%d(自然%d)"
              % (len(out5), max_delta_overlap, max_delta_plain,
                 start_jump, start_natural, end_jump, end_natural))

        # ---------- 验收 6：时间线非法整体拒绝，无任何输出 ----------
        d6 = os.path.join(ws_root, "t6"); os.makedirs(d6)
        r6 = run_joiner(d6, os.path.join(ws, "edit_bad_timeline.json"),
                        os.path.join(d6, "o.wav"),
                        os.path.join(d6, "r.json"))
        check(6, "起始秒非严格递增时报错退出且不生成文件",
              r6.returncode != 0
              and not os.path.exists(os.path.join(d6, "o.wav"))
              and not os.path.exists(os.path.join(d6, "r.json"))
              and "严格递增" in r6.stderr.decode("utf-8", "replace"),
              "退出码=%d" % r6.returncode)

        # ---------- 验收 7：全部跳过 -> 无 WAV，报告标明无可用片段 ----------
        d7 = os.path.join(ws_root, "t7"); os.makedirs(d7)
        r7 = run_joiner(ws, os.path.join(ws, "edit_all_missing.json"),
                        os.path.join(d7, "o.wav"),
                        os.path.join(d7, "r.json"))
        rep7 = load_report(os.path.join(d7, "r.json"))
        check(7, "全部跳过时无 WAV，报告标明无可用片段并列出原因",
              r7.returncode == 0
              and not os.path.exists(os.path.join(d7, "o.wav"))
              and rep7["no_usable_clip"] is True
              and all(c["status"] == "跳过" and c["reason"] for c in rep7["clips"]),
              "片段数=%d 均含原因" % len(rep7["clips"]))

        # ---------- 验收 8：CLI 缺省值与显式三参数 ----------
        d8 = os.path.join(ws_root, "t8"); os.makedirs(d8)
        shutil.copy(os.path.join(ws, "edit.json"), os.path.join(d8, "edit.json"))
        shutil.copytree(os.path.join(ws, "wav"), os.path.join(d8, "wav"))
        r8a = run_joiner(d8)   # 不带参数
        r8b = run_joiner(ws, os.path.join(ws, "edit.json"),
                         os.path.join(d8, "custom.wav"),
                         os.path.join(d8, "custom_report.json"))
        check(8, "缺省参数写默认文件，带参数写指定路径",
              r8a.returncode == 0
              and os.path.exists(os.path.join(d8, "output.wav"))
              and os.path.exists(os.path.join(d8, "report.json"))
              and r8b.returncode == 0
              and os.path.exists(os.path.join(d8, "custom.wav"))
              and os.path.exists(os.path.join(d8, "custom_report.json")))

        # ---------- 验收 9：报告合法、状态枚举、时长三位小数字符串 ----------
        import re
        dur = rep["total_duration_seconds"]
        legal_status = all(c["status"] in ("成功", "跳过") for c in rep["clips"])
        check(9, "报告为合法JSON，状态仅成功/跳过，总时长三位小数字符串",
              isinstance(dur, str) and re.fullmatch(r"\d+\.\d{3}", dur)
              and dur == "1.800" and legal_status,
              "total_duration_seconds=%r" % dur)

        # ---------- 验收 10：中间片段缺失，剩余片段保持原时间线位置 ----------
        d10 = os.path.join(ws_root, "t10"); os.makedirs(d10)
        r10 = run_joiner(ws, os.path.join(ws, "edit_middle_missing.json"),
                         os.path.join(d10, "o.wav"),
                         os.path.join(d10, "r.json"))
        rep10 = load_report(os.path.join(d10, "r.json"))
        _, out10 = read_wav(os.path.join(d10, "o.wav"))
        block_a = all(out10[i] == a[i] for i in range(44100))
        block_b = all(out10[88200 + i] == b[i] for i in range(44100))
        # 缓冲只由剩余片段的样本写入构成，代码不主动补静音
        only_remaining = rep10["clips"][1]["status"] == "跳过" \
            and "not_exist.wav" in rep10["clips"][1]["reason"]
        check(10, "中间缺失时剩余片段按原起始秒定位且报告标明缺失段",
              r10.returncode == 0 and block_a and block_b
              and len(out10) == 132300 and only_remaining,
              "总帧数=%d（=最后片段起点2.0s+1.0s），缺失段已在报告标注"
              % len(out10))

        # ---------- 验收 11：缺字段/缺顶层列表 -> 报错终止无输出 ----------
        d11 = os.path.join(ws_root, "t11"); os.makedirs(d11)
        r11a = run_joiner(d11, os.path.join(ws, "edit_missing_field.json"),
                          os.path.join(d11, "o.wav"),
                          os.path.join(d11, "r.json"))
        r11b = run_joiner(d11, os.path.join(ws, "edit_no_clips.json"),
                          os.path.join(d11, "o2.wav"),
                          os.path.join(d11, "r2.json"))
        no_files = not any(f.startswith(("o", "r")) for f in os.listdir(d11))
        check(11, "缺字段或顶层缺列表时报具体错误且不生成输出",
              r11a.returncode != 0 and r11b.returncode != 0 and no_files
              and "缺少必需字段 'start'" in r11a.stderr.decode("utf-8", "replace")
              and "clips" in r11b.stderr.decode("utf-8", "replace"))

        # ---------- 验收 12：控制台逐片段结果 + 最终输出路径 ----------
        d12 = os.path.join(ws_root, "t12"); os.makedirs(d12)
        r12 = run_joiner(ws, os.path.join(ws, "edit_mixed.json"),
                         os.path.join(d12, "o.wav"),
                         os.path.join(d12, "r.json"))
        stdout12 = r12.stdout.decode("utf-8", "replace")
        per_clip = sum(1 for line in stdout12.splitlines()
                       if line.startswith(("[成功]", "[跳过]", "[警告]")))
        check(12, "控制台逐片段输出结果并显示最终输出路径",
              per_clip >= 4 and "WAV 输出路径" in stdout12
              and "报告输出路径" in stdout12
              and "[跳过]" in stdout12 and "[警告]" in stdout12,
              "结果行=%d 行" % per_clip)

        # ---------- 验收 13：输出 WAV 规格与报告时长一致 ----------
        params, samples13 = read_wav(out_wav)
        expected_frames = int(round(float(rep["total_duration_seconds"]) * RATE))
        check(13, "输出为单声道16-bit/44100Hz，样本数与报告时长一致",
              params == {"channels": 1, "sampwidth": 2, "rate": 44100,
                         "frames": 79380}
              and params["frames"] == len(samples13) == expected_frames
              and params["frames"] == rep["total_frames"],
              str(params))

        # ---------- 验收 14：同起点非首片段跳过；首片段非0起点不补静音 ----------
        d14a = os.path.join(ws_root, "t14a"); os.makedirs(d14a)
        r14a = run_joiner(ws, os.path.join(ws, "edit_same_start.json"),
                          os.path.join(d14a, "o.wav"),
                          os.path.join(d14a, "r.json"))
        rep14a = load_report(os.path.join(d14a, "r.json"))
        _, out14a = read_wav(os.path.join(d14a, "o.wav"))
        same_start_ok = rep14a["clips"][1]["status"] == "跳过" \
            and "相同" in rep14a["clips"][1]["reason"] \
            and len(out14a) == 44100 and all(out14a[i] == a[i] for i in range(44100))

        d14b = os.path.join(ws_root, "t14b"); os.makedirs(d14b)
        r14b = run_joiner(ws, os.path.join(ws, "edit_first_offset.json"),
                          os.path.join(d14b, "o.wav"),
                          os.path.join(d14b, "r.json"))
        rep14b = load_report(os.path.join(d14b, "r.json"))
        _, out14b = read_wav(os.path.join(d14b, "o.wav"))
        offset_ok = len(out14b) == 79380 and out14b[0] == a[0] \
            and rep14b["total_duration_seconds"] == "1.800"
        check(14, "同起点片段跳过；首片段非0起点不补静音、从该起点计时",
              r14a.returncode == 0 and r14b.returncode == 0
              and same_start_ok and offset_ok,
              "同起点输出帧数=%d，偏移起点输出帧数=%d 首样本=%d"
              % (len(out14a), len(out14b), out14b[0]))

        # ---------------- 汇总 ----------------
        passed = sum(1 for _, _, ok, _ in results if ok)
        print("\n================ 验收汇总 ================")
        print("通过 %d/%d" % (passed, len(results)))
        if passed != len(results):
            print("存在失败项，请检查上方 FAIL 行")
            return 1
        print("全部验收点通过")
        return 0
    finally:
        shutil.rmtree(ws_root, ignore_errors=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
