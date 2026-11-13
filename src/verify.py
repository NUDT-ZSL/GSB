# -*- coding: utf-8 -*-
"""ad_inserter 的端到端验收脚本（对应任务 8 项验收点）。

运行方式:
    python -I verify.py            # 自动生成样本并执行全部检查
    python -I verify.py --keep     # 检查结束后保留临时目录便于人工查看

检查项:
    1. 立体声 / 采样率不一致 -> 明确报错且输出目录无新文件
    2. 含负值/越界/非整数次数的计划 -> 报告标记无效项，有效插入仍完成
    3. 已知波形逐采样点核对：插入点前后与预期连续波形一致、无静音填充/重叠
    4. 报告条目数量/顺序/起止索引/编号与实际插入位置逐一相符
    5. 剩余空间不足时只放完整广告，输出不超过原节目时长，实际/未插入次数正确
    6. 输出 WAV 头部与输入一致，报告总采样点数与文件实际相符
    7. 含空格路径 + 标准输出摘要含插入段数与输出路径
    8. 缺字段/时间非数字 -> 报错、无新文件、已有同名文件保持原样
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import wave
from typing import Any, Dict, List, Optional, Tuple

# 子进程已各自 reconfigure 到 UTF-8；父进程自身也切到 UTF-8 避免 GBK 控制台乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "ad_inserter.py")
MAKE_SAMPLES = os.path.join(HERE, "make_samples.py")

PASS = "PASS"
FAIL = "FAIL"
results: List[Tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    mark = "[PASS]" if ok else "[FAIL]"
    print(f"{mark} {name}" + (f" -- {detail}" if detail else ""))


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_wav(path: str) -> Tuple[wave._wave_params, array.array]:
    with wave.open(path, "rb") as wf:
        params = wf.getparams()
        raw = wf.readframes(params.nframes)
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()
    return params, samples


def run_tool(root: str, outdir: str, program: str, ad: str, plan: str) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable, "-I", SRC,
        "--program", program,
        "--ad", ad,
        "--plan", plan,
        "--outdir", outdir,
    ]
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=root)


# --------------------------------------------------------------------------- #
# 独立参考实现：用与工具不同的写法（直接切片拼接 + 时间轴位移）推导预期输出
# --------------------------------------------------------------------------- #
def reference_build(
    program: array.array,
    ad: array.array,
    plan: List[Dict[str, Any]],
    rate: int,
) -> Tuple[array.array, List[Dict[str, int]], int, List[Dict[str, Any]]]:
    """返回 (预期输出采样, 每段广告的预期起止(含), 截断采样数, 无效项)。"""
    n = len(program)
    ad_len = len(ad)
    valid, invalid = [], []
    for idx, item in enumerate(plan):
        t, r = item["time_sec"], item["repeats"]
        if not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(t) or t < 0 or t > n / rate:
            invalid.append(idx)
        elif not isinstance(r, int) or isinstance(r, bool) or r <= 0:
            invalid.append(idx)
        else:
            valid.append((idx, max(0, min(int(round(t * rate)), n)), r))

    # 参考调度：按位置排序，维护 (原始节目区间, 广告) 的块序列
    blocks: List[Tuple[str, int, int, Optional[Tuple[int, int]]]] = [("p", 0, n, None)]
    scheduled: Dict[int, Tuple[int, List[Tuple[int, int]]]] = {}
    for plan_idx, at, repeats in sorted(valid, key=lambda x: (x[1], x[0])):
        pos = 0
        target = -1
        for bi, (kind, start, length, _o) in enumerate(blocks):
            if kind == "p" and pos <= at <= pos + length:
                target = bi
                tstart, tend = pos, pos + length
                break
            pos += length
        remaining = n - tstart
        actual = min(repeats, max(0, remaining // ad_len))
        kind, pstart, plen, _o = blocks[target]
        off = at - tstart
        new = blocks[:target]
        if off:
            new.append(("p", pstart, off, None))
        copies = []
        for c in range(actual):
            owner = (plan_idx, c)
            new.append(("a", 0, ad_len, owner))
            copies.append(owner)
        if plen - off:
            new.append(("p", pstart + off, plen - off, None))
        new.extend(blocks[target + 1:])
        blocks = new
        scheduled[plan_idx] = (actual, copies)

    out = array.array("h")
    ranges: Dict[Tuple[int, int], Tuple[int, int]] = {}
    pos = 0
    kept = 0
    for kind, start, length, owner in blocks:
        if pos >= n:
            break
        take = min(length, n - pos)
        if kind == "p":
            out.extend(program[start:start + take])
            kept += take
        else:
            ranges[owner] = (pos, pos + take - 1)
            out.extend(ad[start:start + take])
        pos += take

    # 报告项按计划顺序、编号从 1
    details = []
    number = 0
    for plan_idx, at, repeats in valid:
        actual, copies = scheduled[plan_idx]
        for c in range(actual):
            number += 1
            s, e = ranges[(plan_idx, c)]
            details.append({"ad_number": number, "start": s, "end": e})
    return out, details, n - kept, invalid


def expected_program_sample(i: int, rate: int = 8000) -> int:
    return max(-32768, min(32767, round(8000 * math.sin(2 * math.pi * 220 * i / rate))))


def expected_ad_sample(i: int, rate: int = 8000) -> int:
    return max(-32768, min(32767, round(12000 * math.sin(2 * math.pi * 880 * i / rate)) + (i % 97 - 48)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留临时工作目录")
    args = parser.parse_args()

    root = tempfile.mkdtemp(prefix="ad_inserter_verify_")
    print("工作目录:", root, "\n")

    # 1) 生成确定性样本
    gen = subprocess.run(
        [sys.executable, "-I", MAKE_SAMPLES, "--root", root],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    check("样本生成脚本退出码为 0", gen.returncode == 0, gen.stderr.strip())
    input_dir = os.path.join(root, "input")
    plans_dir = os.path.join(root, "plans")
    keep_dir = os.path.join(root, "output_keep")

    program_path = os.path.join(input_dir, "program.wav")
    ad_path = os.path.join(input_dir, "ad.wav")
    _, program_samples = read_wav(program_path)
    rate, program_len, ad_len = 8000, len(program_samples), 16000

    # 校验确定性样本本身符合已知公式（抽查首尾与中间点）
    formula_ok = all(
        program_samples[i] == expected_program_sample(i)
        for i in (0, 1, 2, 100, 7999, 80000, program_len - 1)
    )
    check("确定性节目样本符合已知波形公式", formula_ok)

    # ------------------------------------------------------------------ #
    # 验收 1：立体声 / 采样率不一致 -> 报错且无新文件
    # ------------------------------------------------------------------ #
    for bad_wav, expect_word in [
        (os.path.join(input_dir, "stereo_program.wav"), "单声道"),
        (os.path.join(input_dir, "rate16k_ad.wav"), "采样率不一致"),
    ]:
        out1 = os.path.join(root, "out_bad_" + os.path.basename(bad_wav).split(".")[0])
        os.makedirs(out1, exist_ok=True)
        before = set(os.listdir(out1))
        if "stereo" in bad_wav:
            proc = run_tool(root, out1, bad_wav, ad_path, os.path.join(plans_dir, "plan_valid.json"))
        else:
            proc = run_tool(root, out1, program_path, bad_wav, os.path.join(plans_dir, "plan_valid.json"))
        after = set(os.listdir(out1))
        check(
            f"非法输入 {os.path.basename(bad_wav)} 报错退出(码1)且提示含'{expect_word}'",
            proc.returncode == 1 and expect_word in proc.stderr,
            proc.stderr.strip(),
        )
        check(
            f"非法输入 {os.path.basename(bad_wav)} 时输出目录无新文件",
            before == after,
            f"新增: {sorted(after - before)}",
        )

    # ------------------------------------------------------------------ #
    # 验收 7（先验证含空格路径正常场景）+ 3 + 4 + 6：正常计划
    # ------------------------------------------------------------------ #
    out_valid = os.path.join(root, "out 空格 目录")
    proc = run_tool(root, out_valid, program_path, ad_path, os.path.join(plans_dir, "plan_valid.json"))
    check("含空格路径下工具退出码为 0", proc.returncode == 0, proc.stderr.strip())
    stdout_ok = (
        "插入段数: 4" in proc.stdout
        and os.path.abspath(os.path.join(out_valid, "output.wav")) in proc.stdout
        and os.path.abspath(os.path.join(out_valid, "report.json")) in proc.stdout
    )
    check("标准输出摘要含插入段数(4)与输出路径", stdout_ok, proc.stdout.replace("\n", " | "))

    out_wav = os.path.join(out_valid, "output.wav")
    report_path = os.path.join(out_valid, "report.json")
    check("正常运行生成 output.wav 与 report.json", os.path.isfile(out_wav) and os.path.isfile(report_path))

    params, out_samples = read_wav(out_wav)
    with open(report_path, "r", encoding="utf-8") as fh:
        report = json.load(fh)

    # 验收 6：头部一致 + 总采样点相符
    check(
        "输出 WAV 头部与输入一致(8000Hz/16bit/单声道/PCM)",
        params.framerate == 8000 and params.sampwidth == 2
        and params.nchannels == 1 and params.comptype == "NONE",
        f"rate={params.framerate}, width={params.sampwidth}, ch={params.nchannels}",
    )
    check(
        "报告总采样点数与输出文件实际采样点数一致",
        report["output_total_samples"] == len(out_samples) == params.nframes
        and report["program_total_samples"] == program_len,
        f"report={report['output_total_samples']}, file={len(out_samples)}",
    )
    check("输出未超过原节目时长", len(out_samples) <= program_len,
          f"{len(out_samples)} <= {program_len}")

    # 验收 3 + 4：独立参考实现逐采样点比对
    with open(os.path.join(plans_dir, "plan_valid.json"), "r", encoding="utf-8") as fh:
        plan_valid = json.load(fh)["insertions"]
    ref_samples, ref_details, ref_trunc, _ = reference_build(program_samples, array.array("h",
        [expected_ad_sample(i) for i in range(ad_len)]), plan_valid, rate)
    check("输出波形与独立参考实现逐采样点一致", out_samples == ref_samples,
          f"长度 {len(out_samples)} vs {len(ref_samples)}")

    # 无静音填充：输出中不应出现工具引入的 0 填充段（节目/广告自身公式天然不为 0 串）
    zero_run = run = 0
    for v in out_samples:
        run = run + 1 if v == 0 else 0
        zero_run = max(zero_run, run)
    check("输出中不存在静音填充(连续0不超过1个采样)", zero_run <= 1, f"最长连续0={zero_run}")

    # 报告条目数量/顺序/编号/起止逐一相符
    rep_ins = report["insertions"]
    order_ok = (
        len(rep_ins) == len(ref_details)
        and [x["ad_number"] for x in rep_ins] == list(range(1, len(rep_ins) + 1))
        and all(
            rep_ins[i]["start_sample"] == ref_details[i]["start"]
            and rep_ins[i]["end_sample"] == ref_details[i]["end"]
            and rep_ins[i]["sample_count"] == ref_details[i]["end"] - ref_details[i]["start"] + 1
            for i in range(len(rep_ins))
        )
    )
    check("报告条目数量/顺序/编号(1起)/起止索引与参考实现逐一相符", order_ok)

    # 直接按报告区间核对：区间内是广告波形，区间前后是节目波形（衔接连续性）
    boundary_ok = True
    detail_msg = []
    for item in rep_ins:
        s, e = item["start_sample"], item["end_sample"]
        for j in range(s, e + 1):
            if out_samples[j] != expected_ad_sample(j - s):
                boundary_ok = False
                detail_msg.append(f"广告段{item['ad_number']}内采样{j}不符")
                break
        # 衔接点：广告前一个采样（若存在）必须等于节目波形在该输出位置的预期值
        if s > 0 and out_samples[s - 1] != ref_samples[s - 1]:
            boundary_ok = False
            detail_msg.append(f"广告段{item['ad_number']}前衔接点不符")
        if e + 1 < len(out_samples) and out_samples[e + 1] != ref_samples[e + 1]:
            boundary_ok = False
            detail_msg.append(f"广告段{item['ad_number']}后衔接点不符")
    check("报告区间内为广告波形且前后衔接采样与预期连续波形一致", boundary_ok, "; ".join(detail_msg))

    # 无重叠：报告区间两两不相交，且均在输出范围内
    spans = sorted((x["start_sample"], x["end_sample"]) for x in rep_ins)
    overlap_ok = all(spans[i][1] < spans[i + 1][0] for i in range(len(spans) - 1))
    check("各广告区间互不重叠", overlap_ok)
    check("截断节目尾部采样数与参考实现一致",
          report["truncated_program_tail_samples"] == ref_trunc,
          f"{report['truncated_program_tail_samples']} vs {ref_trunc}")

    # ------------------------------------------------------------------ #
    # 验收 2：无效项标记跳过，有效插入仍完成
    # ------------------------------------------------------------------ #
    out_invalid = os.path.join(root, "out invalid items")
    proc = run_tool(root, out_invalid, program_path, ad_path,
                    os.path.join(plans_dir, "plan_invalid_items.json"))
    check("含无效项计划整体退出码为 0（不中断）", proc.returncode == 0, proc.stderr.strip())
    with open(os.path.join(out_invalid, "report.json"), "r", encoding="utf-8") as fh:
        rep2 = json.load(fh)
    skipped = rep2["skipped_invalid_items"]
    skipped_idx = sorted(x["plan_index"] for x in skipped)
    check("报告标记全部 4 个无效项(下标1/2/3/5)且含原因",
          skipped_idx == [1, 2, 3, 5] and all(x["reason"] for x in skipped),
          f"下标={skipped_idx}")
    # 有效项：下标0(1次) 与下标4(3次) -> 共 4 段广告
    check("有效插入仍完成，共 4 段广告且编号 1..4",
          rep2["inserted_segment_count"] == 4
          and [x["ad_number"] for x in rep2["insertions"]] == [1, 2, 3, 4])
    with open(os.path.join(plans_dir, "plan_invalid_items.json"), "r", encoding="utf-8") as fh:
        plan2 = json.load(fh)["insertions"]
    _, out2 = read_wav(os.path.join(out_invalid, "output.wav"))
    ref2, ref_det2, _, inv2 = reference_build(program_samples,
                                              array.array("h", [expected_ad_sample(i) for i in range(ad_len)]),
                                              plan2, rate)
    check("含无效项计划的输出波形与参考实现一致", out2 == ref2)
    check("参考实现与工具对无效项下标判定一致", sorted(inv2) == skipped_idx)

    # ------------------------------------------------------------------ #
    # 验收 5：空间不足，只插完整广告，输出不超时，实际/未插入次数正确
    # ------------------------------------------------------------------ #
    short_path = os.path.join(input_dir, "short_program.wav")
    _, short_samples = read_wav(short_path)
    out_over = os.path.join(root, "out overflow")
    proc = run_tool(root, out_over, short_path, ad_path,
                    os.path.join(plans_dir, "plan_overflow.json"))
    check("空间不足场景退出码为 0", proc.returncode == 0, proc.stderr.strip())
    with open(os.path.join(out_over, "report.json"), "r", encoding="utf-8") as fh:
        rep5 = json.load(fh)
    _, out5 = read_wav(os.path.join(out_over, "output.wav"))
    check("输出 WAV 未超出原短节目时长(24000 采样点)",
          len(out5) <= len(short_samples) == 24000, f"输出={len(out5)}")
    # 第一项 0.5s 放 1 次（2s 广告，剩余 2.5s 可放 1 次）；第二项 2.5s 处剩余 0
    ins5 = rep5["insertions"]
    first = [x for x in ins5 if x["plan_index"] == 0]
    check("第一项实际插入 1 次，区间为 [4000, 19999]",
          len(first) == 1 and first[0]["start_sample"] == 4000 and first[0]["end_sample"] == 19999,
          json.dumps(first, ensure_ascii=False))
    # 第二项实际 0、未插入 3：报告中无该 plan_index 的插入段，且请求摘要显式记录次数
    req5 = {x["plan_index"]: x for x in rep5["insertion_requests"]}
    check("第二项 0 段实际插入（3 次全部放不下）",
          all(x["plan_index"] != 1 for x in ins5) and rep5["inserted_segment_count"] == 1)
    check("报告显式记录实际/未插入次数: 项0=1/0，项1=0/3",
          req5[0]["requested_repeats"] == 1 and req5[0]["actual_inserted"] == 1 and req5[0]["not_inserted"] == 0
          and req5[1]["requested_repeats"] == 3 and req5[1]["actual_inserted"] == 0 and req5[1]["not_inserted"] == 3,
          json.dumps(rep5["insertion_requests"], ensure_ascii=False))
    # 参考实现交叉验证
    with open(os.path.join(plans_dir, "plan_overflow.json"), "r", encoding="utf-8") as fh:
        plan5 = json.load(fh)["insertions"]
    ref5, det5, trunc5, _ = reference_build(short_samples,
                                            array.array("h", [expected_ad_sample(i) for i in range(ad_len)]),
                                            plan5, rate)
    check("空间不足场景输出波形与参考实现一致", out5 == ref5)
    check("空间不足场景报告区间与参考实现一致",
          [(x["start_sample"], x["end_sample"]) for x in ins5]
          == [(x["start"], x["end"]) for x in det5])
    check("第二项不是无效项（语义有效但容量不足）",
          all(x["plan_index"] != 1 for x in rep5["skipped_invalid_items"]))

    # ------------------------------------------------------------------ #
    # 验收 8：缺字段 / 时间非数字 -> 报错、无新文件、同名文件保持原样
    # ------------------------------------------------------------------ #
    placeholder = os.path.join(keep_dir, "placeholder.txt")
    # 预置同名输出文件，验证致命错误后内容不变
    pre_wav = os.path.join(keep_dir, "output.wav")
    pre_rep = os.path.join(keep_dir, "report.json")
    with open(pre_wav, "wb") as fh:
        fh.write(b"PRE-EXISTING-WAV-CONTENT")
    with open(pre_rep, "w", encoding="utf-8") as fh:
        fh.write('{"preexisting": true}')
    hashes_before = {p: file_hash(p) for p in (placeholder, pre_wav, pre_rep)}
    listing_before = set(os.listdir(keep_dir))

    for plan_name, word in [
        ("plan_missing_field.json", "缺少必需字段"),
        ("plan_bad_time.json", "必须是数字"),
    ]:
        proc = run_tool(root, keep_dir, program_path, ad_path, os.path.join(plans_dir, plan_name))
        check(f"{plan_name} 致命报错(码1)且提示含'{word}'",
              proc.returncode == 1 and word in proc.stderr, proc.stderr.strip())
        listing_after = set(os.listdir(keep_dir))
        check(f"{plan_name} 未在输出目录产生新文件", listing_before == listing_after,
              f"差异: {listing_after ^ listing_before}")
        hashes_after = {p: file_hash(p) for p in (placeholder, pre_wav, pre_rep)}
        check(f"{plan_name} 占位文件与同名输出文件内容未变", hashes_before == hashes_after)

    # 汇总
    total = len(results)
    failed = [r for r in results if r[0] == FAIL]
    print("\n" + "=" * 60)
    print(f"合计 {total} 项检查，通过 {total - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        for _, name, detail in failed:
            print(f"  失败: {name} -- {detail}")
    print("工作目录:", root)
    if not args.keep:
        import shutil
        shutil.rmtree(root, ignore_errors=True)
        print("临时目录已清理（加 --keep 可保留）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
