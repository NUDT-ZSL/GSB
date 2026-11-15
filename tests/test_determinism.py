"""需求 8：重复执行结果一致，且不残留过期条带。"""

from __future__ import annotations

import hashlib

from animstrip.pipeline import run_pipeline


def _hash_dir(output_dir) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output_dir.glob("**/*"))
        if path.is_file()
    }


def test_repeated_run_byte_identical(manifest_path, output_dir, report):
    first = _hash_dir(output_dir)
    second_report = run_pipeline(manifest_path, output_dir)
    second = _hash_dir(output_dir)

    # 需求 8 核对方式：连跑两次输出文件集合与逐字节内容一致
    assert set(first) == set(second)
    assert first == second
    assert second_report == report


def test_stale_strip_removed_on_next_run(manifest_path, output_dir):
    run_pipeline(manifest_path, output_dir)
    stale = output_dir / "anim_ghost.png"
    stale.write_bytes(b"stale")
    # 同时放一个非条带文件，确认清理只针对 *.png 条带
    keep = output_dir / "notes.txt"
    keep.write_text("keep me", encoding="utf-8")

    run_pipeline(manifest_path, output_dir)

    assert not stale.exists()  # 过期条带被清除
    assert keep.exists()  # 非条带文件不动
    expected = {
        f"{row['animation_id']}.png"
        for row in run_pipeline(manifest_path, output_dir)["animations"]
        if row["strip"] is not None
    }
    actual = {p.name for p in output_dir.glob("*.png")}
    assert actual == expected


def test_rejection_then_fix_produces_strip(tmp_path, manifest_path, sample_dir):
    # 原本被拒的动画修正后重跑，应能补上条带且结果同步更新。
    # 修正清单需放在样本目录内，使相对图片路径仍按样本目录解析。
    import json

    fixed_manifest = sample_dir / "manifest_fixed.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in data["animations"]:
        if entry["animation_id"] == "anim_bad_dur":
            entry["target_duration_ms"] = 50
    fixed_manifest.write_text(json.dumps(data), encoding="utf-8")

    out = tmp_path / "out"
    run_pipeline(fixed_manifest, out)
    rows = {r["animation_id"]: r for r in run_pipeline(fixed_manifest, out)["animations"]}
    assert rows["anim_bad_dur"]["passed"] is True
    assert (out / "anim_bad_dur.png").is_file()
