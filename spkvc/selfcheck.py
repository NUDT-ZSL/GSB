"""Cross-module consistency checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .dsp import synthesize_pcm
from .models import artifact_hash, sha256_bytes

if TYPE_CHECKING:
    from .pipeline import ConversionEngine, ConversionResult


def run_self_check(
    engine: "ConversionEngine", result: "ConversionResult | None"
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    if result is None:
        checks.append(
            {
                "name": "result_available",
                "ok": False,
                "detail": "需要先完成一次转换或增量运行",
            }
        )
        return {"ok": False, "checks": checks}

    state = engine.snapshots.load()
    expected_ids = sorted(result.utterance_ids)
    stage_sets = {
        entry.stage: sorted(entry.completed_utterance_ids) for entry in state.entries
    }
    stage_ok = (
        len(state.entries) == 5
        and all(ids == expected_ids for ids in stage_sets.values())
    )
    checks.append(
        {
            "name": "stage_utterance_sets_consistent",
            "ok": stage_ok,
            "detail": stage_sets,
        }
    )

    recomputed = set(result.recomputed_nodes)
    reused = set(result.reused_nodes)
    affected = set(result.affected_nodes)
    disjoint = not bool(recomputed & reused)
    covers_affected = affected.issubset(recomputed | reused)
    checks.append(
        {
            "name": "recompute_reuse_disjoint",
            "ok": disjoint,
            "detail": {
                "intersection": sorted(recomputed & reused),
                "recomputed_count": len(recomputed),
                "reused_count": len(reused),
            },
        }
    )
    checks.append(
        {
            "name": "recompute_reuse_cover_affected",
            "ok": covers_affected,
            "detail": {
                "affected_count": len(affected),
                "missing": sorted(affected - (recomputed | reused)),
            },
        }
    )

    params_payload = engine._snapshot_stage("params")[0]
    synth_payload = engine._snapshot_stage("synthesis")[0]
    pcm_checks: dict[str, Any] = {}
    pcm_ok = True
    for utterance_id in expected_ids:
        params = params_payload[utterance_id]
        independent = synthesize_pcm(params, engine.config)
        independent_hash = sha256_bytes(independent.astype("<i2", copy=False).tobytes())
        stored = synth_payload[utterance_id]
        result_hash = sha256_bytes(
            result.pcm_by_utterance[utterance_id]
            .astype("<i2", copy=False)
            .tobytes()
        )
        match = (
            independent_hash == stored.get("pcm_sha256") == result_hash
        )
        if not match:
            pcm_ok = False
        pcm_checks[utterance_id] = {
            "independent_pcm_sha256": independent_hash,
            "snapshot_pcm_sha256": stored.get("pcm_sha256"),
            "result_pcm_sha256": result_hash,
            "match": match,
        }
    checks.append(
        {
            "name": "pcm_independently_recomputable_from_params",
            "ok": pcm_ok,
            "detail": pcm_checks,
        }
    )

    key_required = {
        "features": {"audio_chunk_sha256", "frame_ms", "hop_ms"},
        "timbre": {
            "audio_chunk_sha256",
            "ref_audio_chunk_sha256",
            "ref_revision",
            "frame_ms",
            "hop_ms",
        },
        "params": {
            "audio_chunk_sha256",
            "ref_revision",
            "frame_ms",
            "hop_ms",
            "mapping_version",
            "upstream_artifact_hashes",
        },
        "synthesis": {
            "audio_chunk_sha256",
            "ref_revision",
            "frame_ms",
            "hop_ms",
            "mapping_version",
            "upstream_artifact_hashes",
        },
    }
    key_ok = True
    key_detail: dict[str, Any] = {}
    for utterance_id, nodes in result.node_records.items():
        for node, required in key_required.items():
            components = nodes.get(node, {}).get("key_components", {})
            missing = sorted(required - set(components))
            if missing:
                key_ok = False
                key_detail[f"{node}:{utterance_id}"] = missing
    checks.append(
        {
            "name": "cache_keys_include_required_inputs",
            "ok": key_ok,
            "detail": key_detail,
        }
    )

    artifact_ok = True
    artifact_detail: dict[str, Any] = {}
    for utterance_id, nodes in result.node_records.items():
        for node, record in nodes.items():
            cache_path = record.get("cache_path")
            if not cache_path:
                continue
            try:
                stored = json.loads(Path(cache_path).read_text(encoding="utf-8"))
                valid = artifact_hash(stored["artifact"]) == record["artifact_hash"]
            except Exception:
                valid = False
            if not valid:
                artifact_ok = False
                artifact_detail[f"{node}:{utterance_id}"] = "cache artifact hash mismatch"
    checks.append(
        {
            "name": "cache_artifact_hashes_valid",
            "ok": artifact_ok,
            "detail": artifact_detail,
        }
    )
    return {
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
    }

