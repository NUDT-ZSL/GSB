from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dedup_workbench.checkpoint import ForcedTermination
from dedup_workbench.engine import DedupWorkbench
from dedup_workbench.errors import WorkbenchError
from dedup_workbench.ingest import content_hash_for
from dedup_workbench.query import QueryService
from dedup_workbench.testing import base_config, make_record, write_config, write_jsonl


def three_group_records() -> list[dict]:
    duplicate_text = "incremental duplicate text with multiple deterministic blocks"
    return [
        make_record("dup-a", duplicate_text, quality_score=0.9),
        make_record("dup-b", duplicate_text, quality_score=0.6),
        make_record("solo", "entirely separate isolated signature material"),
    ]


def run(tmp_path: Path, records, config, runtime="runtime", input_name="corpus.jsonl"):
    input_path = tmp_path / input_name
    write_jsonl(input_path, records)
    return DedupWorkbench(tmp_path / runtime, config).run(input_path)


def changed_config(base: dict, **changes) -> dict:
    config = json.loads(json.dumps(base))
    config.update(changes)
    config["config_version"] = base.get("config_version", 1) + 1
    return config


def test_identical_rerun_reuses_every_document(tmp_path):
    config = base_config()
    first = run(tmp_path, three_group_records(), config)
    assert len(first["incremental"]["recomputed_docs"]) == 3
    second = DedupWorkbench(tmp_path / "runtime", config).run(tmp_path / "corpus.jsonl")
    incremental = second["incremental"]
    assert incremental["recomputed_docs"] == []
    assert incremental["reused_docs"] == ["dup-a", "dup-b", "solo"]
    assert incremental["disjoint"] is True
    assert incremental["covers_all_docs"] is True


def test_threshold_change_reuses_signatures_and_comparisons(tmp_path):
    config = base_config()
    run(tmp_path, three_group_records(), config)
    next_config = changed_config(config, similarity_threshold=0.99)
    report = DedupWorkbench(tmp_path / "runtime", next_config).run(tmp_path / "corpus.jsonl")
    incremental = report["incremental"]
    assert incremental["signature_recomputed_docs"] == []
    assert len(incremental["comparison_recomputed_pairs"]) == 0
    assert len(incremental["comparison_reused_pairs"]) == 1
    assert incremental["reused_docs"] == ["solo"]
    assert set(incremental["recomputed_docs"]) == {"dup-a", "dup-b"}
    assert "decision_component" in incremental["recomputed_nodes"]


def test_validation_mode_change_recomputes_comparisons_only(tmp_path):
    config = base_config()
    run(tmp_path, three_group_records(), config)
    digest_config = changed_config(config, validation_mode="digest")
    report = DedupWorkbench(tmp_path / "runtime", digest_config).run(tmp_path / "corpus.jsonl")
    incremental = report["incremental"]
    assert incremental["signature_recomputed_docs"] == []
    assert len(incremental["comparison_recomputed_pairs"]) == 1
    assert incremental["comparison_reused_pairs"] == []
    assert report["comparisons"][0]["validation_mode"] == "digest"


def test_chunk_length_change_recomputes_all_signatures(tmp_path):
    config = base_config()
    run(tmp_path, three_group_records(), config)
    chunk_config = changed_config(config, chunk_signature_length=20)
    report = DedupWorkbench(tmp_path / "runtime", chunk_config).run(tmp_path / "corpus.jsonl")
    incremental = report["incremental"]
    assert set(incremental["signature_recomputed_docs"]) == {"dup-a", "dup-b", "solo"}
    assert set(incremental["recomputed_docs"]) == {"dup-a", "dup-b", "solo"}
    assert "signature" in incremental["recomputed_nodes"]


def test_version_only_bump_conservatively_invalidates_all_stage_keys(tmp_path):
    config = base_config()
    run(tmp_path, three_group_records(), config)
    version_only = changed_config(config)
    report = DedupWorkbench(tmp_path / "runtime", version_only).run(tmp_path / "corpus.jsonl")
    incremental = report["incremental"]
    assert set(incremental["signature_recomputed_docs"]) == {"dup-a", "dup-b", "solo"}
    assert len(incremental["comparison_recomputed_pairs"]) == 1
    assert set(incremental["recomputed_docs"]) == {"dup-a", "dup-b", "solo"}


def test_content_revision_recomputes_only_isolated_document(tmp_path):
    config = base_config()
    run(tmp_path, three_group_records(), config)
    revised = three_group_records()
    revised[2]["text"] = "revised isolated text with fresh block signatures xyz"
    revised[2]["content_hash"] = content_hash_for(revised[2]["text"])
    revised_path = tmp_path / "revised.jsonl"
    write_jsonl(revised_path, revised)
    report = DedupWorkbench(tmp_path / "runtime", config).run(revised_path)
    incremental = report["incremental"]
    assert incremental["changed_docs"] == ["solo"]
    assert incremental["signature_recomputed_docs"] == ["solo"]
    assert incremental["recomputed_docs"] == ["solo"]
    assert set(incremental["reused_docs"]) == {"dup-a", "dup-b"}
    assert incremental["disjoint"] and incremental["covers_all_docs"]


def test_query_records_are_byte_identical_to_batch_export(tmp_path):
    config = base_config()
    report = run(tmp_path, three_group_records(), config)
    service = QueryService(tmp_path / "runtime" / "exports" / "doc_records.jsonl", config)
    for doc_id in ("dup-a", "dup-b", "solo"):
        result = service.query(doc_id, iterations=3)
        assert result["byte_identical_to_batch_line"] is True
        assert result["record_sha256"]
        assert result["timing"]["loop_iterations"] == 3
        assert result["timing"]["signature_block_count"] >= 1

    with pytest.raises(WorkbenchError) as exc_info:
        service.query("solo", budget_ms=0.000000001, iterations=20)
    assert exc_info.value.code == "E_LATENCY_BUDGET"
    assert "timing" in exc_info.value.details

    with pytest.raises(WorkbenchError) as missing:
        service.query("missing")
    assert missing.value.code == "E_NOT_FOUND"


def test_checkpoint_resumes_and_skips_committed_batches(tmp_path):
    records = [make_record(f"doc-{index:02d}", f"unique recovery text number {index} " * 4) for index in range(10)]
    config = base_config(limits={"batch_size": 2})
    input_path = tmp_path / "recovery.jsonl"
    write_jsonl(input_path, records)

    workbench = DedupWorkbench(tmp_path / "recovery-runtime", config)
    with pytest.raises(ForcedTermination):
        workbench.run(input_path, crash_after_batch=1)

    checkpoint = json.loads((tmp_path / "recovery-runtime" / "checkpoints" / "batch_executor.json").read_text())
    assert checkpoint["committed_batch_indexes"] == [0, 1]

    resumed = DedupWorkbench(tmp_path / "recovery-runtime", config).run(input_path)
    assert resumed["checkpoint"]["skipped_committed_batches"] == [0, 1]
    assert resumed["checkpoint"]["executed_batches"] == [2, 3, 4]
    assert resumed["self_check"]["ok"] is True


def test_sigkill_after_commit_recovers_without_recomputing_batch(tmp_path):
    records = [make_record(f"kill-{index:02d}", f"sigkill recovery text {index} " * 4) for index in range(8)]
    config = base_config(limits={"batch_size": 2})
    input_path = tmp_path / "kill.jsonl"
    config_path = tmp_path / "kill-config.json"
    write_jsonl(input_path, records)
    write_config(config_path, config)
    runtime = tmp_path / "kill-runtime"
    project_root = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PYTHONPATH": str(project_root / "src")}

    killed = subprocess.run(
        [
            sys.executable,
            "-m",
            "dedup_workbench.cli",
            "run",
            "--input",
            str(input_path),
            "--config",
            str(config_path),
            "--workdir",
            str(runtime),
            "--kill-after-batch",
            "1",
        ],
        cwd=project_root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert killed.returncode == -9
    checkpoint = json.loads((runtime / "checkpoints" / "batch_executor.json").read_text())
    assert checkpoint["committed_batch_indexes"] == [0, 1]

    resumed = DedupWorkbench(runtime, config).run(input_path)
    assert resumed["checkpoint"]["skipped_committed_batches"] == [0, 1]
    assert resumed["checkpoint"]["executed_batches"] == [2, 3]
    assert resumed["self_check"]["ok"] is True
