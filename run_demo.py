# -*- coding: utf-8 -*-
"""
run_demo.py — 端到端验证脚本，逐条覆盖 14 项验收要求

流程:
  1. 调用 generate_test_videos 生成确定性测试素材（独立目录 test_assets/）；
  2. 以子进程方式运行 shot_cover.py（真实退出码/控制台输出）；
  3. 校验 JSON 报告、JPEG 产物、时间戳、亮度、文件名校验和、源文件 MD5 等；
  4. 打印逐条验收结果表，任一断言失败则以非零码退出。

运行:
  python run_demo.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

ASSET_DIR = os.path.join(HERE, "test_assets")
OUT_ROOT = os.path.join(HERE, "demo_output")
SCRIPT = os.path.join(HERE, "shot_cover.py")

JPEG_RE = re.compile(r"^shot_\d{3}_\d{10}ms\.jpg$")

results: list[tuple[str, bool, str]] = []


def check(req: str, ok: bool, detail: str = ""):
    results.append((req, bool(ok), detail))


def md5_of(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_cli(video: str, out_dir: str):
    """以隔离模式 (-I) 运行命令行，返回 (returncode, stdout, stderr)。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-I", SCRIPT, "--input", video, "--output", out_dir],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def load_report(out_dir: str) -> dict:
    with open(os.path.join(out_dir, "report.json"), "r", encoding="utf-8") as fp:
        return json.load(fp)


def jpeg_mean_brightness(path: str) -> float:
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise AssertionError(f"JPEG 无法读取: {path}")
    return float(img.mean())


def source_frame_brightness(video: str, frame_indices) -> dict:
    cap = cv2.VideoCapture(video, cv2.CAP_FFMPEG)
    out = {}
    for idx in frame_indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, float(idx))
        ok, frame = cap.read()
        if ok:
            out[idx] = float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean())
    cap.release()
    return out


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    # ---- 0. 生成测试素材 -------------------------------------------------
    import shutil
    from generate_test_videos import build_all, FPS
    if os.path.isdir(ASSET_DIR):
        for name in os.listdir(ASSET_DIR):
            os.remove(os.path.join(ASSET_DIR, name))
    build_all(ASSET_DIR)

    # 清空历史产物，保证“目录内 JPEG 集合 == 报告候选集合”可被严格校验
    if os.path.isdir(OUT_ROOT):
        shutil.rmtree(OUT_ROOT)

    videos = {
        "three_cuts": os.path.join(ASSET_DIR, "three_cuts.mp4"),
        "with_black": os.path.join(ASSET_DIR, "with_black.mp4"),
        "static_long": os.path.join(ASSET_DIR, "static_long.mp4"),
        "short": os.path.join(ASSET_DIR, "short_one_sec.mp4"),
        "gradient": os.path.join(ASSET_DIR, "gradient_motion.mp4"),
        "all_black": os.path.join(ASSET_DIR, "all_black.mp4"),
        "corrupted": os.path.join(ASSET_DIR, "corrupted.mp4"),
    }

    # ---- 12. 记录源文件 MD5（运行前） -----------------------------------
    md5_before = {k: md5_of(v) for k, v in videos.items() if k != "corrupted"}

    # ---- 正常视频统一跑 CLI ---------------------------------------------
    outputs = {}
    console_ok = True
    console_detail = ""
    for key in ["three_cuts", "with_black", "static_long", "short",
                "gradient", "all_black"]:
        out_dir = os.path.join(OUT_ROOT, key)
        code, stdout, stderr = run_cli(videos[key], out_dir)
        outputs[key] = (out_dir, code, stdout, stderr)
        if code != 0:
            console_ok = False
            console_detail = f"{key} 退出码 {code}: {stderr.strip()}"
        if "检测镜头数" not in stdout or "生成候选帧数" not in stdout:
            console_ok = False
            console_detail = f"{key} 控制台摘要缺失: {stdout.strip()!r}"

    reports = {k: load_report(v[0]) for k, v in outputs.items()}

    # ---- (1) 总时长误差 <= 一帧 -----------------------------------------
    dur_ok, dur_detail = True, []
    for key, report in reports.items():
        cap = cv2.VideoCapture(videos[key], cv2.CAP_FFMPEG)
        meta_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = cap.get(cv2.CAP_PROP_FPS)
        cap.release()
        truth_ms = meta_frames / fps * 1000.0
        frame_ms = 1000.0 / fps
        err = abs(report["duration_ms"] - truth_ms)
        if err > frame_ms + 1e-6:
            dur_ok = False
        dur_detail.append(
            f"{key}: 报告 {report['duration_ms']}ms / 实际 {round(truth_ms, 1)}ms"
            f" / 误差 {round(err, 2)}ms (一帧 {round(frame_ms, 2)}ms)"
        )
    check("(1) 总时长误差<=一帧", dur_ok, "; ".join(dur_detail))

    # ---- (2) 三处硬切 -> 4 镜头 -----------------------------------------
    r = reports["three_cuts"]
    check("(2) 三硬切切出4镜头", r["shot_count"] == 4,
          f"shot_count={r['shot_count']}, 起止="
          + ";".join(f"[{s['start_ms']},{s['end_ms']})" for s in r["shots"]))

    # ---- (3) 每镜头恰好一个候选，时间戳落在镜头区间内 -------------------
    per_shot_ok = True
    detail3 = []
    for key, report in reports.items():
        if report["shot_count"] == 0:
            continue
        per_shot = {c["shot_index"]: c for c in report["candidates"]}
        if len(per_shot) != report["shot_count"]:
            per_shot_ok = False
        for s in report["shots"]:
            c = per_shot.get(s["shot_index"])
            if c is None or not (s["start_ms"] <= c["timestamp_ms"] < s["end_ms"]):
                per_shot_ok = False
        detail3.append(f"{key}: {report['shot_count']}镜头/{len(per_shot)}候选")
    check("(3) 每镜头1候选且在区间内", per_shot_ok, "; ".join(detail3))

    # ---- (4) 黑场画面不被选中：候选亮度 > 黑场亮度 ----------------------
    wb_dir, _, _, _ = outputs["with_black"]
    wb_report = reports["with_black"]
    # 源视频 45..74 帧为黑场（生成器定义）
    black_lum = source_frame_brightness(videos["with_black"], range(45, 75))
    black_mean = float(np.mean(list(black_lum.values())))
    cand_lums = [
        jpeg_mean_brightness(os.path.join(wb_dir, c["filename"]))
        for c in wb_report["candidates"]
    ]
    # 黑场帧为 45..74（30fps -> 1500ms..2500ms 半开），任何候选都不得落入该区间
    black_interval = (round(45 / FPS * 1000), round(75 / FPS * 1000))
    no_black_pick = all(
        not (black_interval[0] <= c["timestamp_ms"] < black_interval[1])
        for c in wb_report["candidates"]
    )
    check("(4) 候选帧亮度均高于黑场",
          wb_report["shot_count"] == 2
          and len(cand_lums) >= 2
          and all(lum > black_mean for lum in cand_lums)
          and no_black_pick,
          f"黑场均值亮度={black_mean:.2f}, 候选亮度="
          + ",".join(f"{x:.2f}" for x in cand_lums)
          + f", 镜头数={wb_report['shot_count']}（黑场作分界，两侧各成镜头）"
          + f", 黑场区间={black_interval}ms")

    # ---- (5) JPEG 命名规则、目录内无重名、与报告候选完全一致 -------------
    name_ok, name_detail = True, []
    for key, (out_dir, _, _, _) in outputs.items():
        files = sorted(f for f in os.listdir(out_dir) if f.endswith(".jpg"))
        expected = sorted(c["filename"] for c in reports[key]["candidates"])
        if any(not JPEG_RE.match(f) for f in files):
            name_ok = False
        if len(files) != len(set(files)):
            name_ok = False
        if files != expected:
            name_ok = False
        name_detail.append(f"{key}:{len(files)}张")
    check("(5) JPEG命名规则/无重名/与报告一致", name_ok, "; ".join(name_detail))

    # ---- (6) 报告字段齐全、时间戳整数毫秒、镜头序号连续 ------------------
    required_top = {"video_path", "duration_ms", "frame_count", "fps",
                    "shot_count", "candidate_count", "shots", "candidates"}
    schema_ok = True
    for key, report in reports.items():
        if not required_top.issubset(report.keys()):
            schema_ok = False
        idxs = [c["shot_index"] for c in report["candidates"]]
        if idxs != list(range(len(idxs))):
            schema_ok = False
        if any(not isinstance(c["timestamp_ms"], int) for c in report["candidates"]):
            schema_ok = False
        if any(not isinstance(s["shot_index"], int)
               or s["shot_index"] != i for i, s in enumerate(report["shots"])):
            schema_ok = False
        for c in report["candidates"]:
            if not {"shot_index", "filename", "timestamp_ms"}.issubset(c):
                schema_ok = False
    check("(6) JSON字段齐全/整数毫秒/序号连续", schema_ok,
          f"报告键={sorted(reports['three_cuts'].keys())}")

    # ---- (7) 损坏文件：非零退出，目录内无候选帧和报告 --------------------
    corrupt_dir = os.path.join(OUT_ROOT, "corrupted")
    if os.path.isdir(corrupt_dir):
        for f in os.listdir(corrupt_dir):
            os.remove(os.path.join(corrupt_dir, f))
    code, stdout, stderr = run_cli(videos["corrupted"], corrupt_dir)
    leftover = []
    if os.path.isdir(corrupt_dir):
        leftover = [f for f in os.listdir(corrupt_dir)
                    if f.endswith(".jpg") or f == "report.json"]
    check("(7) 损坏文件非零退出且无产物", code != 0 and not leftover,
          f"退出码={code}, 残留={leftover}, 提示={stderr.strip()[:80]}")

    # ---- (8) 不存在的新目录自动创建，产物全部在其中 ----------------------
    nested = os.path.join(OUT_ROOT, "nested", "level2", "level3")
    if os.path.exists(nested):
        shutil.rmtree(nested)
    code, stdout, stderr = run_cli(videos["short"], nested)
    created_ok = (code == 0 and os.path.isdir(nested)
                  and os.path.isfile(os.path.join(nested, "report.json"))
                  and any(f.endswith(".jpg") for f in os.listdir(nested)))
    check("(8) 自动创建输出目录并写入全部产物", created_ok,
          f"目录={os.path.relpath(nested, HERE)}, 内容={sorted(os.listdir(nested))}")

    # ---- (9) 1s 短视频：>=1 候选且报告可解析 ----------------------------
    short_r = reports["short"]
    check("(9) 1s短视频至少1候选且报告可解析",
          short_r["candidate_count"] >= 1 and len(short_r["candidates"]) >= 1,
          f"候选数={short_r['candidate_count']}, 时长={short_r['duration_ms']}ms")

    # ---- (10) 静态长镜头：候选数=镜头数，多镜头候选内容互不相似 ----------
    sr = reports["static_long"]
    static_ok = sr["shot_count"] == 1 and sr["candidate_count"] == 1
    # 三硬切视频的 4 张候选应内容各异（两两灰度平均差显著）
    tc_dir = outputs["three_cuts"][0]
    imgs = [
        cv2.imread(os.path.join(tc_dir, c["filename"]), cv2.IMREAD_GRAYSCALE)
        for c in reports["three_cuts"]["candidates"]
    ]
    pair_diffs = []
    distinct_ok = True
    for a in range(len(imgs)):
        for b in range(a + 1, len(imgs)):
            d = float(np.abs(imgs[a].astype(np.int16) - imgs[b].astype(np.int16)).mean())
            pair_diffs.append(round(d, 2))
            if d < 15.0:
                distinct_ok = False
    check("(10) 静态长镜头1候选且同镜头无重复/候选内容各异",
          static_ok and distinct_ok,
          f"静态: {sr['shot_count']}镜头/{sr['candidate_count']}候选; "
          f"四镜头候选两两差异={pair_diffs}")

    # ---- (11) 已知时间点：候选位于各镜头中点，误差<=一帧 ----------------
    r = reports["three_cuts"]
    fps = r["fps"]
    frame_ms = 1000.0 / fps
    ts_ok, ts_detail = True, []
    for s, c in zip(r["shots"], r["candidates"]):
        mid_frame = (s["start_frame"] + s["end_frame"]) // 2
        expected_ms = round(mid_frame / fps * 1000.0)
        err = abs(c["timestamp_ms"] - expected_ms)
        if err > frame_ms + 1e-6:
            ts_ok = False
        # 同时用帧序号反推时间戳，验证与报告一致
        back = round(mid_frame / fps * 1000.0)
        ts_detail.append(
            f"镜头{s['shot_index']}: 报告{c['timestamp_ms']}ms/"
            f"中点帧{mid_frame}理论{back}ms/误差{err}ms"
        )
    check("(11) 毫秒时间戳与源帧位置一致", ts_ok, "; ".join(ts_detail))

    # ---- (12) 运行前后源 MP4 的 MD5 不变 --------------------------------
    md5_after = {k: md5_of(v) for k, v in videos.items() if k != "corrupted"}
    unchanged = md5_before == md5_after
    check("(12) 源文件只读且MD5前后一致", unchanged,
          "全部 6 个正常视频 MD5 运行前后相同" if unchanged
          else str([k for k in md5_before if md5_before[k] != md5_after[k]]))

    # ---- (13) 单一长镜头：镜头数 1、候选 1 ------------------------------
    gr = reports["gradient"]
    check("(13) 无切换长镜头只输出1候选",
          sr["shot_count"] == 1 and sr["candidate_count"] == 1
          and gr["shot_count"] == 1 and gr["candidate_count"] == 1,
          f"static_long: {sr['shot_count']}镜头/{sr['candidate_count']}候选; "
          f"gradient: {gr['shot_count']}镜头/{gr['candidate_count']}候选")

    # ---- (14) 控制台摘要 ------------------------------------------------
    sample = outputs["three_cuts"][2].strip()
    check("(14) 控制台打印镜头数与候选数摘要", console_ok,
          f"示例输出: {sample}")

    # ---- 附加：全黑视频零候选、报告仍可解析（需求4/7边界） ---------------
    ab = reports["all_black"]
    check("附加: 全黑视频零候选且报告可解析",
          ab["shot_count"] == 0 and ab["candidate_count"] == 0,
          f"shot_count={ab['shot_count']}, candidate_count={ab['candidate_count']}")

    # ---- 输出结果表 ------------------------------------------------------
    print("\n" + "=" * 78)
    print("验收结果".center(70))
    print("=" * 78)
    passed = 0
    for req, ok, detail in results:
        passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] {req:<38} {detail}")
    print("=" * 78)
    print(f"合计 {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
