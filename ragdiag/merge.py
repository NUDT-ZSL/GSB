"""Answer fragment merge and independent assembly checks."""

from __future__ import annotations

import math
from typing import Any

from .canonical import canonical_bytes, format_float, sha256_value
from .errors import E_FORMAT_CORRUPT, WorkbenchError


def _tool_signal(parsed_result: dict[str, Any]) -> float:
    values = [float(value) for value in parsed_result["numeric"].values()]
    aggregate = math.fsum(values)
    return aggregate / (1.0 + abs(aggregate))


def _evidence_rows(
    retrieval: dict[str, Any],
    corpus: dict[str, dict[str, Any]],
    call_index: int,
    per_fragment: int,
) -> list[dict[str, Any]]:
    candidates = retrieval["candidates"]
    if not candidates or per_fragment <= 0:
        return []
    start = (call_index * per_fragment) % len(candidates)
    selected = [
        candidates[(start + offset) % len(candidates)]
        for offset in range(min(per_fragment, len(candidates)))
    ]
    return [
        {
            "doc_id": candidate["doc_id"],
            "score": candidate["score"],
            "snippet": corpus[candidate["doc_id"]]["text"],
        }
        for candidate in selected
    ]


def fragment_text(
    tool_id: str,
    merge_position: int,
    tool_rendered: str,
    evidence: list[dict[str, Any]],
) -> str:
    evidence_text = "; ".join(
        f"{row['doc_id']}={row['snippet']}" for row in evidence
    )
    return (
        f"[tool:{merge_position}] {tool_id} => {tool_rendered}"
        f" || evidence: {evidence_text}"
    )


def answer_digest(
    request_id: str,
    turn_no: int,
    fragments: list[dict[str, Any]],
) -> str:
    return sha256_value(
        {
            "request_id": request_id,
            "turn_no": turn_no,
            "fragments": [
                {
                    "merge_position": fragment["merge_position"],
                    "text_digest": fragment["text_digest"],
                    "source_path": fragment["source_path"],
                }
                for fragment in fragments
            ],
        }
    )


def merge_request(
    request: dict[str, Any],
    parsed_results: list[dict[str, Any]],
    retrieval: dict[str, Any],
    corpus: dict[str, dict[str, Any]],
    config: dict[str, Any],
    merge_cache_key: str,
    retrieval_cache_key: str,
    read_cache_keys: list[str],
) -> dict[str, Any]:
    """Build answer fragments in tool-call order from explicit artifacts."""

    calls = request["tool_calls"]
    if len(parsed_results) != len(calls):
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "parsed tool result count does not match tool call count",
            calls=len(calls),
            parsed=len(parsed_results),
        )
    precision = config["precision"]
    merge_config = config.get("merge", {})
    per_fragment = int(merge_config.get("evidence_per_fragment", 1)) + int(
        merge_config.get("tool_expansion", 0)
    )
    unit_id = request["unit_id"]
    fragments: list[dict[str, Any]] = []
    numeric_values: list[float] = []
    for position, (call, parsed) in enumerate(zip(calls, parsed_results)):
        evidence = _evidence_rows(
            retrieval, corpus, position, per_fragment
        )
        retrieval_mean = (
            math.fsum(row["score"] for row in evidence) / len(evidence)
            if evidence
            else 0.0
        )
        tool_signal = _tool_signal(parsed)
        combined = 0.6 * retrieval_mean + 0.4 * tool_signal
        retrieval_component = format_float(retrieval_mean, precision)
        tool_component = format_float(tool_signal, precision)
        combined_component = format_float(combined, precision)
        numeric_values.extend(
            [retrieval_component, tool_component, combined_component]
        )
        text = fragment_text(
            call["tool_id"], position, parsed["rendered"], evidence
        )
        fragment = {
            "position": position,
            "merge_position": position,
            "request_id": request["request_id"],
            "source_request_id": request["request_id"],
            "turn_no": int(request["turn_no"]),
            "unit_id": unit_id,
            "tool_id": call["tool_id"],
            "arg_digest": call["arg_digest"],
            "result_ref": call["result_ref"],
            "format_version": parsed["format_version"],
            "result_format_version": parsed["format_version"],
            "tool_rendered": parsed["rendered"],
            "evidence": evidence,
            "text": text,
            "text_digest": sha256_value(text),
            "retrieval_mean": retrieval_component,
            "tool_signal": tool_component,
            "combined_score": combined_component,
            "source_path": [
                f"request:{unit_id}",
                f"read:{unit_id}:{position}",
                f"retrieve:{unit_id}",
                f"merge:{unit_id}",
            ],
            "cache_keys": {
                "read": read_cache_keys[position],
                "retrieval": retrieval_cache_key,
                "merge": merge_cache_key,
            },
            "cache_key": merge_cache_key,
            "snapshot_version": parsed["snapshot_version"],
        }
        fragments.append(fragment)
    answer_text = "\n".join(fragment["text"] for fragment in fragments)
    answer = {
        "request_id": request["request_id"],
        "turn_no": int(request["turn_no"]),
        "unit_id": unit_id,
        "text": answer_text,
        "fragment_count": len(fragments),
        "digest": answer_digest(
            request["request_id"], int(request["turn_no"]), fragments
        ),
    }
    artifact = {
        "unit_id": unit_id,
        "request_id": request["request_id"],
        "turn_no": int(request["turn_no"]),
        "fragments": fragments,
        "answer": answer,
        "numeric_values": numeric_values,
        "retrieval_hash": retrieval["retrieval_hash"],
    }
    artifact["artifact_hash"] = sha256_value(
        {key: value for key, value in artifact.items() if key != "artifact_hash"}
    )
    return artifact


def verify_answer_from_details(
    request: dict[str, Any],
    artifact: dict[str, Any],
) -> list[str]:
    """Independently rebuild every fragment and the final answer."""

    problems: list[str] = []
    rebuilt_fragments: list[dict[str, Any]] = []
    for expected_position, fragment in enumerate(artifact.get("fragments", [])):
        if fragment.get("merge_position") != expected_position:
            problems.append(
                f"fragment position mismatch at {expected_position}"
            )
        if fragment.get("source_request_id") != request["request_id"]:
            problems.append(
                f"fragment request mismatch at {expected_position}"
            )
        if fragment.get("turn_no") != int(request["turn_no"]):
            problems.append(f"fragment turn mismatch at {expected_position}")
        text = fragment_text(
            fragment["tool_id"],
            int(fragment["merge_position"]),
            fragment["tool_rendered"],
            fragment["evidence"],
        )
        if text != fragment.get("text"):
            problems.append(f"fragment text mismatch at {expected_position}")
        if sha256_value(text) != fragment.get("text_digest"):
            problems.append(f"fragment digest mismatch at {expected_position}")
        rebuilt_fragments.append(
            {
                "merge_position": expected_position,
                "text_digest": sha256_value(text),
                "source_path": fragment.get("source_path", []),
            }
        )
    rebuilt_answer_text = "\n".join(
        fragment["text"] for fragment in artifact.get("fragments", [])
    )
    if rebuilt_answer_text != artifact.get("answer", {}).get("text"):
        problems.append("answer text cannot be rebuilt from fragments")
    expected_digest = answer_digest(
        request["request_id"], int(request["turn_no"]), rebuilt_fragments
    )
    if expected_digest != artifact.get("answer", {}).get("digest"):
        problems.append("answer digest cannot be rebuilt from fragments")
    return problems


def canonical_artifact_bytes(artifact: dict[str, Any]) -> bytes:
    return canonical_bytes(artifact)

