from __future__ import annotations

from collections import Counter
from typing import Any

from .errors import BusinessError, ErrorCode
from .models import LabelSet
from .validate import validate_offsets


def _issue(
    category: str,
    message: str,
    *,
    stage: str,
    doc_id: str | None = None,
    line_no: int | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": ErrorCode.E_INCONSISTENT,
        "category": category,
        "message": message,
        "stage": stage,
        "doc_id": doc_id,
        "line_no": line_no,
        "detail": detail or {},
    }


def run_selfcheck(
    validation_payload: dict[str, Any] | None,
    segmentation_payload: dict[str, Any] | None,
    tagging_payload: dict[str, Any] | None,
    summary_payload: dict[str, Any] | None,
    labels: LabelSet,
    *,
    affected: dict[str, list[str]] | None = None,
    recomputed: dict[str, list[str]] | None = None,
    reused: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    validation_by_doc: dict[str, dict[str, Any]] = {}
    if validation_payload:
        for line in validation_payload.get("lines", []):
            validation_by_doc[line["doc_id"]] = line

    if segmentation_payload:
        for line in segmentation_payload.get("lines", []):
            validation_line = validation_by_doc.get(line["doc_id"])
            text = validation_line["text"] if validation_line else None
            if text is None:
                joined = "".join(token["word"] for token in line.get("tokens", []))
                if len(joined) != line.get("text_codepoint_len"):
                    issues.append(
                        _issue(
                            "OFFSET_COVERAGE",
                            "无原始文本时，词条码点总长与行码点长度不一致",
                            stage="segment",
                            doc_id=line["doc_id"],
                            line_no=line["line_no"],
                            detail={
                                "joined_len": len(joined),
                                "text_codepoint_len": line.get("text_codepoint_len"),
                            },
                        )
                    )
                continue
            try:
                validate_offsets(
                    text,
                    line.get("tokens", []),
                    doc_id=line["doc_id"],
                    line_no=line["line_no"],
                    stage="segment",
                )
            except BusinessError as exc:
                issues.append(
                    _issue(
                        "OFFSET_LEN",
                        exc.message,
                        stage="segment",
                        doc_id=line["doc_id"],
                        line_no=line["line_no"],
                        detail=exc.details,
                    )
                )

    if tagging_payload:
        controlled = set(labels.labels)
        for line in tagging_payload.get("lines", []):
            for token in line.get("tokens", []):
                if token.get("label") not in controlled:
                    issues.append(
                        _issue(
                            "LABEL_NOT_CONTROLLED",
                            "标注标签不在受控集合中",
                            stage="tag",
                            doc_id=line["doc_id"],
                            line_no=line["line_no"],
                            detail={
                                "word": token.get("word"),
                                "label": token.get("label"),
                                "start": token.get("start"),
                            },
                        )
                    )

    if tagging_payload and summary_payload:
        tag_counts: Counter[str] = Counter()
        tag_tokens = 0
        for line in tagging_payload.get("lines", []):
            tag_tokens += int(line.get("token_count", 0))
            for token in line.get("tokens", []):
                tag_counts[str(token.get("label"))] += 1
        summary_counts = Counter(summary_payload.get("label_counts", {}))
        if tag_tokens != int(summary_payload.get("token_count", -1)):
            issues.append(
                _issue(
                    "SUMMARY_TOKEN_COUNT",
                    "汇总 token_count 与标注结果不一致",
                    stage="summarize",
                    detail={
                        "tag_token_count": tag_tokens,
                        "summary_token_count": summary_payload.get("token_count"),
                    },
                )
            )
        if tag_counts != summary_counts:
            issues.append(
                _issue(
                    "SUMMARY_LABEL_COUNTS",
                    "汇总标签计数与标注结果不一致",
                    stage="summarize",
                    detail={
                        "tag": dict(sorted(tag_counts.items())),
                        "summary": dict(sorted(summary_counts.items())),
                    },
                )
            )

    if affected and recomputed and reused:
        for stage in ("validate", "segment", "tag", "summarize"):
            affected_set = set(affected.get(stage, []))
            recomputed_set = set(recomputed.get(stage, []))
            reused_set = set(reused.get(stage, []))
            overlap = sorted(recomputed_set & reused_set)
            if overlap:
                for doc_id in overlap:
                    issues.append(
                        _issue(
                            "RECOMPUTE_REUSE_OVERLAP",
                            "重算集合与复用集合存在交集",
                            stage=stage,
                            doc_id=doc_id,
                            detail={
                                "recomputed": sorted(recomputed_set),
                                "reused": sorted(reused_set),
                            },
                        )
                    )
            if not affected_set.issubset(recomputed_set | reused_set):
                for doc_id in sorted(affected_set - (recomputed_set | reused_set)):
                    issues.append(
                        _issue(
                            "AFFECTED_NOT_COVERED",
                            "受影响集合未被重算或复用集合覆盖",
                            stage=stage,
                            doc_id=doc_id,
                            detail={"affected_count": len(affected_set)},
                        )
                    )
            if affected_set != recomputed_set:
                for doc_id in sorted(affected_set - recomputed_set):
                    issues.append(
                        _issue(
                            "AFFECTED_RECOMPUTE_MISMATCH",
                            "受影响文档未进入重算集合",
                            stage=stage,
                            doc_id=doc_id,
                            detail={"affected_count": len(affected_set)},
                        )
                    )
                for doc_id in sorted(recomputed_set - affected_set):
                    issues.append(
                        _issue(
                            "UNEXPECTED_RECOMPUTE",
                            "未受影响文档进入了重算集合",
                            stage=stage,
                            doc_id=doc_id,
                            detail={"affected_count": len(affected_set)},
                        )
                    )

    return {
        "ok": not issues,
        "code": ErrorCode.OK if not issues else ErrorCode.E_INCONSISTENT,
        "issue_count": len(issues),
        "issues": issues,
    }
