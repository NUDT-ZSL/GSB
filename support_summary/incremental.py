"""Targeted invalidation for append, strength, and vocabulary revisions."""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set, Tuple

from .errors import BusinessError
from .models import Session, SummaryConfig, Vocabulary, session_from_dict
from .pipeline import PipelineRunner, RunResult
from .sample_data import append_turns_to_record
from .selfcheck import run_self_check
from .validation import load_vocabulary, dataset_signature


def _read_session_records(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records


def _stage_counts(result: RunResult) -> Dict[str, Dict[str, int]]:
    counts: Dict[str, Dict[str, int]] = {}
    for event in result.events:
        stage = str(event["stage"])
        status = str(event["status"])
        counts.setdefault(stage, {"recomputed": 0, "reused": 0})
        counts[stage][status] += 1
    return counts


def run_incremental_scenario(
    sessions: Sequence[Session],
    vocabulary: Vocabulary,
    config: SummaryConfig,
    *,
    cache_dir: Path,
    scenario: str,
    target_session_id: str | None = None,
    new_strength: float | None = None,
    vocabulary_path: Path | None = None,
    seed: int = 20261124,
) -> Dict[str, Any]:
    if scenario not in ("append", "strength", "vocab"):
        raise BusinessError(
            "E_MUTATION_INVALID",
            "scenario must be append, strength, or vocab",
            field="scenario",
        )
    baseline = PipelineRunner(
        sessions, vocabulary, config, cache_dir=cache_dir
    ).run()

    mutated_sessions: Sequence[Session] = sessions
    mutated_vocabulary = vocabulary
    mutated_config = config
    affected_sessions: List[str]

    if scenario == "append":
        sid = target_session_id or sessions[0].session_id
        if sid not in {session.session_id for session in sessions}:
            raise BusinessError(
                "E_SESSION_NOT_FOUND",
                f"session {sid!r} does not exist",
                session_id=sid,
                field="session_id",
            )
        records = [session.to_dict() for session in sessions]
        mutated_records = []
        for record in records:
            if record["session_id"] == sid:
                record = append_turns_to_record(record, seed=seed)
            mutated_records.append(record)
        mutated_sessions = [session_from_dict(record) for record in mutated_records]
        affected_sessions = [sid]
    elif scenario == "strength":
        if new_strength is None:
            raise BusinessError(
                "E_MUTATION_INVALID",
                "new_strength is required for the strength scenario",
                field="new_strength",
            )
        mutated_config = replace(config, rewrite_strength=float(new_strength))
        affected_sessions = [session.session_id for session in sessions]
    else:
        if vocabulary_path is None:
            raise BusinessError(
                "E_MUTATION_INVALID",
                "vocabulary_path is required for the vocab scenario",
                field="vocabulary_path",
            )
        mutated_vocabulary = load_vocabulary(vocabulary_path)
        affected_sessions = [session.session_id for session in sessions]

    incremental = PipelineRunner(
        mutated_sessions,
        mutated_vocabulary,
        mutated_config,
        cache_dir=cache_dir,
    ).run()

    with tempfile.TemporaryDirectory(prefix="summary-full-cache-") as temp_dir:
        full = PipelineRunner(
            mutated_sessions,
            mutated_vocabulary,
            mutated_config,
            cache_dir=Path(temp_dir),
        ).run()

    byte_identical = incremental.summary_bytes == full.summary_bytes
    digest_identical = incremental.summary_digest == full.summary_digest
    reco, reuse = incremental.node_sets()
    all_nodes = set(reco) | set(reuse)
    disjoint = not bool(reco & reuse)
    coverage = (reco | reuse) == all_nodes
    self_check = run_self_check(incremental)

    stage_counts = _stage_counts(incremental)
    expected_stage_behavior = {
        "append": {
            "chunk_tokenize": "recomputed_new_blocks_only",
            "unit_score": "recomputed_new_blocks_only",
            "rewrite": "recomputed_new_units_only",
            "summary_merge": "recomputed_affected_and_final",
        },
        "strength": {
            "chunk_tokenize": "fully_reused",
            "unit_score": "fully_reused",
            "rewrite": "fully_recomputed",
            "summary_merge": "fully_recomputed",
        },
        "vocab": {
            "chunk_tokenize": "fully_recomputed",
            "unit_score": "fully_recomputed",
            "rewrite": "fully_recomputed",
            "summary_merge": "fully_recomputed",
        },
    }[scenario]

    behavior_ok = True
    if scenario == "strength":
        behavior_ok = (
            stage_counts["chunk_tokenize"]["recomputed"] == 0
            and stage_counts["unit_score"]["recomputed"] == 0
            and stage_counts["rewrite"]["reused"] == 0
            and stage_counts["summary_merge"]["reused"] == 0
        )
    elif scenario == "vocab":
        behavior_ok = all(
            counts["reused"] == 0 for counts in stage_counts.values()
        )
    else:
        sid = affected_sessions[0]
        chunk_recomputed = {
            event["node_id"]
            for event in incremental.events
            if event["stage"] == "chunk_tokenize"
            and event["status"] == "recomputed"
        }
        chunk_reused = {
            event["node_id"]
            for event in incremental.events
            if event["stage"] == "chunk_tokenize"
            and event["status"] == "reused"
        }
        behavior_ok = (
            len(chunk_recomputed) == 1
            and all(sid in node_id for node_id in chunk_recomputed)
            and any(sid not in node_id for node_id in chunk_reused)
        )

    return {
        "ok": (
            byte_identical
            and digest_identical
            and disjoint
            and coverage
            and behavior_ok
            and self_check["ok"]
        ),
        "scenario": scenario,
        "base_signature": dataset_signature(sessions, vocabulary, config),
        "mutated_signature": dataset_signature(
            mutated_sessions, mutated_vocabulary, mutated_config
        ),
        "affected_sessions": sorted(affected_sessions),
        "affected_nodes": sorted(reco),
        "recomputed_sessions": incremental.to_report()["recomputed_sessions"],
        "reused_sessions": incremental.to_report()["reused_sessions"],
        "recomputed_nodes": sorted(reco),
        "reused_nodes": sorted(reuse),
        "node_partition": {
            "disjoint": disjoint,
            "coverage": coverage,
            "recomputed_count": len(reco),
            "reused_count": len(reuse),
        },
        "stage_counts": stage_counts,
        "expected_stage_behavior": expected_stage_behavior,
        "behavior_ok": behavior_ok,
        "incremental_digest": incremental.summary_digest,
        "full_digest": full.summary_digest,
        "byte_identical_to_full": byte_identical,
        "digest_identical_to_full": digest_identical,
        "incremental_summaries": incremental.summaries,
        "self_check": self_check,
    }
