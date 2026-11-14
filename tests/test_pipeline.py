"""端到端流水线测试，逐条对应任务 9 项核对方式。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cover_preview.compose import preview_filename, probe_candidate  # noqa: E402
from cover_preview.models import Candidate  # noqa: E402
from cover_preview.pipeline import run_batch  # noqa: E402

from conftest import result_by_id  # noqa: E402


# ---- 需求 1：三候选预览含三格且总宽不超过画布宽 ---------------------------

def test_three_candidate_preview_three_cells_fit_canvas(payload, previews_dir):
    record = result_by_id(payload)["V001"]
    img = Image.open(previews_dir / record["preview_file"])
    assert record["candidate_count"] == 3
    assert img.width == 1080            # 总宽等于画布宽，未超出
    assert img.height == 1920 // 3      # 三格等高横向拼贴


def test_render_preview_cell_count_matches_candidates(workspace):
    """直接对渲染函数做结构断言：格数恒等于候选数，宽度恒等于画布宽。"""
    from cover_preview.compose import render_preview
    from cover_preview.fonts import resolve_font
    from cover_preview.layout import layout_copy
    from cover_preview.models import load_manifest

    entries, _, font_hint = load_manifest(workspace / "samples" / "manifest.json")
    font_path = resolve_font(font_hint)
    entry = next(e for e in entries if e.video_id == "V001")
    decoded = []
    from cover_preview.compose import probe_candidate
    for candidate in entry.candidates:
        decoded.append(probe_candidate(candidate))
    layout = layout_copy(entry, font_path)
    preview = render_preview(entry, decoded, layout, font_path)
    cell_w = entry.canvas_width // len(entry.candidates)
    assert preview.width == entry.canvas_width
    # 三格各含一个左上角徽标（蓝底 20,60,120）
    px = preview.load()
    for idx in range(len(entry.candidates)):
        assert px[idx * cell_w + 6, 6] == (20, 60, 120)


def test_preview_width_never_exceeds_canvas(payload, previews_dir):
    for record in payload["results"]:
        img = Image.open(previews_dir / record["preview_file"])
        assert img.width <= 1080


# ---- 需求 2：文案压住保护框的样本输出图中不再相交（像素级） ---------------

def _has_white_text_blob(img: Image.Image, region: tuple[int, int, int, int], min_blob: int = 12) -> bool:
    """区域内是否存在成片纯白像素（文案笔画）。

    缩格 LANCZOS 抗锯齿最多产生零星纯白噪点，不构成连通簇；
    真正的文字会产生远大于 min_blob 的连通像素簇。
    """
    crop = img.crop(region).convert("RGB")
    width, height = crop.size
    mask = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if crop.getpixel((x, y)) == (255, 255, 255)
    }
    while mask:
        seed = mask.pop()
        stack = [seed]
        size = 1
        while stack:
            x, y = stack.pop()
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if (nx, ny) in mask:
                    mask.remove((nx, ny))
                    stack.append((nx, ny))
                    size += 1
        if size >= min_blob:
            return True
    return False


def test_v004_text_clears_protection_box_in_image(payload, previews_dir):
    record = result_by_id(payload)["V004"]
    img = Image.open(previews_dir / record["preview_file"])
    scale = img.width / 1080.0
    # 在保护框内部取安全采样区（内缩 60 全画布像素），避开文案下沿与红框边线
    inset = round(60 * scale)
    x0 = round((60 + 60) * scale)
    y0 = round((120 + 60) * scale)
    x1 = round((60 + 960 - 60) * scale)
    y1 = round((120 + 1200 - 60) * scale)
    assert inset > 0
    # 框内不存在成片纯白像素（文案笔画），零星抗锯齿噪点忽略
    assert not _has_white_text_blob(img, (x0, y0, x1, y1))
    assert record["layout_mode"] == "常规排版"
    assert 40 <= record["final_font_size"] < 96


# ---- 需求 3：极端样本文案整体位于下方条内且字号不小于最小值 ---------------

def test_v005_fallback_text_inside_bottom_bar(payload, previews_dir):
    record = result_by_id(payload)["V005"]
    assert record["layout_mode"] == "回退排版"
    assert record["final_font_size"] >= 40
    img = Image.open(previews_dir / record["preview_file"])
    scale = img.width / 1080.0
    # 条带背景色（回退暖色）应出现在每个格的底部
    px = img.load()
    for cell_x in (img.width // 6, img.width // 2, 5 * img.width // 6):
        r, g, b = px[cell_x, img.height - 4]
        assert r > 235 and 220 < g < 245 and b < 230
    # 条带上方不存在成片纯白文案（检测区在红框内收，避开红框抗锯齿与徽标带）
    upper_region = (
        img.width // 8,
        round(150 * scale),
        7 * img.width // 8,
        round(1600 * scale),
    )
    assert not _has_white_text_blob(img, upper_region)


# ---- 需求 4：损坏帧占位格保留且写明原因，其余候选正常显示 -----------------

def test_v007_placeholder_cells_with_reasons(payload, previews_dir, workspace):
    record = result_by_id(payload)["V007"]
    assert record["candidate_count"] == 3
    reasons = {item["path"]: item["reason"] for item in record["unavailable_candidates"]}
    assert any("解码失败" in v for v in reasons.values())
    assert any("文件缺失" in v for v in reasons.values())

    img = Image.open(previews_dir / record["preview_file"])
    assert img.width == 1080
    cell_w = 1080 // 3
    # 第2、3格画面区应出现占位灰底
    px = img.load()
    for cell_idx in (1, 2):
        cx = cell_idx * cell_w + cell_w // 2
        cy = img.height // 3
        r, g, b = px[cx, cy]
        assert abs(r - g) < 8 and abs(g - b) < 8 and 130 < r < 180
    # 第1格是真实图，与占位灰不同
    r1, g1, b1 = px[cell_w // 2, img.height // 3]
    assert not (abs(r1 - g1) < 8 and abs(g1 - b1) < 8 and 130 < r1 < 180)


def test_probe_candidate_reasons(workspace):
    missing = Candidate(workspace / "samples" / "frames" / "no_such.png", "frames/no_such.png")
    img, reason = probe_candidate(missing)
    assert img is None and reason == "文件缺失"

    corrupt = Candidate(workspace / "samples" / "frames" / "V007_c2.jpg", "frames/V007_c2.jpg")
    img, reason = probe_candidate(corrupt)
    assert img is None and reason.startswith("解码失败")

    good = Candidate(workspace / "samples" / "frames" / "V001_c1.png", "frames/V001_c1.png")
    img, reason = probe_candidate(good)
    assert img is not None and reason == ""


# ---- 需求 5：全坏视频输出占位图，其他视频预览正常 -------------------------

def test_v008_all_unavailable_still_outputs_placeholder(payload, previews_dir):
    record = result_by_id(payload)["V008"]
    assert record["all_candidates_unavailable"] is True
    preview = previews_dir / record["preview_file"]
    assert preview.is_file()
    img = Image.open(preview)
    assert img.width == 1080
    px = img.load()
    # 两格均为占位灰底
    for cell_idx in range(2):
        cx = cell_idx * (1080 // 2) + (1080 // 4)
        r, g, b = px[cx, img.height // 3]
        assert abs(r - g) < 8 and abs(g - b) < 8 and 130 < r < 180


def test_other_videos_unaffected(payload):
    ids = {item["video_id"] for item in payload["results"]}
    assert {"V001", "V007", "V008", "V011"}.issubset(ids)


# ---- 需求 6：结果 JSON 按视频ID排序且字段与可见排版一致 -------------------

def test_results_sorted_and_consistent_with_images(payload, previews_dir):
    ids = [item["video_id"] for item in payload["results"]]
    assert ids == sorted(ids)
    for item in payload["results"]:
        assert item["preview_file"] == preview_filename(item["video_id"])
        assert (previews_dir / item["preview_file"]).is_file()
        assert item["final_font_size"] >= 40 or item["video_id"] in {"V003", "V011"}
        assert item["layout_mode"] in {"常规排版", "回退排版"}


def test_manifest_order_differs_from_output_order(workspace):
    with (workspace / "samples" / "manifest.json").open("r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    manifest_ids = [e["video_id"] for e in manifest["entries"] if e.get("candidates")]
    # 清单本身是打乱的（输出排序由 test_results_sorted... 验证）
    assert manifest_ids != sorted(manifest_ids)


# ---- 需求 7：重复运行覆盖同名文件，不产生副本 -----------------------------

def test_rerun_overwrites_same_filenames(workspace):
    previews_dir = workspace / "output" / "previews"
    before = sorted(p.name for p in previews_dir.glob("*_preview.png"))
    run_batch(workspace / "samples" / "manifest.json", workspace / "output")
    after = sorted(p.name for p in previews_dir.glob("*_preview.png"))
    assert before == after
    assert len(after) == 11


# ---- 需求 8：非法条目进入跳过记录，正常条目仍有预览图 ---------------------

def test_invalid_entries_skipped_and_valid_processed(payload, previews_dir):
    skipped = {item["video_id"]: item["reason"] for item in payload["skipped"]}
    assert "V012" in skipped and "画布尺寸非法" in skipped["V012"]
    assert "V013" in skipped and "候选列表为空" in skipped["V013"]
    assert not (previews_dir / preview_filename("V012")).exists()
    assert (previews_dir / preview_filename("V001")).is_file()
    assert payload["summary"]["success"] == 11
    assert payload["summary"]["skipped"] == 2


# ---- 需求 9：终端摘要数量与输出目录实物一致 -------------------------------

def test_summary_matches_directory(payload, previews_dir):
    physical = len(list(previews_dir.glob("*_preview.png")))
    assert physical == payload["summary"]["preview_count"]
    assert physical == payload["summary"]["success"]
    assert Path(payload["summary"]["results_json"]).is_file()
    assert Path(payload["summary"]["previews_dir"]) == previews_dir


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
