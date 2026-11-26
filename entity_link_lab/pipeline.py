"""Deterministic full pipeline wired to the four-stage checkpoint machine."""

from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any, Callable

from .cache import CacheStore, closure_cache_key, dependency_hash
from .canonical import content_hash, read_jsonl, write_json, write_jsonl
from .chains import ChainRegistry, build_chains, build_trace_records
from .checkpoint import STAGES, CheckpointStore
from .errors import E_INCONSISTENT, LinkError
from .evidence import compute_evidence, select_rule
from .ingest import load_mentions, load_seed_candidates
from .replay import replay_traces
from .rules import internal_hash, read_rule_book
from .selfcheck import run_self_checks


def _within_window(left: dict[str, Any], right: dict[str, Any], book: dict[str, Any]) -> bool:
    if left["doc_id"] != right["doc_id"] or left["entity_type"] != right["entity_type"]:
        return False
    rule = select_rule(book, left["entity_type"])
    return abs(left["sent_id"] - right["sent_id"]) <= int(rule["sentence_window"])


def _evidence_for_pair(
    left: dict[str, Any],
    right: dict[str, Any],
    book: dict[str, Any],
    stats: dict[str, Any],
    path: str,
    origins: list[str],
) -> dict[str, Any]:
    evidence = compute_evidence(left, right, book, stats, path=path)
    evidence["candidate_origins"] = sorted(set(origins))
    evidence["candidate_sources"] = evidence["candidate_origins"]
    evidence["tie_break"] = {
        "first_mention_start": evidence["first_mention"]["start"],
        "entity_type": evidence["shared_entity_type"],
        "pair_key": evidence["pair_key"],
    }
    return evidence


def run_checkpointed_pipeline(
    mentions: list[dict[str, Any]],
    seed_pairs: list[dict[str, str]],
    book: dict[str, Any],
    checkpoint_store: CheckpointStore,
    *,
    stats: dict[str, dict[str, Any]],
    inject_kill_after: int | None = None,
    inject_signal_after: int | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    recovery = checkpoint_store.inspect_recovery()
    if recovery["mode"] == "dirty_checkpoint":
        raise LinkError(
            recovery["code"],
            "refusing to continue from dirty checkpoint",
            stage="pipeline.recovery",
            details=recovery,
        )
    latest = recovery.get("checkpoint")
    checkpoint_store.write_run_status("running", pid=os.getpid())

    if latest is None:
        state: dict[str, Any] = {
            "exact_evidence": [],
            "approx_evidence": [],
            "approx_buckets": {},
            "decisions_progress": [],
            "coverage_progress": [],
        }
        current_version = 0
        start_stage_index = 0
        processed: set[str] = set()
    else:
        state = latest["state"]
        current_version = int(latest["version"])
        start_stage_index = int(latest["stage_index"])
        processed = set(latest["processed_mention_ids"])
        if state.get("stage_complete"):
            start_stage_index += 1
            processed = set()

    by_id = {item["mention_id"]: item for item in mentions}
    seed_by_child = {item["mention_b"]: item for item in seed_pairs}
    last_order_by_doc: dict[str, int] = {}
    for mention in mentions:
        last_order_by_doc[mention["doc_id"]] = mention["order_index"]
    completed_docs: set[str] = set()
    commit_count = 0
    stopped = False

    def commit_after_mention(
        stage_index: int, mention_id: str, *, complete: bool
    ) -> dict[str, Any]:
        nonlocal current_version, commit_count
        if mention_id and mention_id not in processed:
            processed.add(mention_id)
        ordered_processed = sorted(processed)
        mention = by_id[mention_id]
        if mention["order_index"] == last_order_by_doc[mention["doc_id"]]:
            completed_docs.add(mention["doc_id"])
        state["stage_complete"] = complete
        record = checkpoint_store.commit(
            stage=STAGES[stage_index],
            processed_mention_ids=ordered_processed,
            completed_doc_ids=sorted(completed_docs),
            state=state,
            expected_version=current_version,
        )
        current_version = int(record["version"])
        commit_count += 1
        return record

    for stage_index in range(start_stage_index, len(STAGES)):
        stage = STAGES[stage_index]
        if latest is not None and latest.get("stage") != stage:
            processed = set()
            completed_docs = set()
        elif latest is not None and latest.get("stage") == stage and not state.get("stage_complete"):
            processed = set(latest["processed_mention_ids"])
            completed_docs = set(latest["completed_doc_ids"])
        else:
            processed = set()
            completed_docs = set()

        for mention in mentions:
            mention_id = mention["mention_id"]
            if mention_id in processed:
                continue

            if stage == "candidate_generation":
                origins_by_pair: dict[str, list[str]] = {}
                counterparts: list[dict[str, Any]] = []
                for earlier in mentions:
                    if earlier["order_index"] >= mention["order_index"]:
                        break
                    if _within_window(earlier, mention, book):
                        counterparts.append(earlier)
                        origins_by_pair.setdefault(
                            f"{earlier['mention_id']}|{mention_id}", []
                        ).append("rule_scan")
                seed = seed_by_child.get(mention_id)
                if seed:
                    earlier = by_id[seed["mention_a"]]
                    if _within_window(earlier, mention, book):
                        key = seed["pair_key"]
                        if key not in origins_by_pair:
                            counterparts.append(earlier)
                        origins_by_pair.setdefault(key, []).append(
                            seed.get("origin", "seed_list")
                        )
                existing_keys = {item["pair_key"] for item in state["exact_evidence"]}
                for earlier in counterparts:
                    key = f"{earlier['mention_id']}|{mention_id}"
                    if key in existing_keys:
                        continue
                    evidence = _evidence_for_pair(
                        earlier,
                        mention,
                        book,
                        stats,
                        "exact",
                        origins_by_pair.get(key, ["rule_scan"]),
                    )
                    state["exact_evidence"].append(evidence)
                    existing_keys.add(key)

            elif stage == "path_selection":
                bucket_key = (
                    f"{mention['doc_id']}|type:{mention['entity_type']}"
                )
                bucket = state["approx_buckets"].setdefault(
                    bucket_key,
                    {
                        "bucket_key": bucket_key,
                        "doc_id": mention["doc_id"],
                        "type_signature": f"type:{mention['entity_type']}",
                        "mention_ids": [],
                        "retained_pair_keys": [],
                        "filtered_by_window_count": 0,
                    },
                )
                if mention_id not in bucket["mention_ids"]:
                    bucket["mention_ids"].append(mention_id)
                rule = select_rule(book, mention["entity_type"])
                for earlier in mentions:
                    if earlier["order_index"] >= mention["order_index"]:
                        break
                    if earlier["doc_id"] != mention["doc_id"]:
                        continue
                    if earlier["entity_type"] != mention["entity_type"]:
                        continue
                    key = f"{earlier['mention_id']}|{mention_id}"
                    if abs(earlier["sent_id"] - mention["sent_id"]) > rule["sentence_window"]:
                        bucket["filtered_by_window_count"] += 1
                        continue
                    if any(item["pair_key"] == key for item in state["approx_evidence"]):
                        continue
                    origins = ["type_bucket_scan"]
                    seed = seed_by_child.get(mention_id)
                    if seed and seed["pair_key"] == key:
                        origins.append(seed.get("origin", "seed_list"))
                    evidence = _evidence_for_pair(
                        earlier, mention, book, stats, "approx", origins
                    )
                    evidence["bucket"] = {
                        "bucket_key": bucket_key,
                        "type_signature": f"type:{mention['entity_type']}",
                        "filter_basis": (
                            f"same_doc+type:{mention['entity_type']}+"
                            f"sentence_distance<={rule['sentence_window']}"
                        ),
                    }
                    state["approx_evidence"].append(evidence)
                    bucket["retained_pair_keys"].append(key)

            elif stage == "evidence_aggregation":
                child_evidence = [
                    item
                    for item in state["exact_evidence"]
                    if item["mention_b"] == mention_id
                ]
                if child_evidence:
                    from .evidence import candidate_sort_key

                    ordered = sorted(child_evidence, key=candidate_sort_key)
                    state["decisions_progress"].append(
                        {
                            "child_mention_id": mention_id,
                            "processed": True,
                            "candidate_keys": [item["pair_key"] for item in ordered],
                            "winner_key": ordered[0]["pair_key"],
                            "conflict": len(ordered) > 1,
                        }
                    )

            elif stage == "invalidation_propagation":
                if mention_id not in state["coverage_progress"]:
                    state["coverage_progress"].append(mention_id)

            is_last = mention_id == mentions[-1]["mention_id"]
            record = commit_after_mention(stage_index, mention_id, complete=is_last)

            if inject_kill_after is not None and commit_count >= inject_kill_after:
                os._exit(137)

            if inject_signal_after is not None and commit_count >= inject_signal_after:
                os.kill(os.getpid(), signal.SIGTERM)

            if should_stop is not None and should_stop():
                stop_record = checkpoint_store.commit(
                    stage=stage,
                    processed_mention_ids=sorted(processed),
                    completed_doc_ids=sorted(completed_docs),
                    state=state,
                    expected_version=current_version,
                    stop_reason="signal:SIGTERM",
                )
                current_version = int(stop_record["version"])
                checkpoint_store.write_run_status(
                    "signal_stopped", last_signal="SIGTERM", checkpoint_version=current_version
                )
                stopped = True
                break

        if stopped:
            return {
                "stopped": True,
                "latest_version": current_version,
                "state": state,
                "recovery_mode": recovery["mode"],
            }

        if stage == "evidence_aggregation":
            exact_bundle = build_chains(state["exact_evidence"], mentions)
            approx_bundle = build_chains(state["approx_evidence"], mentions)
            state["exact_bundle"] = _bundle_to_state(exact_bundle)
            state["approx_bundle"] = _bundle_to_state(approx_bundle)
            evidence_by_key = {item["pair_key"]: item for item in state["exact_evidence"]}
            approx_by_key = {item["pair_key"]: item for item in state["approx_evidence"]}
            state["traces"] = build_trace_records(
                exact_bundle["chains"], evidence_by_key, approx_by_key
            )
            bundle_record = checkpoint_store.commit(
                stage=stage,
                processed_mention_ids=sorted(processed),
                completed_doc_ids=sorted(completed_docs),
                state=state,
                expected_version=current_version,
            )
            current_version = int(bundle_record["version"])
        if stage == "invalidation_propagation":
            evidence_by_key = {item["pair_key"]: item for item in state["exact_evidence"]}
            state["self_check"] = run_self_checks(
                exact_evidence=state["exact_evidence"],
                approx_evidence=state["approx_evidence"],
                exact_chains=state["exact_bundle"]["chains"],
                approx_chains=state["approx_bundle"]["chains"],
                traces=state["traces"],
                evidence_by_key=evidence_by_key,
            )
            state["replay"] = replay_traces(
                state["traces"], mentions, book, enforce_stale=True
            )
            checkpoint_store.commit(
                stage=stage,
                processed_mention_ids=sorted(processed),
                completed_doc_ids=sorted(completed_docs),
                state=state,
                expected_version=current_version,
            )
            current_version = checkpoint_store.max_committed_version()

    checkpoint_store.write_run_status("complete", latest_version=current_version)
    return {
        "stopped": False,
        "latest_version": current_version,
        "state": state,
        "recovery_mode": recovery["mode"],
    }


def _bundle_to_state(bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "chains": bundle["chains"],
        "singleton_mention_ids": bundle["singleton_mention_ids"],
        "decisions": bundle["decisions"],
        "conflict_decisions": bundle["conflict_decisions"],
        "accepted_count": bundle["accepted_count"],
        "rejected_count": bundle["rejected_count"],
        "winning_edge_count": bundle["winning_edge_count"],
    }


def build_approx_plan_from_state(state: dict[str, Any]) -> dict[str, Any]:
    buckets = list(state.get("approx_buckets", {}).values())
    for bucket in buckets:
        bucket["mention_ids"] = sorted(bucket["mention_ids"])
        bucket["retained_pair_keys"] = sorted(bucket["retained_pair_keys"])
        bucket["retained_pair_count"] = len(bucket["retained_pair_keys"])
        bucket["filter_basis"] = (
            bucket.get("filter_basis")
            or f"same_doc+{bucket['type_signature']}+active_rule_sentence_window"
        )
    buckets.sort(key=lambda item: item["bucket_key"])
    from .evidence import LOW_DISCRIMINATION_FEATURES

    return {
        "path": "approx",
        "bucket_count": len(buckets),
        "buckets": buckets,
        "candidate_count": len(state.get("approx_evidence", [])),
        "pruned_feature_dictionary": [
            {"name": name, "score_weight_bps": 0, "reason": "low_discrimination_zero_score_weight"}
            for name in LOW_DISCRIMINATION_FEATURES
        ],
        "bucket_filter_rule": "same doc_id, exact shared type signature, active rule sentence window",
    }


def run_pipeline(
    data_dir: str | Path,
    rules_source: str | Path | None = None,
    *,
    output_dir: str | Path = "output",
    cache_dir: str | Path = "cache",
    checkpoint_dir: str | Path = "checkpoints",
    inject_kill_after: int | None = None,
    inject_signal_after: int | None = None,
    should_stop: Callable[[], bool] | None = None,
    write_outputs: bool = True,
) -> dict[str, Any]:
    data_dir = Path(data_dir)
    output_dir = Path(output_dir)
    rules_source = Path(rules_source or data_dir / "rules_v2" / "manifest.json")
    mentions = load_mentions(data_dir / "mentions.jsonl")
    seed_pairs = load_seed_candidates(data_dir / "coref_candidates.jsonl", mentions)
    book = read_rule_book(rules_source)
    stats = __import__("entity_link_lab.evidence", fromlist=["document_stats"]).document_stats(
        mentions
    )
    store = CheckpointStore(checkpoint_dir)
    cache = CacheStore(cache_dir)
    run = run_checkpointed_pipeline(
        mentions,
        seed_pairs,
        book,
        store,
        stats=stats,
        inject_kill_after=inject_kill_after,
        inject_signal_after=inject_signal_after,
        should_stop=should_stop,
    )
    if run["stopped"]:
        return run

    state = run["state"]
    exact_evidence = state["exact_evidence"]
    approx_evidence = state["approx_evidence"]
    exact_bundle = state["exact_bundle"]
    approx_bundle = state["approx_bundle"]
    traces = state["traces"]
    evidence_by_key = {item["pair_key"]: item for item in exact_evidence}
    dependency = dependency_hash(mentions, book)

    for evidence in exact_evidence:
        cache.put_current(evidence["cache_key"], internal_hash(book), evidence)
    registry = ChainRegistry()
    for chain in exact_bundle["chains"]:
        registry.register(chain)
        key = closure_cache_key(chain, internal_hash(book))
        cache.put_current(
            key,
            internal_hash(book),
            {"chain": chain, "trace_keys": [t["pair_key"] for t in traces if t["chain_id"] == chain["chain_id"]]},
        )
    for trace in traces:
        trace["dependency_hash"] = dependency
        trace["cache_served"] = False

    replay = replay_traces(traces, mentions, book)
    self_check = run_self_checks(
        exact_evidence=exact_evidence,
        approx_evidence=approx_evidence,
        exact_chains=exact_bundle["chains"],
        approx_chains=approx_bundle["chains"],
        traces=traces,
        evidence_by_key=evidence_by_key,
    )
    approx_plan = build_approx_plan_from_state(state)
    input_checksum = content_hash(
        {
            "mentions": mentions,
            "seed_pairs": seed_pairs,
            "rule_internal_hash": internal_hash(book),
        }
    )
    report = {
        "status": "ok",
        "seed": 20261127,
        "input_checksum": input_checksum,
        "dependency_hash": dependency,
        "rule_hash": internal_hash(book),
        "rule_snapshot_id": book["snapshot_id"],
        "recovery_mode": run["recovery_mode"],
        "checkpoint_version": run["latest_version"],
        "counts": {
            "mentions": len(mentions),
            "seed_pairs": len(seed_pairs),
            "exact_candidates": len(exact_evidence),
            "approx_candidates": len(approx_evidence),
            "chains": len(exact_bundle["chains"]),
            "conflicts": len(exact_bundle["conflict_decisions"]),
            "traces": len(traces),
        },
        "chains": exact_bundle["chains"],
        "conflict_decisions": exact_bundle["conflict_decisions"],
        "decisions": exact_bundle["decisions"],
        "traces": traces,
        "self_check": self_check,
        "replay": {key: value for key, value in replay.items() if key != "details"},
        "approx_plan": approx_plan,
        "exact_evidence": exact_evidence,
        "approx_evidence": approx_evidence,
        "singleton_mention_ids": exact_bundle["singleton_mention_ids"],
        "checks": self_check["checks"],
    }
    if write_outputs:
        output_dir.mkdir(parents=True, exist_ok=True)
        write_jsonl(output_dir / "candidates_exact.jsonl", exact_evidence)
        write_jsonl(output_dir / "candidates_approx.jsonl", approx_evidence)
        write_json(output_dir / "approx_plan.json", approx_plan)
        write_json(output_dir / "chains.json", exact_bundle)
        write_json(output_dir / "traces.json", traces)
        write_json(output_dir / "self_check.json", self_check)
        write_json(output_dir / "replay.json", replay)
        write_json(output_dir / "report.json", report)
    return report
