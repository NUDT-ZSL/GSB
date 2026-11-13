"""端到端验收脚本：逐条核对任务的 8 项验收标准（断言失败即非零退出）。

用法:  python -I verify.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import silence_marker  # noqa: E402
from make_samples import generate  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(ROOT, "verify_workspace")
INPUT = os.path.join(WORK, "input")
OUTPUT = os.path.join(WORK, "output")

_passed = 0


def check(title: str, condition: bool, detail: str = "") -> None:
    global _passed
    if not condition:
        raise AssertionError(f"{title} 失败 —— {detail}")
    _passed += 1
    print(f"  [PASS] {title}")


def read_utf8_json(path: str):
    with open(path, "r", encoding="utf-8") as f:  # 显式 UTF-8 读取
        return json.load(f)


def main() -> int:
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    generate(INPUT)
    os.makedirs(OUTPUT, exist_ok=True)

    print("\n== 场景 1: 正常配置 config.json ==")
    report = silence_marker.process_directory(
        INPUT, os.path.join(INPUT, "config.json"), OUTPUT
    )
    names = [e["file"] for e in report["files"]]

    # 验收 1：字典序（a.wav 在 b.wav 前；bad.wav / stereo_sample.wav 也参与排序）
    check("1. 字典序处理(a.wav 在 b.wav 前)", names.index("a.wav") < names.index("b.wav"), str(names))
    check("1b. 仅扫描当前目录、不递归", all(os.sep not in n for n in names), str(names))

    # 验收 2：正常配置生效
    check("2. 正常配置被采用", report["config"]["threshold_db"] == -40.0
          and report["config"]["min_silence_ms"] == 500.0)

    entries = {e["file"]: e for e in report["files"]}

    # 验收 3：b.wav 前 1 秒静音 -> 出现从 0ms 起算的区间
    b_marks = entries["b.wav"]["marks"]
    check("3. b.wav 存在从 0ms 起算的静音区间",
          any(m["start_ms"] == 0 for m in b_marks), str(b_marks))
    check("3b. b.wav 静音区间终点为 1000ms",
          any(m["start_ms"] == 0 and m["end_ms"] == 1000 for m in b_marks), str(b_marks))

    # a.wav: 80ms 短间隙应被 500ms 门槛过滤，仅保留首尾两段长静音
    a_marks = entries["a.wav"]["marks"]
    check("3c. a.wav 短于 500ms 的间隙被过滤", len(a_marks) == 2, str(a_marks))
    check("3d. a.wav 区间为 [0,1000] 与 [2000,3000]",
          a_marks == [{"start_ms": 0, "end_ms": 1000}, {"start_ms": 2000, "end_ms": 3000}],
          str(a_marks))

    # 验收 5：标记文件存在、字段为整数毫秒
    a_mark_path = os.path.join(OUTPUT, "a.wav.marks.json")
    check("5. a.wav.marks.json 存在", os.path.isfile(a_mark_path))
    a_mark_file = read_utf8_json(a_mark_path)
    check("5b. 标记文件含 marks 数组", isinstance(a_mark_file.get("marks"), list))
    ints_ok = all(
        isinstance(m["start_ms"], int) and isinstance(m["end_ms"], int)
        and not isinstance(m["start_ms"], bool)
        for m in a_mark_file["marks"]
    )
    check("5c. start_ms/end_ms 均为整数毫秒", ints_ok, str(a_mark_file["marks"]))

    # 验收 6：报告字段 marks / status / error / total_marks 齐全
    check("6. 报告含 total_marks", "total_marks" in report and report["total_marks"] == 3)
    fields_ok = all({"file", "marks", "status", "error"} <= set(e) for e in report["files"])
    check("6b. 每个条目 status/error/marks 字段齐全", fields_ok)

    # 验收 7：损坏文件与格式不符文件被跳过并记录原因
    check("7. bad.wav 记录解析失败",
          entries["bad.wav"]["status"] == "error"
          and "WAV" in entries["bad.wav"]["error"], entries["bad.wav"]["error"])
    check("7b. 立体声文件记录格式不符",
          entries["stereo_sample.wav"]["status"] == "error"
          and "声道" in entries["stereo_sample.wav"]["error"],
          entries["stereo_sample.wav"]["error"])
    check("7c. 错误文件不生成标记文件",
          not os.path.exists(os.path.join(OUTPUT, "bad.wav.marks.json"))
          and not os.path.exists(os.path.join(OUTPUT, "stereo_sample.wav.marks.json")))
    check("7d. 合法文件标记仍完整生成",
          entries["a.wav"]["status"] == "ok" and entries["b.wav"]["status"] == "ok"
          and len(entries["a.wav"]["marks"]) == 2 and len(entries["b.wav"]["marks"]) == 1)

    # 验收 8：输出可被 UTF-8 解析、为合法 JSON 对象、含 marks 数组
    for fname in ("a.wav.marks.json", "b.wav.marks.json", "report.json"):
        obj = read_utf8_json(os.path.join(OUTPUT, fname))
        check(f"8. {fname} 为 UTF-8 合法 JSON", isinstance(obj, dict))
    check("8b. 标记文件含 marks 数组",
          isinstance(read_utf8_json(os.path.join(OUTPUT, "b.wav.marks.json"))["marks"], list))

    print("\n== 场景 2: 缺少 threshold_db 字段（验收 2 默认值回退）==")
    report2 = silence_marker.process_directory(
        INPUT, os.path.join(INPUT, "config_missing_threshold.json"),
        os.path.join(WORK, "out_missing"),
    )
    check("2c. 缺少阈值字段时回退 -40dB", report2["config"]["threshold_db"] == -40.0)
    check("2d. 报告注明采用了默认阈值",
          any("默认阈值" in n for n in report2["config"]["notes"]),
          str(report2["config"]["notes"]))

    print("\n== 场景 3: 空配置 / 无配置文件 ==")
    report3 = silence_marker.process_directory(
        INPUT, os.path.join(INPUT, "config_empty.json"),
        os.path.join(WORK, "out_empty"),
    )
    check("2e. 空 JSON 对象回退全部默认值",
          report3["config"]["threshold_db"] == -40.0
          and report3["config"]["min_silence_ms"] == 500.0)
    report4 = silence_marker.process_directory(
        INPUT, os.path.join(INPUT, "not_exist.json"),
        os.path.join(WORK, "out_none"),
    )
    check("2f. 配置文件不存在时回退默认值且不崩溃",
          report4["config"]["threshold_db"] == -40.0
          and any("配置文件不存在" in n for n in report4["config"]["notes"]))

    print("\n== 场景 4: 阈值边界 0dB 与 -96dB（验收 4，不崩溃且有输出）==")
    for db, out_name in ((0.0, "out_0db"), (-96.0, "out_96db")):
        # 边界阈值下完整跑一遍批处理，确认程序正常退出、报告正常生成
        r = silence_marker.process_directory(
            INPUT, None, os.path.join(WORK, out_name)
        )
        check(f"4e. {db:g}dB 批处理正常完成", r["failed_files"] == 2 and r["success_files"] == 2)
        samples, rate = silence_marker.read_mono_pcm16_wav(os.path.join(INPUT, "b.wav"))
        marks = silence_marker.find_silence_marks(samples, rate, db, 500.0)
        check(f"4. 阈值 {db:g}dB 正常输出标记", isinstance(marks, list) and len(marks) >= 1, str(marks))
        check(f"4b. 阈值 {db:g}dB 幅度换算为有限非负值",
              silence_marker.db_to_amplitude(db) >= 0.0)

    # 0dB：幅度=32768，|样本|<32768 恒成立 -> 整文件都是静音，只有一个覆盖全段的区间
    samples_b, rate_b = silence_marker.read_mono_pcm16_wav(os.path.join(INPUT, "b.wav"))
    marks_0 = silence_marker.find_silence_marks(samples_b, rate_b, 0.0, 500.0)
    check("4c. 0dB 时整文件判为静音(单区间 0~2000ms)",
          marks_0 == [{"start_ms": 0, "end_ms": 2000}], str(marks_0))
    # -96dB：幅度约 0.51，仅零采样算静音 -> 只有前 1s
    marks_96 = silence_marker.find_silence_marks(samples_b, rate_b, -96.0, 500.0)
    check("4d. -96dB 时仅纯静音段命中(0~1000ms)",
          marks_96 == [{"start_ms": 0, "end_ms": 1000}], str(marks_96))

    print("\n== 场景 5: 非法配置字段（字符串/布尔/负数）==")
    bad_cfg = os.path.join(WORK, "bad_config.json")
    with open(bad_cfg, "w", encoding="utf-8") as f:
        json.dump({"threshold_db": "loud", "min_silence_ms": -5}, f)
    report5 = silence_marker.process_directory(
        INPUT, bad_cfg, os.path.join(WORK, "out_badcfg")
    )
    check("2g. 非法字段回退默认值",
          report5["config"]["threshold_db"] == -40.0
          and report5["config"]["min_silence_ms"] == 500.0
          and len(report5["config"]["notes"]) == 2)

    # 带 UTF-8 BOM 的配置（Windows 记事本 / PS5.1 Set-Content 常见产物）
    bom_cfg = os.path.join(WORK, "bom_config.json")
    with open(bom_cfg, "w", encoding="utf-8-sig") as f:
        json.dump({"threshold_db": -30, "min_silence_ms": 250}, f)
    report_bom = silence_marker.process_directory(
        INPUT, bom_cfg, os.path.join(WORK, "out_bom")
    )
    check("2h. 带 BOM 的 UTF-8 配置可正常解析",
          report_bom["config"]["threshold_db"] == -30.0
          and report_bom["config"]["min_silence_ms"] == 250.0
          and report_bom["config"]["notes"] == [])

    # 损坏的 JSON 文本：整体回退默认值并注明原因，不抛异常
    broken_cfg = os.path.join(WORK, "broken_config.json")
    with open(broken_cfg, "w", encoding="utf-8") as f:
        f.write("{ threshold_db: ")
    report_broken = silence_marker.process_directory(
        INPUT, broken_cfg, os.path.join(WORK, "out_broken")
    )
    check("2i. 损坏 JSON 回退默认值且不崩溃",
          report_broken["config"]["threshold_db"] == -40.0
          and any("无法解析" in n for n in report_broken["config"]["notes"]))

    print("\n== 场景 6: 毫秒向下取整（合成非整除采样率样本）==")
    import array as _array
    # 3000Hz 采样率：静音 1000 个采样 -> 333.333ms 应向下取整为 333
    fake = _array.array("h", [0] * 1000 + [10000] * 1000)
    marks_floor = silence_marker.find_silence_marks(fake, 3000, -40.0, 100.0)
    check("5d. 毫秒端点向下取整(333 而非 334)",
          marks_floor == [{"start_ms": 0, "end_ms": 333}], str(marks_floor))

    print(f"\n全部 {_passed} 项断言通过 [ALL PASS]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
