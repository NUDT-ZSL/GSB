"""Shared helpers for building deterministic in-memory sample fixtures."""

from __future__ import annotations

from pathlib import Path

from diagwb.formats import (render_v1_body, render_v2_body, render_v3_body,
                            write_result_file)


def write_triplet(directory: Path, tool_id: str, result_ref: str,
                  payload=None, status: str = "OK",
                  confidence=None, shard_count: int = 2,
                  schema: str = "schema-2.0.0") -> Path:
    """Render the same logical result in v1, v2 and v3 on disk."""
    payload = payload or {"topic": "billing", "text": f"{tool_id}-text",
                          "metric": 0.42}
    confidence = confidence or [0.88, 0.92]
    result_dir = Path(directory) / tool_id
    result_dir.mkdir(parents=True, exist_ok=True)
    write_result_file(result_dir / "v1.txt", 1,
                      render_v1_body(tool_id, payload, status))
    write_result_file(result_dir / "v2.txt", 2, render_v2_body({
        "result_ref": result_ref, "tool_id": tool_id, "payload": payload,
        "status": status, "result_schema_version": schema,
        "confidence": confidence}))
    shards = [{"shard_id": f"{tool_id}-s-{i}", "order": i,
               "text": f"{tool_id}-part-{i}", "source": "native"}
              for i in range(shard_count)]
    v3 = {
        "format_version": 3, "result_ref": result_ref, "tool_id": tool_id,
        "payload": payload, "status": status,
        "result_schema_version": schema, "confidence": confidence,
        "shards": shards,
        "aggregation": {"policy": "merge_first",
                        "member_count": shard_count,
                        "member_refs": [s["shard_id"] for s in shards],
                        "rollup_metric": 0.5},
        "lineage": [
            {"node_id": f"{tool_id}:src:0", "source": "tool-execution",
             "detail": "raw", "upstream": []},
            {"node_id": f"{tool_id}:src:1", "source": "ingest",
             "detail": "warehouse", "upstream": [f"{tool_id}:src:0"]}],
    }
    write_result_file(result_dir / "v3.txt", 3, render_v3_body(v3))
    return result_dir
