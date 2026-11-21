from __future__ import annotations

import math
from typing import Any, Callable, Iterable

from .canonical import canonical_json, quantize
from .config import PipelineConfig
from .errors import BusinessError, ErrorCode
from .models import LabelSet


def _token_label_scores(
    token: dict[str, Any],
    labels: LabelSet,
    line_path_score: float,
    token_count: int,
) -> dict[str, float]:
    weight = abs(line_path_score) / float(max(1, token_count))
    lexical_factor = 1.0 + math.log(float(max(1, token.get("freq", 1))))
    length_factor = 1.0 + 0.05 * min(8, int(token.get("codepoint_len", len(token["word"]))))
    base = weight * lexical_factor * length_factor
    scores: dict[str, float] = {}
    for candidate in token["pos_candidates"]:
        if candidate not in labels.priors:
            raise BusinessError(
                ErrorCode.E_LABEL_INVALID,
                f"分词快照引用了不在受控集合中的标签: {candidate}",
                details={"label": candidate, "word": token.get("word")},
            )
        scores[candidate] = quantize(base * labels.priors[candidate])
    return scores


def _select_label(scores: dict[str, float], labels: LabelSet) -> tuple[str, float]:
    ranked = sorted(
        ((-score, labels.index(label), label, score) for label, score in scores.items())
    )
    if not ranked:
        raise BusinessError(ErrorCode.E_LABEL_INVALID, "词条没有可用的词性候选")
    _, _, selected, selected_score = ranked[0]
    total = sum(score for score in scores.values())
    confidence = selected_score / total if total > 0 else 1.0
    return selected, quantize(confidence)


def _tag_line(
    line: dict[str, Any],
    labels: LabelSet,
) -> dict[str, Any]:
    tagged_tokens: list[dict[str, Any]] = []
    label_counts = {label: 0 for label in labels.labels}
    confidences: list[float] = []
    for token in line["tokens"]:
        scores = _token_label_scores(
            token,
            labels,
            float(line["path_score"]),
            int(line["token_count"]),
        )
        selected, confidence = _select_label(scores, labels)
        label_counts[selected] += 1
        confidences.append(confidence)
        tagged_tokens.append(
            {
                "word": token["word"],
                "start": token["start"],
                "end": token["end"],
                "label": selected,
                "confidence": confidence,
                "scores": scores,
            }
        )
    mean_confidence = (
        quantize(sum(confidences) / float(len(confidences))) if confidences else 0.0
    )
    return {
        "doc_id": line["doc_id"],
        "line_no": line["line_no"],
        "text_codepoint_len": line["text_codepoint_len"],
        "path_score": line["path_score"],
        "token_count": line["token_count"],
        "tokens": tagged_tokens,
        "label_counts": {label: count for label, count in label_counts.items() if count},
        "mean_confidence": mean_confidence,
        "degradations": list(line.get("degradations", [])),
    }


def _resident_bytes(line: dict[str, Any]) -> int:
    score_maps = [
        token.get("scores", {})
        for token in line.get("tokens", [])
    ]
    return len(canonical_json(score_maps).encode("utf-8"))


def _trim_scores(line: dict[str, Any]) -> None:
    for token in line.get("tokens", []):
        token.pop("scores", None)


def run_tagging(
    segmentation_payload: dict[str, Any],
    labels: LabelSet,
    config: PipelineConfig,
    *,
    reusable_lines: dict[str, dict[str, Any]] | None = None,
    recompute_doc_ids: Iterable[str] | None = None,
    progress_hook: Callable[[str, int, str], None] | None = None,
) -> dict[str, Any]:
    recompute = set(recompute_doc_ids) if recompute_doc_ids is not None else None
    reusable_lines = reusable_lines or {}
    lines_out: list[dict[str, Any]] = []
    degradations: list[dict[str, Any]] = []
    resident_bytes = 0
    degraded = False

    for processed, line in enumerate(segmentation_payload["lines"], start=1):
        doc_id = line["doc_id"]
        if recompute is not None and doc_id not in recompute and doc_id in reusable_lines:
            tagged = reusable_lines[doc_id]
        else:
            tagged = _tag_line(line, labels)

        estimated = _resident_bytes(tagged)
        if resident_bytes + estimated > config.max_tag_resident_bytes:
            if config.tag_memory_policy == "error":
                raise BusinessError(
                    ErrorCode.E_RESOURCE_LIMIT,
                    "标注阶段驻留中间结果内存估算超过上限",
                    doc_id=doc_id,
                    line_no=line["line_no"],
                    stage="tag",
                    details={
                        "estimated_resident_bytes": resident_bytes + estimated,
                        "max_tag_resident_bytes": config.max_tag_resident_bytes,
                        "policy": config.tag_memory_policy,
                    },
                )
            if not degraded:
                for existing in lines_out:
                    _trim_scores(existing)
                    existing.setdefault("degradations", []).append(
                        {
                            "category": "TAG_MEMORY_TRIM",
                            "doc_id": existing["doc_id"],
                            "line_no": existing["line_no"],
                            "stage": "tag",
                            "detail": {
                                "trigger_doc_id": doc_id,
                                "max_tag_resident_bytes": config.max_tag_resident_bytes,
                                "offset_unit": "unicode_codepoint",
                            },
                        }
                    )
                    degradations.append(existing["degradations"][-1])
                degraded = True
            _trim_scores(tagged)
            record = {
                "category": "TAG_MEMORY_TRIM",
                "doc_id": doc_id,
                "line_no": line["line_no"],
                "stage": "tag",
                "detail": {
                    "estimated_resident_bytes": resident_bytes + estimated,
                    "max_tag_resident_bytes": config.max_tag_resident_bytes,
                    "offset_unit": "unicode_codepoint",
                },
            }
            tagged.setdefault("degradations", []).append(record)
            degradations.append(record)
            resident_bytes = 0
        else:
            resident_bytes += estimated
        lines_out.append(tagged)
        if progress_hook is not None:
            progress_hook("tag", processed, doc_id)

    return {
        "stage": "tag",
        "labels": labels.to_ref(),
        "label_set": list(labels.labels),
        "config_checksum": config.checksum,
        "line_count": len(lines_out),
        "lines": lines_out,
        "degradations": degradations,
    }
