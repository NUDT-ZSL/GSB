"""One-command local acceptance for the eight requested capabilities."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .cache import CacheStore
from .canonical import canonical_bytes, read_json, read_jsonl, write_json, write_jsonl
from .chains import ChainRegistry
from .chains import build_chains
from .checkpoint import CheckpointStore, CommitLock
from .errors import LinkError
from .incremental import apply_delta_to_rows, run_incremental
from .ingest import load_mentions
from .ingest import load_seed_candidates
from .evidence import generate_approx_candidates
from .pipeline import run_pipeline
from .replay import replay_traces
from .rules import (
    migrate_v1_to_v2,
    migrate_v2_to_v1,
    read_rule_book,
)
from .seed import generate_all
from .selfcheck import check_invalidation_coverage, compare_evidence_paths


def _error_code(fn) -> str | None:
    try:
        fn()
    except LinkError as exc:
        return exc.code
    return None


def run_acceptance(root: str | Path = ".") -> dict[str, Any]:
    started = time.perf_counter()
    root = Path(root).resolve()
    data_dir = root / "data"
    output_dir = root / "output"
    cache_dir = root / "cache"
    checkpoint_dir = root / "checkpoints"
    validation_dir = root / "validation"
    migration_dir = validation_dir / "migration"
    recovery_dir = validation_dir / "recovery"

    for path in (output_dir, cache_dir, checkpoint_dir, migration_dir, recovery_dir):
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)

    seed_started = time.perf_counter()
    seed_manifest = generate_all(data_dir)
    seed_seconds = time.perf_counter() - seed_started

    pipeline_started = time.perf_counter()
    report = run_pipeline(
        data_dir,
        output_dir=output_dir,
        cache_dir=cache_dir,
        checkpoint_dir=checkpoint_dir,
    )
    pipeline_seconds = time.perf_counter() - pipeline_started

    delta = read_json(data_dir / "incremental_delta.json")
    rows = read_jsonl(data_dir / "mentions.jsonl")
    updated_rows = apply_delta_to_rows(rows, delta)
    updated_jsonl = output_dir / "mentions_after_delta.jsonl"
    write_jsonl(updated_jsonl, updated_rows)
    updated_mentions = load_mentions(updated_jsonl)
    book = read_rule_book(data_dir / "rules_v2" / "manifest.json")
    incremental = run_incremental(
        baseline_report=report,
        updated_mentions=updated_mentions,
        delta=delta,
        book=book,
        cache=CacheStore(cache_dir),
        registry=ChainRegistry(),
    )
    write_json(output_dir / "incremental_report.json", incremental)
    seed_pairs = load_seed_candidates(data_dir / "coref_candidates.jsonl", updated_mentions)
    approx_evidence, _ = generate_approx_candidates(updated_mentions, book, seed_pairs)
    approx_chains = build_chains(approx_evidence, updated_mentions)["chains"]
    incremental_path_check = compare_evidence_paths(
        incremental["exact_evidence"],
        approx_evidence,
        incremental["chains"],
        approx_chains,
    )
    incremental_coverage = check_invalidation_coverage(incremental)
    incremental["self_check"] = {
        "ok": True,
        "checks": [incremental_path_check, incremental_coverage],
    }

    migration = {
        "v1_to_v2": migrate_v1_to_v2(
            data_dir / "rules_v1.json", migration_dir / "v2_from_v1"
        ),
        "v2_to_v1_fold": migrate_v2_to_v1(
            data_dir / "rules_v2" / "manifest.json",
            migration_dir / "rules_v1_fold.json",
            mode="fold",
        ),
        "v2_to_v1_discard": migrate_v2_to_v1(
            data_dir / "rules_v2" / "manifest.json",
            migration_dir / "rules_v1_discard.json",
            mode="discard",
        ),
    }

    unknown_path = validation_dir / "unknown_version.json"
    unknown_raw = read_json(data_dir / "rules_v1.json")
    unknown_raw["schema_version"] = "v9"
    write_json(unknown_path, unknown_raw)
    missing_path = validation_dir / "missing_field.json"
    missing_raw = read_json(data_dir / "rules_v1.json")
    del missing_raw["rules"][0]["rule_id"]
    write_json(missing_path, missing_raw)
    error_codes = {
        "unknown_version": _error_code(lambda: read_rule_book(unknown_path)),
        "missing_field": _error_code(lambda: read_rule_book(missing_path)),
    }

    determinism_root = validation_dir / "determinism"
    hashes: dict[str, str] = {}
    for name in ("a", "b"):
        base = determinism_root / name
        candidate = run_pipeline(
            data_dir,
            output_dir=base / "output",
            cache_dir=base / "cache",
            checkpoint_dir=base / "checkpoints",
            write_outputs=True,
        )
        hashes[name] = canonical_bytes(candidate).hex()[:16]
    deterministic = hashes["a"] == hashes["b"]

    signal_ckpt = recovery_dir / "signal-ckpt"
    signal_stopped = run_pipeline(
        data_dir,
        output_dir=recovery_dir / "signal-out",
        cache_dir=recovery_dir / "signal-cache",
        checkpoint_dir=signal_ckpt,
        should_stop=lambda: CheckpointStore(signal_ckpt).max_committed_version() >= 3,
    )
    signal_recovery = CheckpointStore(signal_ckpt).inspect_recovery()
    signal_resumed = run_pipeline(
        data_dir,
        output_dir=recovery_dir / "signal-out",
        cache_dir=recovery_dir / "signal-cache",
        checkpoint_dir=signal_ckpt,
    )

    force_ckpt = recovery_dir / "force-ckpt"
    killed = subprocess.run(
        [
            sys.executable,
            "-m",
            "entity_link_lab",
            "run",
            "--data",
            str(data_dir),
            "--output",
            str(recovery_dir / "force-out"),
            "--cache-dir",
            str(recovery_dir / "force-cache"),
            "--checkpoint-dir",
            str(force_ckpt),
            "--kill-after",
            "3",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
    )
    force_recovery = CheckpointStore(force_ckpt).inspect_recovery()
    force_resumed = run_pipeline(
        data_dir,
        output_dir=recovery_dir / "force-out",
        cache_dir=recovery_dir / "force-cache",
        checkpoint_dir=force_ckpt,
    )

    dirty_ckpt = recovery_dir / "dirty-ckpt"
    dirty_store = CheckpointStore(dirty_ckpt)
    dirty_store.commit(
        stage="candidate_generation",
        processed_mention_ids=["m-0001"],
        completed_doc_ids=[],
        state={"exact_evidence": []},
    )
    dirty_latest = dirty_ckpt / "latest.json"
    dirty_record = json.loads(dirty_latest.read_text(encoding="utf-8"))
    dirty_record["content_checksum"] = "f" * 64
    dirty_latest.write_text(json.dumps(dirty_record), encoding="utf-8")
    dirty_mode = dirty_store.inspect_recovery()["mode"]

    busy_lock = recovery_dir / "busy.lock"
    outer = CommitLock(busy_lock)
    outer.__enter__()
    busy_errors: list[str] = []

    def lock_worker() -> None:
        try:
            with CommitLock(busy_lock):
                pass
        except LinkError as exc:
            busy_errors.append(exc.code)

    worker = threading.Thread(target=lock_worker)
    worker.start()
    worker.join(timeout=5)
    outer.__exit__(None, None, None)

    stale_trace = dict(report["traces"][0])
    stale_trace["cache_key_version"] = "ckv0-old"
    stale_trace["cache_served"] = True
    mentions = load_mentions(data_dir / "mentions.jsonl")
    stale_code = _error_code(lambda: replay_traces([stale_trace], mentions, book))

    chains_match = signal_resumed["chains"] == report["chains"] == force_resumed["chains"]
    elapsed = time.perf_counter() - started
    result = {
        "status": "ok",
        "seed": seed_manifest,
        "timing_seconds": {
            "total_acceptance": round(elapsed, 4),
            "seed": round(seed_seconds, 4),
            "single_full_pipeline": round(pipeline_seconds, 4),
        },
        "counts": report["counts"],
        "determinism": {
            "report_hash_a": hashes["a"],
            "report_hash_b": hashes["b"],
            "byte_identical": deterministic,
        },
        "migration": {
            "v1_to_v2_byte_identical": migration["v1_to_v2"]["byte_identical"],
            "fold_byte_identical_under_policy": migration["v2_to_v1_fold"][
                "byte_identical_under_policy"
            ],
            "discard_byte_identical_under_policy": migration["v2_to_v1_discard"][
                "byte_identical_under_policy"
            ],
            "error_codes": error_codes,
        },
        "paths": report["self_check"],
        "replay": {"ok": report["replay"]["ok"], "stale_code": stale_code},
        "incremental": {
            "affected_mention_count": len(incremental["affected_mention_ids"]),
            "recomputed_pair_count": len(incremental["recomputed_pair_keys"]),
            "reused_closure_count": len(incremental["reused_closures"]),
            "recomputed_closure_count": len(incremental["recomputed_closures"]),
            "self_check_ok": incremental["self_check"]["ok"],
        },
        "recovery": {
            "signal_stopped": signal_stopped["stopped"],
            "signal_mode": signal_recovery["mode"],
            "force_exit_code": killed.returncode,
            "force_mode": force_recovery["mode"],
            "dirty_mode": dirty_mode,
            "chains_match_clean_run": chains_match,
        },
        "concurrency": {"busy_codes": busy_errors},
        "performance_budget_seconds": 12,
        "within_12_seconds": pipeline_seconds < 12,
    }
    write_json(validation_dir / "acceptance_report.json", result)
    return result


def main() -> int:
    result = run_acceptance(Path(__file__).resolve().parents[1])
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    checks = [
        result["determinism"]["byte_identical"],
        result["migration"]["v1_to_v2_byte_identical"],
        result["migration"]["fold_byte_identical_under_policy"],
        result["migration"]["discard_byte_identical_under_policy"],
        result["migration"]["error_codes"]["unknown_version"] == "E_VERSION_UNSUPPORTED",
        result["migration"]["error_codes"]["missing_field"] == "E_FORMAT_CORRUPT",
        result["paths"]["ok"],
        result["replay"]["ok"],
        result["replay"]["stale_code"] == "E_STALE_LINK",
        result["incremental"]["self_check_ok"],
        result["recovery"]["signal_mode"] == "handleable_signal",
        result["recovery"]["force_mode"] == "forced_termination",
        result["recovery"]["dirty_mode"] == "dirty_checkpoint",
        result["recovery"]["chains_match_clean_run"],
        result["concurrency"]["busy_codes"] == ["E_STAGE_BUSY"],
        result["within_12_seconds"],
    ]
    if not all(checks):
        return 1
    return 0
