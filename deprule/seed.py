"""Deterministic local sample generation.

The fixed seed is explicit data rather than a runtime RNG, making the generated
workspace auditable and byte-reproducible.
"""

from __future__ import annotations

import json
from pathlib import Path

from .common import write_json
from .rules import canonical_ir, finalize_ir, ir_to_v1_doc, ir_to_v3_doc, write_v2_bundle


SEED_ID = "fixed-seed-uid2d1b8ac8-461"


def sample_tasks() -> list[dict]:
    rows = [
        {
            "sentence_id": "en-001",
            "raw_text": "She will quickly finish the detailed report .",
            "token_list": ["She", "will", "quickly", "finish", "the", "detailed", "report", "."],
            "pos_tag_list": ["PRON", "AUX", "ADV", "VERB", "DET", "ADJ", "NOUN", "PUNCT"],
            "lang": "en",
        },
        {
            "sentence_id": "en-002",
            "raw_text": "The committee approved the plan yesterday .",
            "token_list": ["The", "committee", "approved", "the", "plan", "yesterday", "."],
            "pos_tag_list": ["DET", "NOUN", "VERB", "DET", "NOUN", "NOUN", "PUNCT"],
            "lang": "en",
        },
        {
            "sentence_id": "en-003",
            "raw_text": "The cat sat on the mat .",
            "token_list": ["The", "cat", "sat", "on", "the", "mat", "."],
            "pos_tag_list": ["DET", "NOUN", "VERB", "ADP", "DET", "NOUN", "PUNCT"],
            "lang": "en",
        },
        {
            "sentence_id": "zh-001",
            "raw_text": "她 已经 完成 报告 。",
            "token_list": ["她", "已经", "完成", "报告", "。"],
            "pos_tag_list": ["PRON", "ADV", "VERB", "NOUN", "PUNCT"],
            "lang": "zh",
        },
        {
            "sentence_id": "zh-002",
            "raw_text": "项目 团队 昨天 发布 新 版本",
            "token_list": ["项目", "团队", "昨天", "发布", "新", "版本"],
            "pos_tag_list": ["NOUN", "NOUN", "ADV", "VERB", "ADJ", "NOUN"],
            "lang": "zh",
        },
        {
            "sentence_id": "zh-003",
            "raw_text": "明天 晴天 。",
            "token_list": ["明天", "晴天", "。"],
            "pos_tag_list": ["NOUN", "NOUN", "PUNCT"],
            "lang": "zh",
        },
    ]
    for row in rows:
        if row["raw_text"] != " ".join(row["token_list"]):
            raise AssertionError(f"seed raw_text/token mismatch: {row['sentence_id']}")
        if len(row["token_list"]) != len(row["pos_tag_list"]):
            raise AssertionError(f"seed POS mismatch: {row['sentence_id']}")
    return rows


def seed_workspace(out_dir: str | Path = "data", *, force: bool = True) -> dict:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    tasks_path = root / "tasks.jsonl"
    with tasks_path.open("w", encoding="utf-8") as handle:
        for task in sample_tasks():
            handle.write(json.dumps(task, ensure_ascii=False, sort_keys=True) + "\n")

    ir_v1 = canonical_ir(revision=1)
    v1_doc = ir_to_v1_doc(ir_v1)
    v1_doc["seed_id"] = SEED_ID
    write_json(root / "rules-v1.json", v1_doc)

    v2_dir = root / "rules-v2"
    write_v2_bundle(v2_dir, ir_v1)

    v3_doc = ir_to_v3_doc(ir_v1)
    v3_doc["seed_id"] = SEED_ID
    write_json(root / "rules-v3.json", v3_doc)

    ir_rev2 = canonical_ir(revision=2, changed=True)
    rev2_dir = root / "rules-v2-rev2"
    write_v2_bundle(rev2_dir, ir_rev2)

    extended_ir = canonical_ir(revision=3, extended=True)
    write_json(root / "rules-v3-extended.json", ir_to_v3_doc(extended_ir))

    drift_ir = canonical_ir(revision=1)
    drift_ir["root_policy"]["search_window"] = 1
    finalize_ir(drift_ir)
    write_json(root / "rules-v3-drift.json", ir_to_v3_doc(drift_ir))

    malformed_dir = root / "malformed"
    malformed_dir.mkdir(exist_ok=True)
    write_json(malformed_dir / "unknown-version.json", {"version": "v9", "rule_id": "bad"})

    bad_count = dict(v1_doc)
    bad_count["pipe_record"] = "V1|only|three|fields"
    write_json(malformed_dir / "bad-pipe-count.json", bad_count)

    zero_window = dict(v1_doc)
    parts = zero_window["pipe_record"].split("|")
    parts[9] = "0"
    zero_window["pipe_record"] = "|".join(parts)
    write_json(malformed_dir / "zero-window.json", zero_window)

    bad_tags = {
        "version": "v3",
        "rule": {
            "syntax": {"tag_set": ["NOUN", 7, "VERB"], "dependency_tag_set": ["root"]},
            "dependency": {},
            "root": {},
        },
        "checksums": {},
    }
    write_json(malformed_dir / "bad-tag-array.json", bad_tags)

    return {
        "seed_id": SEED_ID,
        "out_dir": str(root),
        "tasks": str(tasks_path),
        "rules_v1": str(root / "rules-v1.json"),
        "rules_v2": str(v2_dir / "v2_manifest.json"),
        "rules_v3": str(root / "rules-v3.json"),
        "rules_v2_rev2": str(rev2_dir / "v2_manifest.json"),
        "rules_v3_extended": str(root / "rules-v3-extended.json"),
        "rules_v3_drift": str(root / "rules-v3-drift.json"),
        "malformed_dir": str(malformed_dir),
    }


if __name__ == "__main__":
    print(json.dumps(seed_workspace(), ensure_ascii=False, indent=2))
