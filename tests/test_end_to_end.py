"""端到端测试：调用真实 CLI、真实 ffmpeg，回读生成视频核对。

覆盖需求 9（尺寸/帧率/输出目录/文件名）、10（报告结构）、11（非法输入）、
12（成功项独立保留）、13（重复运行一致）。
"""

from __future__ import annotations

import hashlib

import pytest

from previewkit.models import EnvironmentProblem
from previewkit.planner import frame_count, frame_time_ms
from previewkit.video import decode_frames, find_ffmpeg, pick_codec
from helpers import SAMPLES, load_report, run_cli


@pytest.fixture(scope="session")
def ffmpeg() -> str:
    try:
        exe = find_ffmpeg()
        pick_codec(exe)
        return exe
    except EnvironmentProblem as exc:  # pragma: no cover - 环境缺依赖时跳过
        pytest.skip(f"ffmpeg 不可用：{exc}")


@pytest.fixture(scope="session")
def main_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("main")
    proc = run_cli(
        "--shots",
        str(SAMPLES / "shots_main.json"),
        "--subtitles",
        str(SAMPLES / "subtitles_main.json"),
        "--out-dir",
        str(out),
        "--width",
        "320",
        "--height",
        "180",
        "--fps",
        "10",
    )
    assert proc.returncode == 0, proc.stderr
    report = load_report(out / "report_shots_main__subtitles_main.json")
    return out, report, proc


# ---------------------------------------------------------------- 需求 9
def test_output_layout_and_cli_settings(main_run):
    out, report, _ = main_run
    video = out / "preview_shots_main__subtitles_main.mkv"
    report_path = out / "report_shots_main__subtitles_main.json"
    assert video.is_file() and report_path.is_file()
    assert report["settings"]["width"] == 320
    assert report["settings"]["height"] == 180
    assert report["settings"]["fps"] == 10
    assert report["outputs"] == {
        "video": video.name,
        "report": report_path.name,
    }
    # 目录外没有多余产物
    assert {p.name for p in out.iterdir()} == {video.name, report_path.name}


def test_custom_filenames_and_resolution(tmp_path):
    proc = run_cli(
        "--shots",
        str(SAMPLES / "shots_gap.json"),
        "--subtitles",
        str(SAMPLES / "subtitles_gap.json"),
        "--out-dir",
        str(tmp_path / "nested" / "dir"),
        "--width",
        "256",
        "--height",
        "144",
        "--fps",
        "8",
        "--video-name",
        "my_preview.mkv",
        "--report-name",
        "my_report.json",
    )
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "nested" / "dir" / "my_preview.mkv").is_file()
    assert (tmp_path / "nested" / "dir" / "my_report.json").is_file()


# ---------------------------------------------------------------- 需求 10
def test_report_schema_and_fixed_reason_vocabulary(main_run):
    _, report, _ = main_run
    assert report["schema_version"] == "1.0"
    assert report["status"] in ("complete", "partial")
    summary = report["summary"]
    assert summary["shots_total"] == 4
    assert summary["subtitles_total"] == 9
    assert summary["subtitles_composed"] == 4
    assert summary["subtitles_unfinished"] == 5
    assert summary["subtitles_spanning_cuts"] == 1
    assert len(report["shots"]["canonical"]) == 4
    allowed = {"reversed_range", "out_of_bounds", "empty_text", "unknown_shot", "outside_shot"}
    groups = report["subtitles"]["unfinished_by_reason"]
    assert groups  # 主样例一定有未完成项
    seen = set()
    for group in groups:
        assert group["reason"] in allowed
        assert group["count"] == len(group["items"])
        seen.add(group["reason"])
    assert seen == {"reversed_range", "out_of_bounds", "empty_text", "unknown_shot", "outside_shot"}
    for item in report["subtitles"]["unfinished"]:
        assert item["reason"] in allowed
        assert item["text"] and isinstance(item["detail"], str) and item["detail"]


def test_report_keeps_composed_shots_and_subtitles_independently(main_run):
    _, report, _ = main_run
    composed = report["subtitles"]["composed"]
    assert len(composed) == report["subtitles"]["composed_count"]
    assert [c["subtitle_index"] for c in composed] == sorted(c["subtitle_index"] for c in composed)
    canonical = report["shots"]["canonical"]
    assert [s["shot_id"] for s in canonical] == ["S01", "S02", "S03", "S04"]
    assert all(s["rendered"] for s in canonical)
    # 每条成功字幕都能定位到镜头，且这些镜头确实在帧里出现
    for entry in composed:
        assert entry["covered_shots"]
        for sid in entry["covered_shots"]:
            shot = next(s for s in canonical if s["shot_id"] == sid)
            assert shot["frame_range"] is not None
            assert entry["subtitle_index"] in shot["composed_subtitles"]


# ---------------------------------------------------------------- 需求 4 + 5 + 6 + 13
def test_frames_carry_expected_subtitle_text(main_run, ffmpeg):
    out, report, _ = main_run
    video = out / report["outputs"]["video"]
    width, height = report["settings"]["width"], report["settings"]["height"]
    frames = list(decode_frames(ffmpeg, video, width, height))
    assert len(frames) == frame_count(report["settings"]["duration_ms"], report["settings"]["fps"])

    # 跨切点字幕：切点 4800ms 前后两帧文本一致
    by_time = {frame_time_ms(i, 10): frames[i] for i in range(len(frames))}
    before, after = by_time[4700], by_time[4800]
    assert before.tobytes() != after.tobytes()  # 镜头确实换了
    index = report["frame_index"]
    before_spec = next(f for f in index if f["time_ms"] == 4700)
    after_spec = next(f for f in index if f["time_ms"] == 4800)
    assert before_spec["shot_id"] == "S02"
    assert after_spec["shot_id"] == "S03"
    assert (
        before_spec["subtitle_texts"]
        == after_spec["subtitle_texts"]
        == ["这句字幕横跨切点，不能断"]
    )

    # 无效镜头编号的字幕不出现在任何帧里
    banned = "幽灵镜头里的字幕"
    assert all(banned not in f["subtitle_texts"] for f in index)
    # 越界/倒序字幕也不出现
    for text in ("倒序字幕", "越界字幕"):
        assert all(text not in f["subtitle_texts"] for f in index)
    # 合法字幕在对应帧出现
    assert any("第一镜：开场" in f["subtitle_texts"] for f in index)
    assert any("结尾一句" in f["subtitle_texts"] for f in index)


def test_verification_block_reports_pixel_identical(main_run):
    _, report, _ = main_run
    verification = report["verification"]
    assert verification["ok"] is True
    assert verification["pixel_identical"] is True
    assert verification["decoded_frame_count"] == verification["expected_frame_count"]
    assert verification["sampled_frames"]
    # 抽样必须覆盖每个切点前后两帧
    sampled = set(verification["sampled_frames"])
    for pair in verification["boundary_frames"].values():
        assert set(pair) <= sampled


# ---------------------------------------------------------------- 需求 7
def test_overlap_sample_reports_conflict_and_cut(tmp_path, ffmpeg):
    proc = run_cli(
        "--shots",
        str(SAMPLES / "shots_overlap.json"),
        "--subtitles",
        str(SAMPLES / "subtitles_overlap.json"),
        "--out-dir",
        str(tmp_path),
        "--width",
        "256",
        "--height",
        "144",
    )
    assert proc.returncode == 0, proc.stderr
    report = load_report(tmp_path / "report_shots_overlap__subtitles_overlap.json")
    assert report["summary"]["conflicts"] == 1
    conflict = report["conflicts"][0]
    assert (conflict["earlier_shot_id"], conflict["later_shot_id"]) == ("S_A", "S_B")
    assert conflict["cut_at_ms"] == 3000
    assert conflict["overlap_start_ms"] == 2200

    s_b = next(s for s in report["shots"]["canonical"] if s["shot_id"] == "S_B")
    assert s_b["effective_start_ms"] == 3000
    assert s_b["status"] == "head_clamped"
    # 时间轴依据更早结束时刻：2999ms 属于 S_A，3000ms 属于 S_B
    index = report["frame_index"]
    at_2900 = next(f for f in index if f["time_ms"] == 2900)
    at_3000 = next(f for f in index if f["time_ms"] == 3000)
    assert at_2900["shot_id"] == "S_A"
    assert at_3000["shot_id"] == "S_B"
    # 跨重叠切点的字幕在切点前后连续
    assert at_2900["subtitle_texts"] == at_3000["subtitle_texts"] == ["跨过重叠切点的字幕"]
    frames = list(decode_frames(ffmpeg, tmp_path / report["outputs"]["video"], 256, 144))
    assert len(frames) == report["settings"]["frame_count"]


# ---------------------------------------------------------------- 需求 8
def test_gap_sample_renders_blank_placeholder_and_lists_gap(tmp_path, ffmpeg):
    proc = run_cli(
        "--shots",
        str(SAMPLES / "shots_gap.json"),
        "--subtitles",
        str(SAMPLES / "subtitles_gap.json"),
        "--out-dir",
        str(tmp_path),
        "--width",
        "256",
        "--height",
        "144",
    )
    assert proc.returncode == 0, proc.stderr
    report = load_report(tmp_path / "report_shots_gap__subtitles_gap.json")
    assert report["gaps"] == [
        {
            "start_ms": 4000,
            "end_ms": 5000,
            "duration_ms": 1000,
            "note": "空档渲染为空画面占位，不延续相邻镜头，也不显示字幕",
        }
    ]
    assert report["summary"]["gap_frames"] == 10
    gap_frames = [f for f in report["frame_index"] if f["kind"] == "gap"]
    assert len(gap_frames) == 10
    assert all(f["shot_id"] is None and f["subtitle_texts"] == [] for f in gap_frames)
    assert [f["time_ms"] for f in gap_frames] == list(range(4000, 5000, 100))

    # 空档帧的画面既不同于前一镜头也不同于后一镜头（不是被延续过去的）
    frames = list(decode_frames(ffmpeg, tmp_path / report["outputs"]["video"], 256, 144))
    assert frames[39].tobytes() != frames[45].tobytes()
    assert frames[50].tobytes() != frames[45].tobytes()
    # 跨空档字幕被登记为未完成，且不出现在任何帧
    assert report["subtitles"]["unfinished_count"] == 1
    assert report["subtitles"]["unfinished"][0]["reason"] == "outside_shot"
    assert all("不应出现在空档里的字幕" not in f["subtitle_texts"] for f in report["frame_index"])


# ---------------------------------------------------------------- 需求 11
@pytest.mark.parametrize(
    "shots_name, subtitles_name, needle",
    [
        ("invalid/shots_broken_json.json", "invalid/subtitles_ok.json", "不是合法 JSON"),
        ("invalid/shots_bad_field_type.json", "invalid/subtitles_ok.json", "start_ms 必须是整数"),
        (
            "invalid/shots_valid_but_subtitles_wrong_format.json",
            "invalid/subtitles_missing_field.json",
            "缺少字段 end_ms",
        ),
    ],
)
def test_invalid_input_fails_without_artifacts(tmp_path, shots_name, subtitles_name, needle):
    out = tmp_path / "should_not_be_created"
    proc = run_cli(
        "--shots",
        str(SAMPLES / shots_name),
        "--subtitles",
        str(SAMPLES / subtitles_name),
        "--out-dir",
        str(out),
    )
    assert proc.returncode != 0
    assert needle in proc.stderr
    # 输出目录里不得有任何预览文件
    assert not out.exists() or not list(out.iterdir())


def test_missing_input_file_reports_clear_error(tmp_path):
    proc = run_cli(
        "--shots",
        str(tmp_path / "nope.json"),
        "--subtitles",
        str(SAMPLES / "subtitles_main.json"),
        "--out-dir",
        str(tmp_path / "out"),
    )
    assert proc.returncode == 2
    assert "找不到输入文件" in proc.stderr
    assert not (tmp_path / "out").exists()


def test_invalid_cli_arguments(tmp_path):
    proc = run_cli(
        "--shots",
        str(SAMPLES / "shots_gap.json"),
        "--subtitles",
        str(SAMPLES / "subtitles_gap.json"),
        "--out-dir",
        str(tmp_path),
        "--width",
        "301",
    )
    assert proc.returncode == 2
    assert "偶数" in proc.stderr


# ---------------------------------------------------------------- 需求 13
def test_repeated_runs_are_byte_identical(tmp_path):
    args = (
        "--shots",
        str(SAMPLES / "shots_main.json"),
        "--subtitles",
        str(SAMPLES / "subtitles_main.json"),
        "--width",
        "320",
        "--height",
        "180",
        "--fps",
        "10",
    )
    outs = []
    for name in ("a", "b"):
        out = tmp_path / name
        proc = run_cli(*args, "--out-dir", str(out))
        assert proc.returncode == 0, proc.stderr
        outs.append(out)
    for filename in (
        "preview_shots_main__subtitles_main.mkv",
        "report_shots_main__subtitles_main.json",
    ):
        digest_a = hashlib.sha256((outs[0] / filename).read_bytes()).hexdigest()
        digest_b = hashlib.sha256((outs[1] / filename).read_bytes()).hexdigest()
        assert digest_a == digest_b, f"{filename} 两次运行不一致"


def test_report_is_deterministic_ignoring_paths(tmp_path):
    # 两次运行参数完全相同、输出目录不同；outputs 只记录文件名，
    # 所以两份报告应逐字符相同。
    args = (
        "--shots",
        str(SAMPLES / "shots_gap.json"),
        "--subtitles",
        str(SAMPLES / "subtitles_gap.json"),
        "--width",
        "256",
        "--height",
        "144",
    )
    texts = []
    for name in ("x", "y"):
        out = tmp_path / name
        assert run_cli(*args, "--out-dir", str(out)).returncode == 0
        texts.append((out / "report_shots_gap__subtitles_gap.json").read_text(encoding="utf-8"))
    assert texts[0] == texts[1]


# ---------------------------------------------------------------- 覆盖保护
def test_existing_outputs_require_overwrite_flag(tmp_path):
    args = (
        "--shots",
        str(SAMPLES / "shots_gap.json"),
        "--subtitles",
        str(SAMPLES / "subtitles_gap.json"),
        "--out-dir",
        str(tmp_path),
        "--width",
        "256",
        "--height",
        "144",
    )
    assert run_cli(*args).returncode == 0
    video = tmp_path / "preview_shots_gap__subtitles_gap.mkv"
    before = video.read_bytes()

    again = run_cli(*args)
    assert again.returncode == 3
    assert "--overwrite" in again.stderr
    assert video.read_bytes() == before  # 原有产物不应被半途破坏

    assert run_cli(*args, "--overwrite").returncode == 0
    assert video.read_bytes() == before  # 同输入重跑，产物逐字节不变
