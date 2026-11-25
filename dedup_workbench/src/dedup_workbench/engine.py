"""Deterministic multi-stage dedup graph executor and incremental planner."""
from __future__ import annotations

import json
import os
import signal
import tempfile
from pathlib import Path
from typing import Any

from .cache import ContentAddressedCache
from .canonical import canonical_bytes, hash_json
from .candidates import candidate_cache_inputs, generate_candidates
from .checkpoint import BatchCheckpointStore, ForcedTermination
from .compare import comparison_cache_inputs, compare_pair
from .config import parse_config, stage_parameters
from .decide import build_candidate_component_decisions, candidate_components, decision_cache_inputs
from .errors import WorkbenchError
from .ingest import load_documents
from .query import build_doc_record
from .selfcheck import run_self_check
from .signatures import build_signature, signature_cache_inputs
from .snapshots import SnapshotStore


class DedupWorkbench:
    def __init__(self, workdir: str | Path, config: dict[str, Any] | str | Path) -> None:
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.config = parse_config(config)
        self.cache = ContentAddressedCache(
            self.workdir / "cas",
            max_memory_bytes=int(self.config["limits"]["max_cache_memory_bytes"]),
            overflow_policy=self.config["limits"]["cache_overflow_policy"],
        )
        self.snapshots = SnapshotStore(self.workdir / "snapshots")
        self.checkpoints = BatchCheckpointStore(self.workdir / "checkpoints" / "batch_executor.json")
        self.export_dir = self.workdir / "exports"
        self.state_path = self.workdir / "state" / "run_manifest.json"
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.doc_records_path = self.export_dir / "doc_records.jsonl"

    def _load_manifest(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"doc_state": {}, "run_count": 0}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".manifest.", suffix=".tmp", dir=self.state_path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_bytes(manifest))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.state_path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def commit_config_snapshot(self) -> tuple[dict[str, Any], bool]:
        path = self.snapshots.path_for("config")
        if path.exists():
            current = self.snapshots.load("config")
            if hash_json(current["data"]) == hash_json(self.config):
                return current, False
        return self.snapshots.commit("config", self.config), True

    def plan_invalidation(self, documents: list[dict[str, Any]], previous: dict[str, Any]) -> dict[str, Any]:
        old_config = previous.get("config")
        old_docs = previous.get("doc_state", {})
        current_ids = [doc["doc_id"] for doc in documents]
        current_by_id = {doc["doc_id"]: doc for doc in documents}

        new_docs = [doc_id for doc_id in current_ids if doc_id not in old_docs]
        changed_docs = [
            doc_id
            for doc_id in current_ids
            if doc_id in old_docs and old_docs[doc_id].get("input_hash") != current_by_id[doc_id]["input_hash"]
        ]
        deleted_docs = sorted(doc_id for doc_id in old_docs if doc_id not in current_by_id)

        changed_fields: list[str] = []
        invalidated_nodes: set[str] = set()
        if not old_config:
            changed_fields.append("$all")
            invalidated_nodes.update({"ingest", "signature", "candidate", "comparison", "decision", "aggregate"})
        else:
            field_to_stage = {
                "chunk_signature_length": "signature",
                "candidate_window": "candidate",
                "bucket_strategy": "candidate",
                "bucket_prefix_chars": "candidate",
                "validation_mode": "comparison",
                "similarity_threshold": "decision",
            }
            for field, stage in field_to_stage.items():
                if old_config.get(field) != self.config.get(field):
                    changed_fields.append(field)
                    if stage == "signature":
                        invalidated_nodes.update({"signature", "candidate", "comparison", "decision", "aggregate"})
                    elif stage == "candidate":
                        invalidated_nodes.update({"candidate", "comparison", "decision", "aggregate"})
                    elif stage == "comparison":
                        invalidated_nodes.update({"comparison", "decision", "aggregate"})
                    elif stage == "decision":
                        invalidated_nodes.update({"decision", "aggregate"})
            if old_config.get("config_version") != self.config.get("config_version") and not changed_fields:
                changed_fields.append("config_version")
                invalidated_nodes.update({"signature", "candidate", "comparison", "decision", "aggregate"})
            if changed_docs or new_docs or deleted_docs:
                invalidated_nodes.update({"signature", "candidate", "comparison", "decision", "aggregate"})

        return {
            "changed_config_fields": changed_fields,
            "new_docs": sorted(new_docs),
            "changed_docs": sorted(changed_docs),
            "deleted_docs": deleted_docs,
            "seed_docs": sorted(set(new_docs) | set(changed_docs)),
            "planned_invalidated_nodes": sorted(invalidated_nodes),
        }

    def _resolve_stage_versions(self, previous_manifest: dict[str, Any]) -> dict[str, int]:
        stages = ("signature", "candidate", "comparison", "decision")
        previous_config = previous_manifest.get("config") or {}
        previous_versions = previous_config.get("stage_versions") or {}
        versions = {
            stage: int(previous_versions.get(stage, self.config.get("stage_versions", {}).get(stage, 1)))
            for stage in stages
        }

        requested_versions = self.config.get("stage_versions") or {}
        if requested_versions and not previous_config:
            versions.update({stage: int(version) for stage, version in requested_versions.items() if stage in stages})
            self.config["stage_versions"] = versions
            return versions

        if previous_config:
            field_to_stage = {
                "chunk_signature_length": "signature",
                "candidate_window": "candidate",
                "bucket_strategy": "candidate",
                "bucket_anchor_count": "candidate",
                "bucket_prefix_chars": "candidate",
                "validation_mode": "comparison",
                "similarity_threshold": "decision",
            }
            affected_stages = {
                stage
                for field, stage in field_to_stage.items()
                if previous_config.get(field) != self.config.get(field)
            }
            top_version_changed = previous_config.get("config_version") != self.config.get("config_version")
            if top_version_changed and not affected_stages:
                affected_stages.update(stages)
            for stage in affected_stages:
                # Field changes are already part of the stage key and therefore
                # invalidate exactly the relevant artifacts.  A version-only
                # bump is an explicit conservative invalidation.
                if top_version_changed and not any(
                    previous_config.get(field) != self.config.get(field)
                    for field, mapped_stage in field_to_stage.items()
                    if mapped_stage == stage
                ):
                    versions[stage] += 1

        if requested_versions:
            for stage, version in requested_versions.items():
                if stage in versions:
                    versions[stage] = max(versions[stage], int(version))

        self.config["stage_versions"] = versions
        return versions

    def run(
        self,
        input_source: str | Path,
        *,
        crash_after_batch: int | None = None,
        kill_after_batch: int | None = None,
        run_self_check_flag: bool = True,
    ) -> dict[str, Any]:
        previous_manifest = self._load_manifest()
        self._resolve_stage_versions(previous_manifest)
        documents = load_documents(input_source, self.config)
        plan = self.plan_invalidation(documents, previous_manifest)
        ingest_key_inputs = {
            "document_input_hashes": {doc["doc_id"]: doc["input_hash"] for doc in documents},
            "content_hashes": {doc["doc_id"]: doc["content_hash"] for doc in documents},
            "normalization_version": 1,
            "allowed_langs": list(self.config["allowed_langs"]),
        }
        _ingest_artifact, ingest_cache_key, ingest_hit = self.cache.get_or_compute(
            "ingest_validation",
            ingest_key_inputs,
            lambda: {
                "document_count": len(documents),
                "doc_ids": [doc["doc_id"] for doc in documents],
                "documents": documents,
            },
            priority=10,
        )
        config_snapshot, config_snapshot_changed = self.commit_config_snapshot()

        batch_size = int(self.config["limits"]["batch_size"])
        batches = [documents[index : index + batch_size] for index in range(0, len(documents), batch_size)]
        content_hashes = {doc["doc_id"]: doc["content_hash"] for doc in documents}
        run_fingerprint = hash_json(
            {
                "content_hashes": dict(sorted(content_hashes.items())),
                "signature_parameters": stage_parameters(self.config, "signature"),
                "batch_size": batch_size,
            }
        )
        checkpoint = self.checkpoints.ensure_for_run(
            run_fingerprint=run_fingerprint,
            total_batches=len(batches),
            batch_size=batch_size,
        )

        signatures: list[dict[str, Any]] = []
        signature_keys: dict[str, str] = {}
        signature_hashes: dict[str, str] = {}
        signature_hits: dict[str, bool] = {}
        executed_batches: list[int] = []
        skipped_batches: list[int] = []

        for batch_index, batch in enumerate(batches):
            if batch_index in checkpoint.get("committed_batch_indexes", []):
                skipped_batches.append(batch_index)
                for document in batch:
                    payload, cache_key, cache_hit = self.cache.get_or_compute(
                        "signature",
                        signature_cache_inputs(document, self.config),
                        lambda document=document: build_signature(document, self.config),
                        priority=9,
                    )
                    signatures.append(payload)
                    signature_keys[document["doc_id"]] = cache_key
                    signature_hashes[document["doc_id"]] = hash_json(payload)
                    signature_hits[document["doc_id"]] = cache_hit
                continue

            executed_batches.append(batch_index)
            batch_hashes: list[str] = []
            for document in batch:
                payload, cache_key, cache_hit = self.cache.get_or_compute(
                    "signature",
                    signature_cache_inputs(document, self.config),
                    lambda document=document: build_signature(document, self.config),
                    priority=9,
                )
                signatures.append(payload)
                signature_keys[document["doc_id"]] = cache_key
                signature_hashes[document["doc_id"]] = hash_json(payload)
                signature_hits[document["doc_id"]] = cache_hit
                batch_hashes.append(signature_hashes[document["doc_id"]])

            batch_hash = hash_json({"batch_index": batch_index, "doc_ids": [doc["doc_id"] for doc in batch], "hashes": batch_hashes})
            checkpoint = self.checkpoints.commit_batch(checkpoint, batch_index, batch_hash)
            if crash_after_batch == batch_index:
                raise ForcedTermination(f"Simulated forced termination immediately after committed batch {batch_index}")
            if kill_after_batch == batch_index:
                os.kill(os.getpid(), signal.SIGKILL)

        signatures.sort(key=lambda item: (item["shard_id"], item["doc_id"]))
        signature_by_id = {item["doc_id"]: item for item in signatures}

        candidate_key_inputs = candidate_cache_inputs(signatures, self.config, signature_hashes)
        candidates, candidate_cache_key, candidate_hit = self.cache.get_or_compute(
            "candidate_set",
            candidate_key_inputs,
            lambda: generate_candidates(signatures, self.config),
            priority=7,
        )

        comparisons: list[dict[str, Any]] = []
        comparison_hashes: dict[str, str] = {}
        comparison_hits: dict[str, bool] = {}
        for pair in candidates["pairs"]:
            doc_a = pair["doc_id_a"]
            doc_b = pair["doc_id_b"]
            payload, _cache_key, cache_hit = self.cache.get_or_compute(
                "pair_comparison",
                comparison_cache_inputs(pair, signature_by_id[doc_a], signature_by_id[doc_b], self.config),
                lambda pair=pair: compare_pair(pair, signature_by_id, self.config),
                priority=4,
            )
            comparisons.append(payload)
            comparison_hashes[pair["pair_id"]] = hash_json(payload)
            comparison_hits[pair["pair_id"]] = cache_hit

        doc_ids = [doc["doc_id"] for doc in documents]
        components = candidate_components(candidates, doc_ids)
        decisions: dict[str, Any] = {}
        removed: list[dict[str, Any]] = []
        representatives: list[dict[str, Any]] = []
        component_records: list[dict[str, Any]] = []
        qualifying_pair_count = 0
        decision_hits: dict[str, bool] = {}
        decision_hashes: dict[str, str] = {}

        comparisons_by_pair = {item["pair_id"]: item for item in comparisons}
        for component_id, member_ids in components.items():
            member_set = set(member_ids)
            component_pair_hashes = {
                pair_id: artifact_hash
                for pair_id, artifact_hash in comparison_hashes.items()
                if pair_id in comparisons_by_pair
                and comparisons_by_pair[pair_id]["doc_id_a"] in member_set
                and comparisons_by_pair[pair_id]["doc_id_b"] in member_set
            }
            key_inputs = decision_cache_inputs(
                component_id,
                member_ids,
                component_pair_hashes,
                documents,
                self.config,
            )
            if not component_pair_hashes:
                key_inputs["decision_parameters"] = {
                    "decision_policy_version": key_inputs["decision_parameters"]["decision_policy_version"],
                    "config_version": key_inputs["decision_parameters"]["config_version"],
                    "threshold_relevant": False,
                }
            component_payload, _key, cache_hit = self.cache.get_or_compute(
                "decision_component",
                key_inputs,
                lambda component_id=component_id, member_ids=member_ids: build_candidate_component_decisions(
                    component_id,
                    member_ids,
                    comparisons,
                    documents,
                    self.config,
                ),
                priority=6,
            )
            decision_hits[component_id] = cache_hit
            decision_hashes[component_id] = hash_json(component_payload)
            decisions.update(component_payload["decisions"])
            removed.extend(component_payload["removed"])
            representatives.extend(component_payload["representatives"])
            component_records.extend(component_payload["duplicate_components"])
            qualifying_pair_count += int(component_payload["qualifying_pair_count"])

        removed.sort(key=lambda item: item["doc_id"])
        representatives.sort(key=lambda item: item["representative_doc_id"])
        component_records.sort(key=lambda item: item["component_id"])
        decision_bundle = {
            "threshold": self.config["similarity_threshold"],
            "threshold_exact": f"{threshold_value_numerator(self.config)}/{threshold_value_denominator(self.config)}",
            "decision_count": len(decisions),
            "decisions": dict(sorted(decisions.items())),
            "removed": removed,
            "removed_doc_ids": [item["doc_id"] for item in removed],
            "representatives": representatives,
            "duplicate_components": component_records,
            "qualifying_pair_count": qualifying_pair_count,
        }

        doc_records: list[dict[str, Any]] = []
        raw_record_bytes: dict[str, bytes] = {}
        for document in documents:
            doc_id = document["doc_id"]
            record = build_doc_record(
                document,
                signature_by_id[doc_id],
                candidates["doc_pair_index"],
                comparisons_by_pair,
                decisions[doc_id],
            )
            doc_records.append(record)
            raw = canonical_bytes(record)
            raw_record_bytes[doc_id] = raw

        self._write_doc_records_jsonl(doc_records)

        incremental = self._build_incremental_summary(
            documents=documents,
            previous_manifest=previous_manifest,
            signature_hits=signature_hits,
            comparison_hits=comparison_hits,
            decision_hits=decision_hits,
            decisions_by_doc={doc_id: decisions[doc_id]["candidate_component_id"] for doc_id in decisions},
            raw_record_bytes=raw_record_bytes,
            plan=plan,
            candidate_hit=candidate_hit,
            ingest_hit=ingest_hit,
        )

        stats = self._build_stats(
            documents=documents,
            signatures=signatures,
            candidates=candidates,
            comparisons=comparisons,
            decision_bundle=decision_bundle,
            incremental=incremental,
            checkpoint=checkpoint,
            executed_batches=executed_batches,
            skipped_batches=skipped_batches,
            config_snapshot_changed=config_snapshot_changed,
        )
        doc_record_hashes = {record["doc_id"]: hash_json(record) for record in doc_records}
        _aggregate_artifact, aggregate_cache_key, aggregate_hit = self.cache.get_or_compute(
            "aggregate_export",
            {
                "stats_hash": hash_json(stats),
                "doc_record_hashes": dict(sorted(doc_record_hashes.items())),
                "incremental_hash": hash_json(incremental),
                "aggregate_policy_version": "canonical-jsonl-v1",
            },
            lambda: {
                "stats": stats,
                "doc_record_hashes": dict(sorted(doc_record_hashes.items())),
                "incremental": incremental,
            },
            priority=8,
        )
        stats_snapshot, stats_changed = self.commit_content_snapshot("stats", stats)
        removal_snapshot, removals_changed = self.commit_content_snapshot(
            "removals",
            {"removed_doc_ids": decision_bundle["removed_doc_ids"], "removed": decision_bundle["removed"]},
        )

        report = {
            "ok": True,
            "config": self.config,
            "config_snapshot": {"version": config_snapshot["version"], "changed": config_snapshot_changed},
            "stats_snapshot": {"version": stats_snapshot["version"], "changed": stats_changed},
            "removal_snapshot": {"version": removal_snapshot["version"], "changed": removals_changed},
            "aggregate_cache": {"cache_key": aggregate_cache_key, "hit": aggregate_hit},
            "documents": documents,
            "signatures": signatures,
            "candidates": candidates,
            "comparisons": comparisons,
            "decisions": decision_bundle,
            "doc_records": doc_records,
            "doc_record_raw_bytes": raw_record_bytes,
            "incremental": incremental,
            "stats": stats,
            "checkpoint": {
                "path": str(self.checkpoints.path),
                "executed_batches": executed_batches,
                "skipped_committed_batches": skipped_batches,
                "committed_batch_indexes": checkpoint["committed_batch_indexes"],
            },
        }

        if run_self_check_flag:
            report["self_check"] = run_self_check(report)

        self._write_manifest(
            {
                "schema": "dedup-workbench/run-manifest/v1",
                "run_count": int(previous_manifest.get("run_count", 0)) + 1,
                "config": json.loads(canonical_bytes(self.config).decode("utf-8")),
                "doc_state": {
                    doc["doc_id"]: {
                        "input_hash": doc["input_hash"],
                        "content_hash": doc["content_hash"],
                        "signature_cache_key": signature_keys[doc["doc_id"]],
                        "signature_hash": signature_hashes[doc["doc_id"]],
                        "doc_record_hash": hash_json(next(record for record in doc_records if record["doc_id"] == doc["doc_id"])),
                        "component_id": decisions[doc["doc_id"]]["component_id"],
                    }
                    for doc in documents
                },
                "incremental": incremental,
            }
        )
        self.checkpoints.complete(checkpoint)
        return report

    def commit_content_snapshot(self, kind: str, data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        path = self.snapshots.path_for(kind)
        if path.exists():
            current = self.snapshots.load(kind)
            if hash_json(current["data"]) == hash_json(data):
                return current, False
        return self.snapshots.commit(kind, data), True

    def _write_doc_records_jsonl(self, records: list[dict[str, Any]]) -> None:
        payload = b"".join(canonical_bytes(record) + b"\n" for record in sorted(records, key=lambda item: item["doc_id"]))
        descriptor, temporary_name = tempfile.mkstemp(prefix=".doc-records.", suffix=".jsonl.tmp", dir=self.doc_records_path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.doc_records_path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _build_incremental_summary(
        self,
        *,
        documents: list[dict[str, Any]],
        previous_manifest: dict[str, Any],
        signature_hits: dict[str, bool],
        comparison_hits: dict[str, bool],
        decision_hits: dict[str, bool],
        decisions_by_doc: dict[str, str],
        raw_record_bytes: dict[str, bytes],
        plan: dict[str, Any],
        candidate_hit: bool,
        ingest_hit: bool,
    ) -> dict[str, Any]:
        old_doc_state = previous_manifest.get("doc_state", {})
        recomputed_docs: list[str] = []
        reused_docs: list[str] = []
        signature_recomputed = sorted(doc_id for doc_id, hit in signature_hits.items() if not hit)
        signature_reused = sorted(doc_id for doc_id, hit in signature_hits.items() if hit)
        comparison_recomputed = sorted(pair_id for pair_id, hit in comparison_hits.items() if not hit)
        comparison_reused = sorted(pair_id for pair_id, hit in comparison_hits.items() if hit)
        decision_recomputed = sorted(component_id for component_id, hit in decision_hits.items() if not hit)
        decision_reused = sorted(component_id for component_id, hit in decision_hits.items() if hit)

        pairs_by_doc: dict[str, set[str]] = {doc["doc_id"]: set() for doc in documents}
        for pair_id in comparison_hits:
            left, right = pair_id.split("⇔", 1)
            pairs_by_doc.setdefault(left, set()).add(pair_id)
            pairs_by_doc.setdefault(right, set()).add(pair_id)

        for doc in documents:
            doc_id = doc["doc_id"]
            component_id = decisions_by_doc[doc_id]
            cache_recomputed = (
                not signature_hits[doc_id]
                or any(not comparison_hits[pair_id] for pair_id in pairs_by_doc[doc_id])
                or not decision_hits[component_id]
            )
            previous_hash = old_doc_state.get(doc_id, {}).get("doc_record_hash")
            record_changed = previous_hash is None or previous_hash != hash_json(json.loads(raw_record_bytes[doc_id].decode("utf-8")))
            if cache_recomputed or record_changed:
                recomputed_docs.append(doc_id)
            else:
                reused_docs.append(doc_id)

        node_status = {
            "ingest_validation": "reused" if ingest_hit else "recomputed",
            "signature": "reused" if not signature_recomputed else "recomputed",
            "candidate_set": "reused" if candidate_hit else "recomputed",
            "pair_comparison": "reused" if not comparison_recomputed else "recomputed",
            "decision_component": "reused" if not decision_recomputed else "recomputed",
            "aggregate_export": "recomputed" if recomputed_docs else "reused",
        }
        recomputed_nodes = sorted(name for name, status in node_status.items() if status == "recomputed")
        reused_nodes = sorted(name for name, status in node_status.items() if status == "reused")
        revalidated_nodes: list[str] = []

        return {
            **plan,
            "recomputed_docs": sorted(recomputed_docs),
            "reused_docs": sorted(reused_docs),
            "signature_recomputed_docs": signature_recomputed,
            "signature_reused_docs": signature_reused,
            "comparison_recomputed_pairs": comparison_recomputed,
            "comparison_reused_pairs": comparison_reused,
            "decision_recomputed_components": decision_recomputed,
            "decision_reused_components": decision_reused,
            "node_status": node_status,
            "recomputed_nodes": recomputed_nodes,
            "reused_nodes": reused_nodes,
            "revalidated_nodes": revalidated_nodes,
            "disjoint": not set(recomputed_docs) & set(reused_docs),
            "covers_all_docs": set(recomputed_docs) | set(reused_docs) == {doc["doc_id"] for doc in documents},
        }

    def _build_stats(
 self,
        *,
        documents: list[dict[str, Any]],
        signatures: list[dict[str, Any]],
        candidates: dict[str, Any],
        comparisons: list[dict[str, Any]],
        decision_bundle: dict[str, Any],
        incremental: dict[str, Any],
        checkpoint: dict[str, Any],
        executed_batches: list[int],
        skipped_batches: list[int],
        config_snapshot_changed: bool,
    ) -> dict[str, Any]:
        lang_counts: dict[str, int] = {}
        source_counts: dict[str, int] = {}
        shard_counts: dict[str, int] = {}
        for doc in documents:
            lang_counts[doc["lang"]] = lang_counts.get(doc["lang"], 0) + 1
            source_counts[doc["source"]] = source_counts.get(doc["source"], 0) + 1
            shard_counts[doc["shard_id"]] = shard_counts.get(doc["shard_id"], 0) + 1
        return {
            "schema": "dedup-workbench/stats/v1",
            "document_count": len(documents),
            "shard_count": len(shard_counts),
            "shard_counts": dict(sorted(shard_counts.items())),
            "lang_counts": dict(sorted(lang_counts.items())),
            "source_counts": dict(sorted(source_counts.items())),
            "block_count": sum(item["block_count"] for item in signatures),
            "candidate_bucket_count": candidates["bucket_count"],
            "candidate_pair_count": candidates["pair_count"],
            "comparison_count": len(comparisons),
            "qualifying_pair_count": decision_bundle["qualifying_pair_count"],
            "duplicate_component_count": len(decision_bundle["duplicate_components"]),
            "removed_count": len(decision_bundle["removed_doc_ids"]),
            "removed_doc_ids": decision_bundle["removed_doc_ids"],
            "representative_count": len(decision_bundle["representatives"]),
            "cache": self.cache.stats(),
            "incremental": {
                "recomputed_doc_count": len(incremental["recomputed_docs"]),
                "reused_doc_count": len(incremental["reused_docs"]),
                "comparison_recomputed_pair_count": len(incremental["comparison_recomputed_pairs"]),
                "comparison_reused_pair_count": len(incremental["comparison_reused_pairs"]),
                "recomputed_nodes": incremental["recomputed_nodes"],
            },
            "checkpoint": {
                "executed_batches": executed_batches,
                "skipped_committed_batches": skipped_batches,
                "committed_batch_indexes": checkpoint["committed_batch_indexes"],
                "status": checkpoint["status"],
            },
            "config_snapshot_changed": config_snapshot_changed,
        }


def threshold_value_numerator(config: dict[str, Any]) -> int:
    from fractions import Fraction

    return Fraction(str(config["similarity_threshold"])).numerator


def threshold_value_denominator(config: dict[str, Any]) -> int:
    from fractions import Fraction

    return Fraction(str(config["similarity_threshold"])).denominator
