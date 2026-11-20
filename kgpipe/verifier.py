from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .corpus import load_corpus
from .dictionary import EntityDictionary
from .errors import KGError, error_dict
from .models import CandidateTriple, Corpus, Document, EntitySpan
from .normalization import normalize_document, recompute_graph_stats
from .paths import compare_candidate_sets
from .recognition import recognize_document, verify_span_invariants
from .relation import RelationSchema, extract_relations
from .tokenizer import tokenize
from .util import canonical_json, content_checksum, read_json, verify_checksum


def _add_issue(
    issues: list[dict[str, Any]],
    *,
    category: str,
    doc_id: str | None,
    message: str,
    **details: Any,
) -> None:
    issue = error_dict(
        "E_INCONSISTENT",
        message,
        category=category,
        doc_id=doc_id,
        **details,
    )
    issues.append(issue)


def load_truth(data_dir: str | Path) -> dict[str, Any]:
    path = Path(data_dir) / "truth.json"
    try:
        raw = read_json(path)
    except (OSError, ValueError) as exc:
        raise KGError(
            "E_TRUTH_INVALID",
            "三元组真值清单无法读取",
            details={"path": str(path), "error": str(exc)},
        ) from exc
    if not isinstance(raw, dict):
        raise KGError("E_TRUTH_INVALID", "三元组真值清单必须是 JSON 对象")
    version = raw.get("truth_version")
    if not isinstance(version, int) or version < 1:
        raise KGError(
            "E_TRUTH_INVALID",
            "truth_version 必须为正整数",
            details={"truth_version": version},
        )
    expected = raw.get("checksum")
    if not isinstance(expected, str):
        raise KGError(
            "E_TRUTH_INVALID",
            "三元组真值清单缺少 checksum",
            details={"truth_version": version},
        )
    actual = content_checksum(raw)
    if expected != actual:
        raise KGError(
            "E_TRUTH_INVALID",
            "三元组真值清单校验和不符",
            details={
                "truth_version": version,
                "expected_checksum": expected,
                "actual_checksum": actual,
            },
        )
    return raw


def _normalize_truth_key(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(item["doc_id"]),
        str(item["head_entity_id"]),
        str(item["tail_entity_id"]),
        str(item["relation_type"]),
    )


def evaluate_truth(
    truth: dict[str, Any],
    normalized_docs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    truth_keys: list[tuple[str, str, str, str]] = []
    for item in truth.get("triples", []):
        try:
            truth_keys.append(_normalize_truth_key(item))
        except KeyError:
            raise KGError(
                "E_TRUTH_INVALID",
                "真值三元组缺少必需字段",
                details={"row": item},
            )
    if len(truth_keys) != len(set(truth_keys)):
        raise KGError("E_TRUTH_INVALID", "真值清单包含重复三元组")

    predicted_keys: list[tuple[str, str, str, str]] = []
    for doc_id in sorted(normalized_docs):
        for triple in normalized_docs[doc_id]["triples"]:
            predicted_keys.append(
                (
                    doc_id,
                    triple["head_entity_id"],
                    triple["tail_entity_id"],
                    triple["relation_type"],
                )
            )
    truth_set = set(truth_keys)
    predicted_set = set(predicted_keys)
    true_positive = truth_set & predicted_set
    false_positive = predicted_set - truth_set
    false_negative = truth_set - predicted_set
    precision = 1.0 if not predicted_set else len(true_positive) / len(predicted_set)
    recall = 1.0 if not truth_set else len(true_positive) / len(truth_set)
    f1 = (
        0.0
        if precision + recall == 0
        else 2 * precision * recall / (precision + recall)
    )

    def brief(key: tuple[str, str, str, str]) -> dict[str, str]:
        return {
            "doc_id": key[0],
            "head_entity_id": key[1],
            "tail_entity_id": key[2],
            "relation_type": key[3],
        }

    return {
        "truth_version": truth["truth_version"],
        "truth_checksum": truth["checksum"],
        "truth_count": len(truth_set),
        "predicted_count": len(predicted_set),
        "true_positive_count": len(true_positive),
        "false_positive_count": len(false_positive),
        "false_negative_count": len(false_negative),
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "false_positives": [brief(key) for key in sorted(false_positive)],
        "false_negatives": [brief(key) for key in sorted(false_negative)],
    }


def _index_documents(corpus: Corpus) -> dict[str, Document]:
    return {document.doc_id: document for document in corpus.documents}


def _report_span_rows(report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {}
    for document in report.get("stages", {}).get("entity_recognition", {}).get(
        "documents", []
    ):
        rows[document["doc_id"]] = document.get("spans", [])
    return rows


def _verify_triple_row(
    triple: dict[str, Any],
    *,
    doc_id: str,
    spans_by_id: dict[str, dict[str, Any]],
    schema: RelationSchema,
    sentence_text_by_no: dict[int, str],
    issues: list[dict[str, Any]],
) -> None:
    required = (
        "head_span_id",
        "tail_span_id",
        "relation_type",
        "confidence_bp",
        "score_components",
        "pattern_line",
        "trigger",
        "trigger_offset",
    )
    missing = [key for key in required if key not in triple]
    if missing:
        _add_issue(
            issues,
            category="TRIPLE_FIELDS",
            doc_id=doc_id,
            message="三元组缺少必需字段",
            triple_id=triple.get("triple_id"),
            missing_fields=missing,
        )
        return
    head = spans_by_id.get(triple["head_span_id"])
    tail = spans_by_id.get(triple["tail_span_id"])
    if head is None or tail is None:
        _add_issue(
            issues,
            category="TRIPLE_ENDPOINT",
            doc_id=doc_id,
            message="三元组端点不在同一文档实体集合中",
            triple_id=triple.get("triple_id"),
        )
        return
    if head.get("sent_no") != tail.get("sent_no"):
        _add_issue(
            issues,
            category="TRIPLE_ENDPOINT",
            doc_id=doc_id,
            message="三元组两端不在同一句",
            triple_id=triple.get("triple_id"),
        )
    components = triple["score_components"]
    if not isinstance(components, list) or not components:
        _add_issue(
            issues,
            category="CONFIDENCE_RECOMPUTE",
            doc_id=doc_id,
            message="置信分项为空",
            triple_id=triple.get("triple_id"),
        )
        return
    component_sum = 0
    for component in components:
        value = component.get("basis_points")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            _add_issue(
                issues,
                category="CONFIDENCE_RECOMPUTE",
                doc_id=doc_id,
                message="置信分项 basis_points 非法",
                triple_id=triple.get("triple_id"),
                component=component,
            )
            return
        component_sum += value
    expected_bp = triple["confidence_bp"]
    if component_sum != expected_bp:
        _add_issue(
            issues,
            category="CONFIDENCE_RECOMPUTE",
            doc_id=doc_id,
            message="置信度无法由分项明细重算",
            triple_id=triple.get("triple_id"),
            expected_bp=component_sum,
            actual_bp=expected_bp,
        )
    expected_confidence = round(expected_bp / 10000, 6)
    if triple.get("confidence") != expected_confidence:
        _add_issue(
            issues,
            category="CONFIDENCE_RECOMPUTE",
            doc_id=doc_id,
            message="浮点置信度与整数基点不一致",
            triple_id=triple.get("triple_id"),
            expected=expected_confidence,
            actual=triple.get("confidence"),
        )
    pattern = next(
        (item for item in schema.patterns if item.line_no == triple["pattern_line"]),
        None,
    )
    if pattern is None:
        _add_issue(
            issues,
            category="PATTERN_LINE",
            doc_id=doc_id,
            message="三元组引用了不存在的模式行号",
            triple_id=triple.get("triple_id"),
            pattern_line=triple["pattern_line"],
        )
        return
    if (
        pattern.relation_type != triple["relation_type"]
        or pattern.trigger != triple["trigger"]
    ):
        _add_issue(
            issues,
            category="PATTERN_LINE",
            doc_id=doc_id,
            message="三元组关系类型或触发词与模式行不一致",
            triple_id=triple.get("triple_id"),
        )
    sent_no = int(triple.get("sent_no", head.get("sent_no", 0)))
    text = sentence_text_by_no.get(sent_no, "")
    offset = triple["trigger_offset"]
    if (
        not isinstance(offset, int)
        or offset < 0
        or text[offset : offset + len(triple["trigger"])] != triple["trigger"]
    ):
        _add_issue(
            issues,
            category="TRIGGER_OFFSET",
            doc_id=doc_id,
            message="触发词偏移无法在源句文本中复现",
            triple_id=triple.get("triple_id"),
            trigger_offset=offset,
        )
    elif not (
        int(head["end"]) <= offset
        and offset + len(triple["trigger"]) <= int(tail["start"])
    ):
        _add_issue(
            issues,
            category="TRIGGER_OFFSET",
            doc_id=doc_id,
            message="触发词不在头实体与尾实体之间",
            triple_id=triple.get("triple_id"),
        )


def _verify_cross_path(
    *,
    report: dict[str, Any],
    schema: RelationSchema,
    issues: list[dict[str, Any]],
) -> None:
    cross = report.get("cross_path")
    if not isinstance(cross, dict):
        return
    for row in cross.get("documents", []):
        if row["common_count"] > min(row["exact_count"], row["approx_count"]):
            _add_issue(
                issues,
                category="CROSS_PATH_COUNTS",
                doc_id=row["doc_id"],
                message="跨路径公共三元组计数超过单路径计数",
            )
        if row["exact_count"] == 0:
            expected_recall = 1.0
        else:
            expected_recall = round(row["common_count"] / row["exact_count"], 6)
        if row["recall_estimate"] != expected_recall:
            _add_issue(
                issues,
                category="CROSS_PATH_RECALL",
                doc_id=row["doc_id"],
                message="跨路径召回估计无法由集合计数重算",
                expected=expected_recall,
                actual=row["recall_estimate"],
            )
        if row["missing_in_approx"] and row["statistics_comparable"]:
            _add_issue(
                issues,
                category="CROSS_PATH_COMPARABILITY",
                doc_id=row["doc_id"],
                message="存在缺失三元组却声明统计口径可比",
            )
    divergence = cross.get("error")
    if cross.get("statistics_comparable") is False:
        if not isinstance(divergence, dict) or divergence.get("code") != "E_PATH_DIVERGENCE":
            _add_issue(
                issues,
                category="CROSS_PATH_DIVERGENCE",
                doc_id=cross.get("first_divergence_doc_id"),
                message="跨路径不可比时未返回 E_PATH_DIVERGENCE",
            )


def verify_execution_report(
    report: dict[str, Any],
    data_dir: str | Path,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    try:
        corpus = load_corpus(data_dir)
        entity_dictionary = EntityDictionary.load(Path(data_dir) / "dictionary.json")
        schema = RelationSchema.load(Path(data_dir) / "relation_schema.json")
        truth = load_truth(data_dir)
    except KGError as exc:
        return {
            "valid": False,
            "issues": [exc.to_dict()],
            "issue_count": 1,
            "checks": {},
        }

    documents = _index_documents(corpus)
    report_spans = _report_span_rows(report)
    path_rows = report.get("stages", {}).get("relation_extraction", {}).get(
        "paths", {}
    )
    selected_paths = report.get("selection", {}).get("chosen_paths", [])
    neighbor_limit = int(report.get("selection", {}).get("neighbor_limit", 0))
    fresh_entities = {}
    fresh_paths: dict[str, dict[str, list[CandidateTriple]]] = {}

    for document in corpus.documents:
        fresh = recognize_document(document, entity_dictionary)
        fresh_entities[document.doc_id] = fresh
        reported_spans = report_spans.get(document.doc_id)
        if reported_spans is None:
            _add_issue(
                issues,
                category="SPAN_SET_MISSING",
                doc_id=document.doc_id,
                message="报告缺少文档实体跨度集合",
            )
        else:
            expected_spans = [span.to_dict() for span in fresh.spans]
            if canonical_json(reported_spans) != canonical_json(expected_spans):
                _add_issue(
                    issues,
                    category="SPAN_SET_MISMATCH",
                    doc_id=document.doc_id,
                    message="实体跨度集合无法由源文本和词典独立复现",
                )
            if len({span.get("span_id") for span in reported_spans}) != len(
                reported_spans
            ):
                _add_issue(
                    issues,
                    category="SPAN_ID_DUPLICATE",
                    doc_id=document.doc_id,
                    message="报告存在重复 span_id",
                )
            spans_by_sentence: dict[int, list[dict[str, Any]]] = {}
            for span in reported_spans:
                spans_by_sentence.setdefault(int(span["sent_no"]), []).append(span)
            for sentence in document.sentences:
                typed_spans = [
                    EntitySpan(
                        span_id=span["span_id"],
                        doc_id=span["doc_id"],
                        sent_no=span["sent_no"],
                        start=span["start"],
                        end=span["end"],
                        text=span["text"],
                        entity_id=span["entity_id"],
                        canonical_name=span["canonical_name"],
                        type=span["type"],
                        type_id=span["type_id"],
                        dictionary_version=span["dictionary_version"],
                    )
                    for span in spans_by_sentence.get(sentence.sent_no, [])
                ]
                for problem in verify_span_invariants(
                    text=sentence.text,
                    spans=typed_spans,
                ):
                    _add_issue(
                        issues,
                        category=problem.upper(),
                        doc_id=document.doc_id,
                        message="实体跨度不满足边界或互不重叠约束",
                        sent_no=sentence.sent_no,
                    )
                expected_tokens = [
                    token.to_dict() for token in tokenize(sentence.text)
                ]
                reported_doc = next(
                    (
                        item
                        for item in report.get("stages", {})
                        .get("entity_recognition", {})
                        .get("documents", [])
                        if item["doc_id"] == document.doc_id
                    ),
                    None,
                )
                reported_tokens = (
                    reported_doc.get("tokens_by_sentence", {}).get(
                        str(sentence.sent_no)
                    )
                    if reported_doc
                    else None
                )
                if reported_tokens is not None and canonical_json(
                    reported_tokens
                ) != canonical_json(expected_tokens):
                    _add_issue(
                        issues,
                        category="TOKENIZATION_MISMATCH",
                        doc_id=document.doc_id,
                        message="分词结果无法独立复现",
                        sent_no=sentence.sent_no,
                    )

    for path in selected_paths:
        if path not in path_rows:
            _add_issue(
                issues,
                category="PATH_MISSING",
                doc_id=None,
                message="选择路径缺少抽取结果",
                path=path,
            )
            continue
        fresh_paths[path] = {}
        expected_doc_ids = {document.doc_id for document in corpus.documents}
        reported_doc_ids = set(path_rows[path])
        if reported_doc_ids != expected_doc_ids:
            _add_issue(
                issues,
                category="PATH_DOC_SET",
                doc_id=None,
                message="路径结果的文档集合与语料不一致",
                path=path,
                missing_docs=sorted(expected_doc_ids - reported_doc_ids),
                extra_docs=sorted(reported_doc_ids - expected_doc_ids),
            )
        for document in corpus.documents:
            expected = extract_relations(
                document=document,
                entities=fresh_entities[document.doc_id],
                schema=schema,
                mode=path,
                neighbor_limit=neighbor_limit,
            )
            fresh_paths[path][document.doc_id] = expected
            reported = path_rows[path].get(document.doc_id, [])
            expected_rows = [item.to_dict() for item in expected]
            if canonical_json(reported) != canonical_json(expected_rows):
                _add_issue(
                    issues,
                    category="TRIPLE_SET_MISMATCH",
                    doc_id=document.doc_id,
                    message="候选三元组集合无法由源文本、模式表和路径配置独立复现",
                    path=path,
                )

    mandatory_lines = {
        pattern.line_no for pattern in schema.mandatory_patterns
    }
    if "exact" in path_rows and "approx" in path_rows:
        for document in corpus.documents:
            exact_mandatory = [
                row
                for row in path_rows["exact"].get(document.doc_id, [])
                if row["pattern_line"] in mandatory_lines
            ]
            approx_ids = {
                (
                    row["sent_no"],
                    row["head_span_id"],
                    row["tail_span_id"],
                    row["relation_type"],
                )
                for row in path_rows["approx"].get(document.doc_id, [])
            }
            for row in exact_mandatory:
                key = (
                    row["sent_no"],
                    row["head_span_id"],
                    row["tail_span_id"],
                    row["relation_type"],
                )
                if key not in approx_ids:
                    _add_issue(
                        issues,
                        category="MANDATORY_MISS",
                        doc_id=document.doc_id,
                        message="近似路径遗漏了强制高置信模式命中",
                        triple_id=row.get("triple_id"),
                    )
    elif "approx" in path_rows and mandatory_lines:
        for document in corpus.documents:
            exact_scan = extract_relations(
                document=document,
                entities=fresh_entities[document.doc_id],
                schema=schema,
                mode="exact",
                neighbor_limit=neighbor_limit,
            )
            exact_mandatory = [
                triple
                for triple in exact_scan
                if triple.pattern_line in mandatory_lines
            ]
            approx_keys = {
                (
                    row["sent_no"],
                    row["head_span_id"],
                    row["tail_span_id"],
                    row["relation_type"],
                )
                for row in path_rows["approx"].get(document.doc_id, [])
            }
            for triple in exact_mandatory:
                key = (
                    triple.sent_no,
                    triple.head_span_id,
                    triple.tail_span_id,
                    triple.relation_type,
                )
                if key not in approx_keys:
                    _add_issue(
                        issues,
                        category="MANDATORY_MISS",
                        doc_id=document.doc_id,
                        message="近似路径遗漏了强制高置信模式命中",
                        triple_id=triple.triple_id,
                    )

    normalized_report = report.get("stages", {}).get("normalization", {}).get(
        "documents", {}
    )
    expected_doc_ids = {document.doc_id for document in corpus.documents}
    if set(normalized_report) != expected_doc_ids:
        _add_issue(
            issues,
            category="NORMALIZATION_DOC_SET",
            doc_id=None,
            message="归一化明细的文档集合与语料不一致",
            missing_docs=sorted(expected_doc_ids - set(normalized_report)),
            extra_docs=sorted(set(normalized_report) - expected_doc_ids),
        )
    fresh_normalized: dict[str, dict[str, Any]] = {}
    for document in corpus.documents:
        canonical_path = (
            report.get("stages", {})
            .get("relation_extraction", {})
            .get("canonical_path")
        )
        path_candidates = fresh_paths.get(canonical_path, {}).get(document.doc_id, [])
        expected = normalize_document(
            document=document,
            entities=fresh_entities[document.doc_id],
            candidates=path_candidates,
            entity_dictionary=entity_dictionary,
        )
        fresh_normalized[document.doc_id] = expected
        reported = normalized_report.get(document.doc_id)
        if reported is None:
            _add_issue(
                issues,
                category="NORMALIZATION_MISSING",
                doc_id=document.doc_id,
                message="报告缺少归一化文档明细",
            )
            continue
        if canonical_json(reported) != canonical_json(expected):
            _add_issue(
                issues,
                category="NORMALIZATION_MISMATCH",
                doc_id=document.doc_id,
                message="归一化与去重结果无法独立复现",
            )

        spans_by_id = {
            span["span_id"]: span
            for span in report_spans.get(document.doc_id, [])
        }
        sentence_text_by_no = {
            sentence.sent_no: sentence.text for sentence in document.sentences
        }
        aliases_seen: dict[str, str] = {}
        for entity in reported.get("canonical_entities", []):
            entity_span_ids = set(entity.get("span_ids", []))
            literal_set = sorted(
                {
                    spans_by_id[span_id]["text"]
                    for span_id in entity_span_ids
                    if span_id in spans_by_id
                }
            )
            if sorted(entity.get("aliases", [])) != literal_set:
                _add_issue(
                    issues,
                    category="ALIAS_SPAN_MISMATCH",
                    doc_id=document.doc_id,
                    message="规范实体的别名集合与跨度字面量不一致",
                    entity_id=entity.get("entity_id"),
                )
            for alias in entity.get("aliases", []):
                previous = aliases_seen.get(alias)
                if previous is not None and previous != entity["entity_id"]:
                    _add_issue(
                        issues,
                        category="ALIAS_CONFLICT",
                        doc_id=document.doc_id,
                        message="同一别名映射到多个规范实体",
                        alias=alias,
                    )
                aliases_seen[alias] = entity["entity_id"]
                if reported.get("alias_to_canonical", {}).get(alias) != entity[
                    "entity_id"
                ]:
                    _add_issue(
                        issues,
                        category="ALIAS_MAPPING",
                        doc_id=document.doc_id,
                        message="全局别名映射与规范实体不一致",
                        alias=alias,
                    )
        reported_triple_keys = [
            (
                triple["head_entity_id"],
                triple["tail_entity_id"],
                triple["relation_type"],
            )
            for triple in reported.get("triples", [])
        ]
        duplicates = [
            key
            for key, count in Counter(reported_triple_keys).items()
            if count > 1
        ]
        if duplicates:
            _add_issue(
                issues,
                category="TRIPLE_DUPLICATE",
                doc_id=document.doc_id,
                message="归一化去重后三元组重复",
                duplicates=duplicates,
            )
        for triple in reported.get("triples", []):
            _verify_triple_row(
                triple,
                doc_id=document.doc_id,
                spans_by_id=spans_by_id,
                schema=schema,
                sentence_text_by_no=sentence_text_by_no,
                issues=issues,
            )
            head = spans_by_id.get(triple.get("head_span_id"))
            tail = spans_by_id.get(triple.get("tail_span_id"))
            if head is not None:
                resolved = entity_dictionary.resolve_entity_literal(head["text"])
                if resolved != triple.get("head_entity_id"):
                    _add_issue(
                        issues,
                        category="TRIPLE_ENDPOINT",
                        doc_id=document.doc_id,
                        message="三元组头实体与跨度规范实体不一致",
                        triple_id=triple.get("triple_id"),
                    )
            if tail is not None:
                resolved = entity_dictionary.resolve_entity_literal(tail["text"])
                if resolved != triple.get("tail_entity_id"):
                    _add_issue(
                        issues,
                        category="TRIPLE_ENDPOINT",
                        doc_id=document.doc_id,
                        message="三元组尾实体与跨度规范实体不一致",
                        triple_id=triple.get("triple_id"),
                    )

    # The graph must be derivable solely from per-document normalized details.
    try:
        recomputed = recompute_graph_stats(
            corpus,
            {
                doc_id: normalized_report[doc_id]
                for doc_id in normalized_report
                if doc_id in documents
            },
        )
    except KGError as exc:
        issues.append(exc.to_dict())
        recomputed = None
    reported_stats = report.get("stages", {}).get("graph", {}).get("statistics")
    if recomputed is not None and canonical_json(recomputed) != canonical_json(
        reported_stats
    ):
        _add_issue(
            issues,
            category="GRAPH_STATS",
            doc_id=None,
            message="图谱统计无法由逐文档明细独立重算",
        )

    _verify_cross_path(report=report, schema=schema, issues=issues)
    if "exact" in fresh_paths and "approx" in fresh_paths:
        cross = report.get("cross_path", {})
        reported_rows = {
            row["doc_id"]: row for row in cross.get("documents", [])
        }
        for document in corpus.documents:
            expected_row = compare_candidate_sets(
                doc_id=document.doc_id,
                exact=fresh_paths["exact"][document.doc_id],
                approximate=fresh_paths["approx"][document.doc_id],
                mandatory_pattern_lines=mandatory_lines,
            )
            if canonical_json(reported_rows.get(document.doc_id)) != canonical_json(
                expected_row
            ):
                _add_issue(
                    issues,
                    category="CROSS_PATH_RECOMPUTE",
                    doc_id=document.doc_id,
                    message="跨路径差异明细无法由两路径结果独立重算",
                )

    try:
        evaluation = evaluate_truth(truth, normalized_report)
    except KGError as exc:
        issues.append(exc.to_dict())
        evaluation = None
    report_evaluation = report.get("evaluation")
    if evaluation is not None and canonical_json(evaluation) != canonical_json(
        report_evaluation
    ):
        _add_issue(
            issues,
            category="TRUTH_EVALUATION",
            doc_id=None,
            message="真值评估无法独立复现",
        )

    return {
        "valid": not issues,
        "issue_count": len(issues),
        "issues": issues,
        "checks": {
            "documents_checked": len(corpus.documents),
            "paths_checked": sorted(selected_paths),
            "entity_spans_recomputed": sum(
                len(item.spans) for item in fresh_entities.values()
            ),
            "normalized_documents_recomputed": len(fresh_normalized),
            "graph_recomputed": True,
            "truth_evaluated": evaluation is not None,
        },
    }

