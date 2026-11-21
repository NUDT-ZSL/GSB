"""Deterministic local sample generation (NumPy-seeded, fully offline)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from .formats import (render_v1_body, render_v2_body, render_v3_body,
                      write_result_file)
from .hashing import content_hash

# Fixed random seed required by the task environment.
SAMPLE_SEED = 20240521

TOPICS = ["billing", "latency", "inventory", "refund", "routing"]
STATUSES = ["OK", "EMPTY", "THROTTLED", "ERROR"]


def _arg_digest(rng: np.random.Generator, token: str) -> str:
    return "arg" + content_hash({"seed": int(rng.integers(0, 1_000_000)),
                                 "token": token})[:16]


def _payload(rng: np.random.Generator, topic: str, index: int) -> Dict[str, Any]:
    return {
        "topic": topic,
        "text": f"{topic}-result-{index}:value-"
                f"{int(rng.integers(100, 9999))}",
        "metric": round(float(rng.uniform(0.1, 0.95)), 6),
    }


def _confidence(rng: np.random.Generator, status: str) -> List[float]:
    point = {"OK": 0.9, "EMPTY": 0.55, "THROTTLED": 0.4,
             "ERROR": 0.25}[status] + float(rng.uniform(-0.05, 0.05))
    margin = 0.02
    return [round(max(0.0, point - margin), 6),
            round(min(1.0, point + margin), 6)]


def _v3_payload(tool_id: str, payload: Dict[str, Any], status: str,
                confidence: List[float], result_ref: str,
                schema: str, shard_count: int,
                rng: np.random.Generator) -> Dict[str, Any]:
    shards = [
        {"shard_id": f"{tool_id}-shard-{order}", "order": order,
         "text": f"{payload['text']}#part{order}",
         "source": "native"}
        for order in range(shard_count)
    ]
    aggregation = {
        "policy": "merge_first",
        "member_count": shard_count,
        "member_refs": [item["shard_id"] for item in shards],
        "rollup_metric": round(float(rng.uniform(0.2, 0.9)), 6),
    }
    lineage = [
        {"node_id": f"{tool_id}:src:0", "source": "tool-execution",
         "detail": "raw tool execution record", "upstream": []},
        {"node_id": f"{tool_id}:src:1", "source": "warehouse-ingest",
         "detail": "ingested into result repository",
         "upstream": [f"{tool_id}:src:0"]},
    ]
    return {
        "format_version": 3,
        "result_ref": result_ref,
        "tool_id": tool_id,
        "payload": payload,
        "status": status,
        "result_schema_version": schema,
        "confidence": confidence,
        "shards": shards,
        "aggregation": aggregation,
        "lineage": lineage,
    }


def generate_workspace(base: Path, request_count: int = 4,
                       calls_per_request: int = 3) -> Dict[str, Any]:
    """Generate the manifest, tri-format repository and format definitions.

    Every logical result is rendered in all three versions under
    ``results/<tool_id>/``. The manifest references each result through a
    specific version so the pipeline exercises real cross-format reads.
    """
    base = Path(base)
    results_root = base / "results"
    manifest_path = base / "requests.jsonl"
    format_defs_path = base / "format_definitions.json"
    results_root.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(SAMPLE_SEED)
    manifest: List[Dict[str, Any]] = []
    logical_index = 0

    for request_index in range(request_count):
        request_id = f"req-{request_index:03d}"
        tool_calls: List[Dict[str, Any]] = []
        for call_index in range(calls_per_request):
            topic = TOPICS[int(rng.integers(0, len(TOPICS)))]
            status = STATUSES[int(rng.integers(0, len(STATUSES)))]
            tool_id = f"tool_{topic}_{logical_index:02d}"
            result_ref = f"{tool_id}@logical"
            payload = _payload(rng, topic, logical_index)
            confidence = _confidence(rng, status)
            schema = f"schema-2.{logical_index % 3}.0"
            shard_count = int(rng.integers(1, 4))
            arg_digest = _arg_digest(rng, tool_id)

            result_dir = results_root / tool_id
            result_dir.mkdir(parents=True, exist_ok=True)
            v1_body = render_v1_body(tool_id, payload, status)
            v2_body = render_v2_body({
                "result_ref": result_ref, "tool_id": tool_id,
                "payload": payload, "status": status,
                "result_schema_version": schema,
                "confidence": confidence})
            v3_obj = _v3_payload(tool_id, payload, status, confidence,
                                 result_ref, schema, shard_count, rng)
            v3_body = render_v3_body(v3_obj)
            write_result_file(result_dir / "v1.txt", 1, v1_body)
            write_result_file(result_dir / "v2.txt", 2, v2_body)
            write_result_file(result_dir / "v3.txt", 3, v3_body)

            pinned_version = (logical_index % 3) + 1
            tool_calls.append({
                "tool_id": tool_id,
                "arg_digest": arg_digest,
                "result_ref": result_ref,
                "result_format_version": pinned_version,
            })
            logical_index += 1

        manifest.append({
            "request_id": request_id,
            "turn_no": request_index + 1,
            "query_text": f"diagnostic question {request_index} over "
                          f"{calls_per_request} tool calls",
            "tool_calls": tool_calls,
        })

    manifest_path.write_text(
        "\n".join(json.dumps(entry, sort_keys=True, ensure_ascii=False)
                  for entry in manifest) + "\n", encoding="utf-8")

    format_definitions = {
        "definitions_version": 1,
        "versions": {
            "1": {"layout": "pipe", "fields": ["tool_id", "payload", "status"],
                  "named_fields": False, "supports_shards": False,
                  "supports_lineage": False},
            "2": {"layout": "named-pipe",
                  "fields": ["result_ref", "tool_id", "payload", "status",
                             "result_schema_version", "confidence"],
                  "named_fields": True, "supports_shards": False,
                  "supports_lineage": False},
            "3": {"layout": "json",
                  "fields": ["result_ref", "tool_id", "payload", "status",
                             "result_schema_version", "confidence",
                             "shards", "aggregation", "lineage"],
                  "named_fields": True, "supports_shards": True,
                  "supports_lineage": True},
        },
        "controlled_statuses": STATUSES,
    }
    format_defs_path.write_text(
        json.dumps(format_definitions, sort_keys=True, indent=2,
                   ensure_ascii=False), encoding="utf-8")

    return {"manifest": manifest_path, "results": results_root,
            "format_definitions": format_defs_path,
            "logical_result_count": logical_index,
            "seed": SAMPLE_SEED}


def load_manifest(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in
            Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
