"""验收测试：对应需求 1-8，全部使用确定性合成样本，不访问网络。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import soundfile as sf

from subaudit.audit import FAIL_HOOK_ENV, audit_manifest
from subaudit.samples import SAMPLE_RATE, build_manifest, build_tone_audio, make_sample

VOICE = [(1000.0, 2000.0), (3000.0, 4000.0), (5000.0, 6000.0)]


@pytest.fixture(scope="module")
def speech_audio():
    return build_tone_audio()


# 需求 1：三句清单按清单顺序输出逐句 JSON 结果
def test_01_order_matches_manifest(speech_audio):
    manifest = build_manifest("normal")
    report = audit_manifest(speech_audio, SAMPLE_RATE, manifest)
    assert [r["id"] for r in report["results"]] == ["S1", "S2", "S3"]
    assert [r["order"] for r in report["results"]] == [1, 2, 3]
    # 逐句结果字段齐全（需求 7 的字段要求一并验证）
    for row in report["results"]:
        assert set(
            [
                "order",
                "id",
                "text",
                "subtitle_start_ms",
                "subtitle_end_ms",
                "voice_start_ms",
                "voice_end_ms",
                "start_deviation_ms",
                "end_deviation_ms",
                "conclusion",
                "issues",
            ]
        ).issubset(row.keys())


# 需求 2：同一输入运行两次，逐句语音起止毫秒完全相同
def test_02_deterministic_across_runs(speech_audio):
    manifest = build_manifest("normal")
    first = audit_manifest(speech_audio, SAMPLE_RATE, manifest)
    second = audit_manifest(speech_audio, SAMPLE_RATE, manifest)
    key = lambda rows: [(r["id"], r["voice_start_ms"], r["voice_end_ms"]) for r in rows]
    assert key(first["results"]) == key(second["results"])


# 需求 3：语音 1000-2000、字幕 800-2200 -> 偏差 -200 / +200
def test_03_deviation_sign_and_value(speech_audio):
    manifest = [
        {"id": "S1", "text": "x", "start_ms": 800, "end_ms": 2200},
        {"id": "S2", "text": "x", "start_ms": 2800, "end_ms": 4200},
        {"id": "S3", "text": "x", "start_ms": 4800, "end_ms": 6200},
    ]
    report = audit_manifest(speech_audio, SAMPLE_RATE, manifest)
    first = report["results"][0]
    assert (first["voice_start_ms"], first["voice_end_ms"]) == (1000, 2000)
    assert first["start_deviation_ms"] == -200
    assert first["end_deviation_ms"] == 200


# 需求 4：字幕终点早于语音终点的句子被单列，其余句结论齐全
def test_04_flagged_listed_others_intact(speech_audio):
    manifest = build_manifest("early_end")
    report = audit_manifest(speech_audio, SAMPLE_RATE, manifest)
    assert "S2" in report["flagged"]
    flagged_row = next(r for r in report["results"] if r["id"] == "S2")
    assert "voice_trailing_subtitle" in flagged_row["issues"]
    # 其余两句结论齐全
    others = [r for r in report["results"] if r["id"] in ("S1", "S3")]
    assert len(others) == 2
    assert all(r["conclusion"] == "ok" for r in others)
    assert all(r["voice_start_ms"] is not None for r in others)


# 需求 5：倒置条目单列且可辨识，其余句结论完好（同时覆盖非数字、重复句号）
def test_05_invalid_entries_isolated(speech_audio):
    manifest = [
        {"id": "S1", "text": "a", "start_ms": 800, "end_ms": 2200},
        {"id": "S2", "text": "b", "start_ms": 4100, "end_ms": 2900},  # 倒置
        {"id": "S3", "text": "c", "start_ms": 4900, "end_ms": 6100},
    ]
    report = audit_manifest(speech_audio, SAMPLE_RATE, manifest)
    invalid = report["invalid_entries"]
    assert len(invalid) == 1
    assert invalid[0]["entry"]["id"] == "S2"
    assert "倒置" in invalid[0]["reason"]
    assert [r["id"] for r in report["results"]] == ["S1", "S3"]
    assert all(r["conclusion"] for r in report["results"])

    # 非数字与重复句号同样被单列
    manifest_bad = [
        {"id": "S1", "start_ms": "soon", "end_ms": 2200},
        {"id": "S1", "start_ms": 1, "end_ms": 2},
        {"id": "S3", "start_ms": 4900, "end_ms": 6100},
    ]
    report_bad = audit_manifest(speech_audio, SAMPLE_RATE, manifest_bad)
    reasons = " ".join(e["reason"] for e in report_bad["invalid_entries"])
    assert "数字" in reasons
    assert "重复" in reasons
    assert [r["id"] for r in report_bad["results"]] == ["S3"]


# 需求 6 + 7：静音 WAV 两句均标注未检出语音，语音起止为 null，结论可区分
def test_06_07_silence_marks_no_voice():
    import numpy as np

    silence = np.zeros(SAMPLE_RATE * 2, dtype=np.float64)
    manifest = build_manifest("silent")
    report = audit_manifest(silence, SAMPLE_RATE, manifest)
    assert len(report["results"]) == 2
    for row in report["results"]:
        assert row["conclusion"] == "no_voice_detected"
        assert row["voice_start_ms"] is None
        assert row["voice_end_ms"] is None
        assert row["start_deviation_ms"] is None
        assert row["end_deviation_ms"] is None
        assert "no_voice_detected" in row["issues"]


# 需求 7：正常句语音起止为整数毫秒、偏差可序列化
def test_07_normal_rows_have_numeric_voice_bounds(speech_audio):
    report = audit_manifest(speech_audio, SAMPLE_RATE, build_manifest("normal"))
    for row in report["results"]:
        assert isinstance(row["voice_start_ms"], int)
        assert isinstance(row["voice_end_ms"], int)
    json.dumps(report, ensure_ascii=False)  # 整体可 JSON 序列化


# 需求 8：注入单句异常，其余句 JSON 结果完整可用，并区分完整/未完成
def test_08_single_sentence_failure_isolated(speech_audio, monkeypatch):
    monkeypatch.setenv(FAIL_HOOK_ENV, "S2")
    report = audit_manifest(speech_audio, SAMPLE_RATE, build_manifest("normal"))
    assert report["status"] == "incomplete"
    assert len(report["failures"]) == 1
    assert report["failures"][0]["id"] == "S2"
    surviving = {r["id"]: r for r in report["results"]}
    assert set(surviving) == {"S1", "S3"}
    for row in surviving.values():
        assert row["voice_start_ms"] is not None
        assert row["conclusion"]
    json.dumps(report, ensure_ascii=False)  # 失败后报告仍可序列化使用


# 端到端：CLI 生成样本 -> 审计 -> 报告文件内容满足全部关键约束
def test_cli_end_to_end(tmp_path):
    wav_path, json_path = make_sample("normal", tmp_path / "s")
    report_path = tmp_path / "report.json"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "subaudit.cli",
            "audit",
            str(wav_path),
            str(json_path),
            "-o",
            str(report_path),
        ],
        cwd=Path(__file__).resolve().parents[1] / "src",
        capture_output=True,
        # Windows 中文控制台可能按 GBK 输出，交给系统编码解码并替换非法字节，
        # 报告本身以 UTF-8 落盘，不受影响。
        text=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "complete"
    assert [r["id"] for r in report["results"]] == ["S1", "S2", "S3"]


# 确定性：落盘 WAV 两次生成的字节一致
def test_sample_generation_is_byte_deterministic(tmp_path):
    d1, d2 = tmp_path / "a", tmp_path / "b"
    w1, _ = make_sample("normal", d1)
    w2, _ = make_sample("normal", d2)
    assert w1.read_bytes() == w2.read_bytes()
    data1, sr1 = sf.read(str(w1))
    data2, sr2 = sf.read(str(w2))
    assert sr1 == sr2 == SAMPLE_RATE
    assert (data1 == data2).all()
