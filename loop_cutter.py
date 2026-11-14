# -*- coding: utf-8 -*-
"""loop_cutter.py — 按标注清单裁剪技能音效循环段并生成核对报告。

用法（Windows 命令行，无需网络与外部服务）：
    python loop_cutter.py manifest.json [--out out_dir]

清单 JSON 格式：
    {
      "entries": [
        {
          "id": "skill_fire_01",
          "wav": "samples/fire.wav",        // 相对清单所在目录或绝对路径
          "sample_rate": 44100,
          "channels": 1,
          "loop_start_frame": 10000,        // 循环起始帧（含）
          "loop_end_frame": 20000,          // 循环结束帧（不含），导出帧数 = end - start
          "preview_ms": 100                 // 试听拼接毫秒（首尾各取该长度）
        }
      ]
    }

输出（--out 目录，默认 <清单目录>/out）：
    <id>_loop.wav      裁剪后的循环段（采样率/声道/位深与源一致）
    <id>_preview.wav   首尾接缝试听片段（尾 N 帧 + 头 N 帧）
    report.json        逐条核对报告，按 id 升序
"""
import argparse
import json
import re
import shutil
import struct
import sys
import wave
from pathlib import Path

REQUIRED_FIELDS = ["id", "wav", "sample_rate", "channels",
                   "loop_start_frame", "loop_end_frame", "preview_ms"]

STATUS_OK = "ok"
STATUS_INVALID = "invalid"      # 循环区间标注无效
STATUS_ERROR = "error"          # 源文件损坏或参数不符 / 字段缺失
STATUS_DUPLICATE = "duplicate"  # id 冲突（保留先出现的一条）


def decode_frame(raw, sampwidth, ch):
    """从原始字节中解码第 ch 声道一个样本为有符号整数。"""
    off = ch * sampwidth
    b = raw[off:off + sampwidth]
    if sampwidth == 1:
        return b[0] - 128  # 8-bit PCM 为无符号
    if sampwidth == 2:
        return struct.unpack("<h", b)[0]
    if sampwidth == 3:
        v = int.from_bytes(b, "little", signed=False)
        return v - (1 << 24) if v & 0x800000 else v
    if sampwidth == 4:
        return struct.unpack("<i", b)[0]
    raise ValueError(f"不支持的位深字节数: {sampwidth}")


def safe_name(id_str):
    return re.sub(r"[^0-9A-Za-z_\-]", "_", str(id_str))


def make_entry(entry_id):
    return {
        "id": entry_id,
        "status": None,
        "jump": None,          # 接缝跳变量（首尾相邻样本差绝对值峰值）
        "loop_wav": None,      # 循环段导出路径
        "preview_wav": None,   # 试听片段导出路径
        "loop_frames": None,   # 循环段帧数
        "preview_frames": None,  # 试听片段总帧数（尾 N + 头 N）
        "reason": None,        # 状态说明 / 失败原因
        "notes": [],           # 附加说明（如试听长度被截取）
    }


def process_entry(idx, spec, manifest_dir, out_dir):
    """处理单条清单，返回报告条目（不写报告文件）。"""
    # 字段校验：缺字段时给出条目序号与字段名，不崩溃
    missing = [f for f in REQUIRED_FIELDS if f not in spec]
    entry_id = spec.get("id", f"<第{idx}条无id>")
    rec = make_entry(entry_id)
    if missing:
        rec["status"] = STATUS_ERROR
        rec["reason"] = (f"清单第 {idx} 条缺少必需字段: "
                         + ", ".join(missing))
        return rec

    start = spec["loop_start_frame"]
    end = spec["loop_end_frame"]
    if not isinstance(start, int) or not isinstance(end, int) \
            or isinstance(start, bool) or isinstance(end, bool):
        rec["status"] = STATUS_ERROR
        rec["reason"] = f"清单第 {idx} 条 loop_start_frame/loop_end_frame 必须为整数"
        return rec

    wav_path = Path(spec["wav"])
    if not wav_path.is_absolute():
        wav_path = manifest_dir / wav_path

    # 读取源 WAV
    try:
        with wave.open(str(wav_path), "rb") as w:
            nch, sw, fr, nframes = (w.getnchannels(), w.getsampwidth(),
                                    w.getframerate(), w.getnframes())
            raw = w.readframes(nframes)
    except Exception as e:  # 损坏或不是 WAV
        rec["status"] = STATUS_ERROR
        rec["reason"] = f"源 WAV 无法读取（损坏或格式不支持）: {wav_path} ({e})"
        return rec

    # 声明参数与实际不符 → 错误清单
    if fr != spec["sample_rate"] or nch != spec["channels"]:
        rec["status"] = STATUS_ERROR
        rec["reason"] = (f"声明参数与实际不符: 声明 {spec['sample_rate']}Hz/"
                         f"{spec['channels']}ch，实际 {fr}Hz/{nch}ch")
        return rec

    # 循环区间有效性：结束帧不大于起始帧、或超出实际帧数 → 无效
    if end <= start:
        rec["status"] = STATUS_INVALID
        rec["reason"] = f"循环结束帧({end})不大于起始帧({start})，标注无效"
        return rec
    if end > nframes:
        rec["status"] = STATUS_INVALID
        rec["reason"] = f"循环结束帧({end})超出音频实际帧数({nframes})，标注无效"
        return rec
    if start < 0:
        rec["status"] = STATUS_INVALID
        rec["reason"] = f"循环起始帧({start})为负，标注无效"
        return rec

    frame_bytes = nch * sw
    loop_frames = end - start
    loop_raw = raw[start * frame_bytes: end * frame_bytes]

    # 接缝跳变量：循环末帧与首帧对应样本差绝对值，取各声道峰值
    first_frame = loop_raw[:frame_bytes]
    last_frame = loop_raw[-frame_bytes:]
    jump = max(abs(decode_frame(first_frame, sw, c)
                   - decode_frame(last_frame, sw, c)) for c in range(nch))

    # 试听拼接长度：毫秒 → 帧，不超过循环段长度一半，越界截取并记录
    want = round(spec["preview_ms"] * fr / 1000.0)
    half = loop_frames // 2
    n = want
    if n > half:
        n = half
        rec["notes"].append(
            f"试听拼接长度被截取: 请求 {spec['preview_ms']}ms={want} 帧，"
            f"超过循环段一半({half} 帧)，已截为 {n} 帧")
    if n <= 0:
        rec["status"] = STATUS_INVALID
        rec["reason"] = (f"试听拼接长度为 0 帧（preview_ms="
                         f"{spec['preview_ms']}，循环段 {loop_frames} 帧），无法生成试听")
        return rec

    tail = loop_raw[-n * frame_bytes:]
    head = loop_raw[: n * frame_bytes]

    name = safe_name(entry_id)
    loop_out = out_dir / f"{name}_loop.wav"
    prev_out = out_dir / f"{name}_preview.wav"
    for path, data in ((loop_out, loop_raw), (prev_out, tail + head)):
        with wave.open(str(path), "wb") as w:
            w.setnchannels(nch)
            w.setsampwidth(sw)   # 位深与源一致
            w.setframerate(fr)   # 采样率与源一致
            w.writeframes(data)

    rec.update({
        "status": STATUS_OK,
        "jump": jump,
        "loop_wav": str(loop_out),
        "preview_wav": str(prev_out),
        "loop_frames": loop_frames,
        "preview_frames": 2 * n,
        "reason": None,
    })
    return rec


def run(manifest_path, out_dir):
    manifest_path = Path(manifest_path)
    manifest_dir = manifest_path.resolve().parent

    # 清单解析失败 → 可读说明，正常报错退出
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[失败] 清单无法解析: {manifest_path}: {e}", file=sys.stderr)
        return 2
    entries = manifest.get("entries") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        print("[失败] 清单缺少 entries 数组或格式不正确", file=sys.stderr)
        return 2

    # 输出目录为受管目录：运行前整体清空重建，保证重复运行无残留
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    if not entries:
        print("清单为空：没有任何条目需要处理，未生成导出文件。")

    records, seen_ids = [], set()
    for idx, spec in enumerate(entries):
        if not isinstance(spec, dict):
            rec = make_entry(f"<第{idx}条>")
            rec["status"] = STATUS_ERROR
            rec["reason"] = f"清单第 {idx} 条不是对象，无法解析"
            records.append(rec)
            continue
        # 同一 id 重复出现：导出前拦截，标出冲突，只保留先出现的一条
        dup_id = spec.get("id") if isinstance(spec, dict) else None
        if dup_id is not None and dup_id in seen_ids:
            rec = make_entry(dup_id)
            rec["status"] = STATUS_DUPLICATE
            rec["reason"] = f"id 冲突: '{dup_id}' 在清单中重复出现，仅保留先出现的一条"
            records.append(rec)
            continue
        rec = process_entry(idx, spec, manifest_dir, out_dir)
        seen_ids.add(rec["id"])
        records.append(rec)

    # 报告按 id 升序
    records.sort(key=lambda r: str(r["id"]))
    report_path = out_dir / "report.json"
    report_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    # 控制台摘要
    ok = [r for r in records if r["status"] == STATUS_OK]
    invalid = [r for r in records if r["status"] == STATUS_INVALID]
    errors = [r for r in records if r["status"] == STATUS_ERROR]
    dups = [r for r in records if r["status"] == STATUS_DUPLICATE]

    print(f"\n处理完成: 有效 {len(ok)} 条, 无效标注 {len(invalid)} 条, "
          f"错误 {len(errors)} 条, id 冲突 {len(dups)} 条")
    if invalid:
        print("\n-- 无效标注（单独列出，无导出文件）--")
        for r in invalid:
            print(f"  [{r['id']}] {r['reason']}")
    if errors:
        print("\n-- 错误清单（源文件或清单问题）--")
        for r in errors:
            print(f"  [{r['id']}] {r['reason']}")
    if dups:
        print("\n-- id 冲突 --")
        for r in dups:
            print(f"  [{r['id']}] {r['reason']}")
    if ok:
        print("\n-- 有效循环接缝跳变量（从小到大，并列按 id 稳定排序）--")
        for r in sorted(ok, key=lambda r: (r["jump"], str(r["id"]))):
            note = f"  （{'; '.join(r['notes'])}）" if r["notes"] else ""
            print(f"  [{r['id']}] 跳变量={r['jump']}  "
                  f"循环 {r['loop_frames']} 帧  试听 {r['preview_frames']} 帧{note}")
    if not ok and not invalid and not errors and not dups:
        print("说明: 清单为空内容，导出目录无 WAV 文件。")
    elif not ok:
        print("说明: 没有有效循环可导出，导出目录无 WAV 文件。")
    print(f"\n核对报告: {report_path}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="按标注清单裁剪循环段并生成核对报告")
    ap.add_argument("manifest", help="标注清单 JSON 路径")
    ap.add_argument("--out", default=None,
                    help="输出目录（默认 <清单所在目录>/out）")
    args = ap.parse_args(argv)
    out_dir = (Path(args.out) if args.out
               else Path(args.manifest).resolve().parent / "out")
    return run(args.manifest, out_dir)


if __name__ == "__main__":
    sys.exit(main())
