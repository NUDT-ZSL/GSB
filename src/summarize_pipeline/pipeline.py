from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from .errors import ErrorCode, PipelineError, sort_errors
from .generator import VOCABULARY
from .models import DocumentInput, PipelineConfig, SentenceInput
from .precision import PrecisionOps
from .textproc import compress_tokens, tokenize
from .utils import round_tree, sha256_object

REQUIRED_FIELDS = ("doc_id", "sent_no", "text", "char_len", "ref_summary_units")
STAGE_ORDER = (
    "frequency_norm",
    "weighted_sum",
    "smoothing",
    "quantization",
    "threshold_comparison",
    "rewrite_normalization",
)


def _error(
    code: str,
    stage: str,
    doc_id: str,
    message: str,
    detail: dict[str, Any] | None = None,
) -> PipelineError:
    return PipelineError(code, stage, doc_id, message, detail or {})


def load_documents(
    path: str | Path,
) -> tuple[list[DocumentInput], list[dict[str, Any]]]:
    errors: list[PipelineError] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    invalid_docs: set[str] = set()

    try:
        handle = Path(path).open("r", encoding="utf-8")
    except OSError as exc:
        raise _error(
            ErrorCode.INVALID_FIELD,
            "input",
            "*",
            f"cannot open input: {exc}",
            {"path": str(path)},
        ) from exc

    with handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(
                    _error(
                        ErrorCode.INVALID_FIELD,
                        "input",
                        "*",
                        f"invalid JSON line {line_number}: {exc.msg}",
                        {"line": line_number},
                    )
                )
                continue
            if not isinstance(row, dict):
                errors.append(
                    _error(
                        ErrorCode.INVALID_FIELD,
                        "input",
                        "*",
                        f"JSON line {line_number} is not an object",
                        {"line": line_number},
                    )
                )
                continue

            raw_doc_id = row.get("doc_id")
            doc_id = raw_doc_id if isinstance(raw_doc_id, str) and raw_doc_id else "*"
            missing = [field for field in REQUIRED_FIELDS if field not in row]
            if "doc_id" in missing:
                doc_id = "*"
            if missing:
                errors.append(
                    _error(
                        ErrorCode.MISSING_FIELD,
                        "input",
                        doc_id,
                        f"missing required field(s): {', '.join(missing)}",
                        {"line": line_number, "fields": missing},
                    )
                )
                if doc_id != "*":
                    invalid_docs.add(doc_id)
                continue

            if not isinstance(raw_doc_id, str) or not raw_doc_id.strip():
                errors.append(
                    _error(
                        ErrorCode.INVALID_FIELD,
                        "input",
                        doc_id,
                        "doc_id must be a non-empty string",
                        {"line": line_number},
                    )
                )
                continue

            text = row.get("text")
            if not isinstance(text, str) or not text.strip():
                errors.append(
                    _error(
                        ErrorCode.EMPTY_TEXT,
                        "sentence_split",
                        doc_id,
                        "text must be a non-empty string",
                        {"line": line_number, "sent_no": row.get("sent_no")},
                    )
                )
                invalid_docs.add(doc_id)
                continue

            sent_no = row.get("sent_no")
            if isinstance(sent_no, bool) or not isinstance(sent_no, int) or sent_no < 0:
                errors.append(
                    _error(
                        ErrorCode.INVALID_FIELD,
                        "sentence_split",
                        doc_id,
                        "sent_no must be a non-negative integer",
                        {"line": line_number, "sent_no": sent_no},
                    )
                )
                invalid_docs.add(doc_id)
                continue

            char_len = row.get("char_len")
            if isinstance(char_len, bool) or not isinstance(char_len, int):
                errors.append(
                    _error(
                        ErrorCode.INVALID_FIELD,
                        "sentence_split",
                        doc_id,
                        "char_len must be an integer",
                        {"line": line_number, "sent_no": sent_no},
                    )
                )
                invalid_docs.add(doc_id)
                continue
            if char_len != len(text):
                errors.append(
                    _error(
                        ErrorCode.INVALID_FIELD,
                        "sentence_split",
                        doc_id,
                        "char_len does not match len(text)",
                        {
                            "line": line_number,
                            "sent_no": sent_no,
                            "char_len": char_len,
                            "actual": len(text),
                        },
                    )
                )
                invalid_docs.add(doc_id)
                continue

            ref_units = row.get("ref_summary_units")
            if (
                not isinstance(ref_units, list)
                or not ref_units
                or any(not isinstance(unit, str) or not unit for unit in ref_units)
            ):
                errors.append(
                    _error(
                        ErrorCode.INVALID_FIELD,
                        "input",
                        doc_id,
                        "ref_summary_units must be a non-empty string list",
                        {"line": line_number, "sent_no": sent_no},
                    )
                )
                invalid_docs.add(doc_id)
                continue

            if doc_id not in grouped:
                grouped[doc_id] = []
                order.append(doc_id)
            grouped[doc_id].append(
                {
                    "sent_no": sent_no,
                    "text": text,
                    "char_len": char_len,
                    "ref_summary_units": tuple(ref_units),
                    "line": line_number,
                }
            )

    documents: list[DocumentInput] = []
    for doc_id in order:
        records = grouped[doc_id]
        seen_sent_no: set[int] = set()
        duplicate = False
        for record in records:
            if record["sent_no"] in seen_sent_no:
                duplicate = True
                errors.append(
                    _error(
                        ErrorCode.DUPLICATE_DOC_ID,
                        "input",
                        doc_id,
                        "duplicate (doc_id, sent_no) indicates a repeated document id",
                        {"sent_no": record["sent_no"], "line": record["line"]},
                    )
                )
            seen_sent_no.add(record["sent_no"])
        if duplicate:
            invalid_docs.add(doc_id)
            continue

        records.sort(key=lambda item: item["sent_no"])
        expected = list(range(len(records)))
        actual = [item["sent_no"] for item in records]
        if actual != expected:
            errors.append(
                _error(
                    ErrorCode.INVALID_SENT_NO,
                    "sentence_split",
                    doc_id,
                    "sent_no values must be contiguous and start at zero",
                    {"actual": actual, "expected": expected},
                )
            )
            invalid_docs.add(doc_id)
            continue

        ref_sets = {record["ref_summary_units"] for record in records}
        if len(ref_sets) != 1:
            errors.append(
                _error(
                    ErrorCode.INVALID_FIELD,
                    "input",
                    doc_id,
                    "ref_summary_units must be identical across a document",
                )
            )
            invalid_docs.add(doc_id)
            continue

        if doc_id in invalid_docs:
            continue
        sentences = tuple(
            SentenceInput(
                sent_no=record["sent_no"],
                text=record["text"],
                char_len=record["char_len"],
            )
            for record in records
        )
        original_chars = sum(sentence.char_len for sentence in sentences) + max(
            0, len(sentences) - 1
        )
        documents.append(
            DocumentInput(
                doc_id=doc_id,
                sentences=sentences,
                ref_summary_units=tuple(records[0]["ref_summary_units"]),
                original_chars=original_chars,
            )
        )

    return documents, sort_errors(error.to_dict() for error in errors)


def _stage_value(value: float | None, ops: PrecisionOps) -> dict[str, Any]:
    if value is None:
        return {"value": None, "fixed_raw": None}
    return {"value": value, "fixed_raw": ops.raw(value)}


def _decision_record(
    *,
    value: float,
    threshold: float,
    tolerance: float,
    policy: str,
) -> dict[str, Any]:
    strict = value > threshold
    tolerance_decision = value >= threshold - tolerance
    return {
        "strict": strict,
        "tolerance": tolerance_decision,
        "selected": strict if policy == "strict" else tolerance_decision,
        "margin": value - threshold,
    }


def selection_order(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score descending, then sent_no ascending for deterministic ties."""

    return sorted(
        candidates,
        key=lambda item: (-float(item["smoothed_score"]), int(item["sent_no"])),
    )


def _policy_impact(
    decisions: Iterable[dict[str, Any]],
    key: str,
) -> dict[str, Any]:
    rows = list(decisions)
    rejected_to_admitted = sorted(
        item[key] for item in rows if not item["strict"] and item["tolerance"]
    )
    admitted_to_rejected = sorted(
        item[key] for item in rows if item["strict"] and not item["tolerance"]
    )
    return {
        "strict_admitted": sum(1 for item in rows if item["strict"]),
        "tolerance_admitted": sum(1 for item in rows if item["tolerance"]),
        "affected_count": len(rejected_to_admitted) + len(admitted_to_rejected),
        "directions": {
            "rejected_to_admitted": rejected_to_admitted,
            "admitted_to_rejected": admitted_to_rejected,
        },
    }


def _base_sentence_result(
    *,
    sentence: SentenceInput,
    doc_id: str,
    sentence_count: int,
    controlled_tokens: Counter[str],
    vocab: tuple[str, ...],
    normalized_freqs: dict[str, float],
    frequency_denominator: float,
    frequency_unprotected: bool,
    doc_vocab_size: int,
    config: PipelineConfig,
    ops: PrecisionOps,
) -> dict[str, Any]:
    tokens = tokenize(sentence.text)
    token_count = len(tokens)
    vocab_set = set(vocab)
    token_set = set(tokens)
    known_tokens = token_set & vocab_set

    position = ops.div(float(sentence_count - sentence.sent_no), float(sentence_count))
    length_numerator = float(
        min(sentence.char_len, config.ideal_sentence_chars)
    )
    length_denominator = float(
        max(sentence.char_len, config.ideal_sentence_chars)
    )
    length = ops.div(length_numerator, length_denominator)

    frequency_total = ops.coerce(0.0)
    for token in tokens:
        frequency_total = ops.add(
            frequency_total, normalized_freqs.get(token, 0.0)
        )
    frequency = ops.div_protected(
        frequency_total, float(token_count)
    )[0]
    coverage = ops.div_protected(
        float(len(known_tokens)), float(doc_vocab_size)
    )[0]

    weighted = ops.mul(config.weights.position, position)
    weighted = ops.add(
        weighted, ops.mul(config.weights.length, length)
    )
    weighted = ops.add(
        weighted, ops.mul(config.weights.frequency, frequency)
    )
    weighted = ops.add(
        weighted, ops.mul(config.weights.coverage, coverage)
    )
    smoothing_denominator = ops.add(1.0, config.smoothing)
    smoothed = ops.div(
        ops.add(weighted, config.smoothing), smoothing_denominator
    )
    quantized = ops.coerce(smoothed)
    threshold = ops.coerce(config.sentence_threshold)
    tolerance = ops.coerce(config.tolerance_abs)
    admission = _decision_record(
        value=smoothed,
        threshold=threshold,
        tolerance=tolerance,
        policy=config.threshold_policy,
    )

    return {
        "doc_id": doc_id,
        "sent_no": sentence.sent_no,
        "text": sentence.text,
        "char_len": sentence.char_len,
        "tokens": list(tokens),
        "components": {
            "position": position,
            "length": length,
            "frequency": frequency,
            "coverage": coverage,
        },
        "component_fixed_raw": {
            "position": ops.raw(position),
            "length": ops.raw(length),
            "frequency": ops.raw(frequency),
            "coverage": ops.raw(coverage),
        },
        "frequency_denominator": frequency_denominator,
        "frequency_denominator_protected": not frequency_unprotected,
        "weighted_sum": weighted,
        "weighted_sum_fixed_raw": ops.raw(weighted),
        "smoothed_score": smoothed,
        "smoothed_fixed_raw": ops.raw(smoothed),
        "admission": admission,
        "selected": False,
        "stages": {
            "frequency_norm": _stage_value(frequency, ops),
            "weighted_sum": _stage_value(weighted, ops),
            "smoothing": _stage_value(smoothed, ops),
            "quantization": _stage_value(quantized, ops),
            "threshold_comparison": _stage_value(
                1.0 if admission["selected"] else 0.0, ops
            ),
            "rewrite_normalization": _stage_value(None, ops),
        },
        "rewrite": None,
        "_tokens": tokens,
    }


def _normalize_rewrites(
    *,
    selected: list[dict[str, Any]],
    config: PipelineConfig,
    ops: PrecisionOps,
) -> dict[str, Any]:
    scores = [float(item["smoothed_score"]) for item in selected]
    denominator_unprotected = ops.sum(scores)
    denominator = denominator_unprotected
    denominator_source = "sum_selected_smoothed_scores"
    denominator_protected = False
    if abs(denominator) < ops.subnormal_floor:
        denominator_protected = True
        denominator = ops.mul(
            config.subnormal_floor, float(len(selected))
        )
        denominator_source = "subnormal_protected_uniform_floor"

    smoothing_term = ops.mul(config.smoothing, float(len(selected)))
    adjusted_denominator = ops.add(denominator, smoothing_term)
    adjusted_values = []
    for item in selected:
        numerator = ops.add(float(item["smoothed_score"]), config.smoothing)
        adjusted_values.append(
            ops.div_protected(numerator, adjusted_denominator)[0]
        )

    temperature_exponent = ops.div(1.0, config.temperature)
    powered = [
        ops.power(value, temperature_exponent) for value in adjusted_values
    ]
    temperature_denominator = ops.sum(powered)
    temperature_source = "sum_temperature_powered_adjusted_scores"
    if abs(temperature_denominator) < ops.subnormal_floor:
        uniform = ops.div(1.0, float(len(selected)))
        powered = [uniform for _ in selected]
        temperature_denominator = ops.coerce(1.0)
        temperature_source = "uniform_subnormal_fallback"

    normalized = [
        ops.div_protected(value, temperature_denominator)[0]
        for value in powered
    ]
    threshold = ops.coerce(config.rewrite_threshold)
    tolerance = ops.coerce(config.tolerance_abs)
    rewrite_decisions = []
    for item, adjusted, norm_value in zip(selected, adjusted_values, normalized):
        decision = _decision_record(
            value=norm_value,
            threshold=threshold,
            tolerance=tolerance,
            policy=config.threshold_policy,
        )
        rewrite_decisions.append(
            {
                "sent_no": item["sent_no"],
                "adjusted": adjusted,
                "normalized": norm_value,
                "normalized_fixed_raw": ops.raw(norm_value),
                "decision": decision,
            }
        )

    return {
        "denominator": {
            "source": denominator_source,
            "raw_sum": denominator_unprotected,
            "effective": denominator,
            "protected": denominator_protected,
            "adjusted": adjusted_denominator,
        },
        "temperature": {
            "value": config.temperature,
            "exponent": temperature_exponent,
            "denominator": temperature_denominator,
            "denominator_source": temperature_source,
        },
        "decisions": rewrite_decisions,
        "normalized_scores": {
            item["sent_no"]: decision["normalized"]
            for item, decision in zip(selected, rewrite_decisions)
        },
    }


def _quality_metrics(
    *,
    doc: DocumentInput,
    rewrite_units: list[dict[str, Any]],
    output_text: str,
    config: PipelineConfig,
    ops: PrecisionOps,
) -> dict[str, Any]:
    ref_set = set(doc.ref_summary_units)
    covered: set[str] = set()
    for unit in rewrite_units:
        covered.update(set(unit["kept_tokens"]) & ref_set)
    coverage = ops.div_protected(
        float(len(covered)), float(len(ref_set))
    )[0]
    output_chars = len(output_text)
    compression = ops.div_protected(
        float(output_chars), float(doc.original_chars)
    )[0]
    target = ops.coerce(config.compression_ratio)
    deviation = ops.div_protected(
        abs(ops.sub(compression, target)), target
    )[0]
    remaining = ops.sub(1.0, deviation)
    efficiency = remaining if remaining > 0.0 else 0.0
    quality = ops.add(
        ops.mul(0.7, coverage), ops.mul(0.3, efficiency)
    )
    threshold = ops.coerce(config.aggregate_threshold)
    tolerance = ops.coerce(config.tolerance_abs)
    aggregate_decision = _decision_record(
        value=quality,
        threshold=threshold,
        tolerance=tolerance,
        policy=config.threshold_policy,
    )
    return {
        "coverage": coverage,
        "compression_ratio": compression,
        "target_compression_ratio": config.compression_ratio,
        "compression_efficiency": efficiency,
        "quality_score": quality,
        "quality_formula": "0.7*coverage + 0.3*compression_efficiency",
        "aggregate_threshold": config.aggregate_threshold,
        "aggregate_decision": aggregate_decision,
        "aggregate_fixed_raw": ops.raw(quality),
        "covered_ref_units": sorted(covered),
        "ref_units": list(doc.ref_summary_units),
        "output_chars": output_chars,
        "original_chars": doc.original_chars,
    }


def _run_document(
    doc: DocumentInput,
    config: PipelineConfig,
    mode: str,
) -> dict[str, Any]:
    tier_precision = replace(config.precision, mode=mode)
    ops = PrecisionOps.from_config(tier_precision, config.subnormal_floor)
    vocab = tuple(config.controlled_vocab)
    vocab_set = set(vocab)
    all_tokens = tuple(
        token for sentence in doc.sentences for token in tokenize(sentence.text)
    )
    controlled_tokens = Counter(
        token for token in all_tokens if token in vocab_set
    )
    total_controlled = sum(controlled_tokens.values())

    frequency_denominator = ops.add(
        float(total_controlled),
        ops.mul(config.smoothing, float(len(vocab))),
    )
    normalized_freqs: dict[str, float] = {}
    frequency_unprotected = True
    for word in vocab:
        count = float(controlled_tokens.get(word, 0))
        value, protected = ops.div_protected(
            ops.add(count, config.smoothing), frequency_denominator
        )
        normalized_freqs[word] = value
        frequency_unprotected = frequency_unprotected and not protected

    doc_vocab = set()
    for token in all_tokens:
        if token in vocab_set:
            doc_vocab.add(token)
    doc_vocab_size = len(doc_vocab)

    sentence_results = [
        _base_sentence_result(
            sentence=sentence,
            doc_id=doc.doc_id,
            sentence_count=len(doc.sentences),
            controlled_tokens=controlled_tokens,
            vocab=vocab,
            normalized_freqs=normalized_freqs,
            frequency_denominator=frequency_denominator,
            frequency_unprotected=frequency_unprotected,
            doc_vocab_size=doc_vocab_size,
            config=config,
            ops=ops,
        )
        for sentence in doc.sentences
    ]

    candidates = [item for item in sentence_results if item["admission"]["selected"]]
    candidates = selection_order(candidates)
    selected: list[dict[str, Any]] = []
    budget_skipped: list[dict[str, Any]] = []
    used_chars = 0
    for item in candidates:
        if used_chars + int(item["char_len"]) <= config.length_budget:
            selected.append(item)
            used_chars += int(item["char_len"])
        else:
            budget_skipped.append(
                {
                    "sent_no": item["sent_no"],
                    "char_len": item["char_len"],
                    "score": item["smoothed_score"],
                }
            )

    if not selected:
        score_details = [
            {
                "sent_no": item["sent_no"],
                "char_len": item["char_len"],
                "components": item["components"],
                "weighted_sum": item["weighted_sum"],
                "smoothed_score": item["smoothed_score"],
                "admission": item["admission"],
            }
            for item in sentence_results
        ]
        raise _error(
            ErrorCode.EMPTY_SELECTION,
            "selection",
            doc.doc_id,
            "selection budget produced an empty summary unit set",
            {
                "length_budget": config.length_budget,
                "admitted_sent_nos": [
                    item["sent_no"] for item in candidates
                ],
                "budget": config.length_budget,
                "sentence_scores": score_details,
                "normalization_denominator": {
                    "source": None,
                    "reason": "no selected units",
                },
            },
        )

    for item in selected:
        item["selected"] = True
    selected_sent_nos = {item["sent_no"] for item in selected}

    normalization = _normalize_rewrites(
        selected=selected, config=config, ops=ops
    )
    rewrite_by_sent = {
        decision["sent_no"]: decision
        for decision in normalization["decisions"]
    }

    output_units: list[dict[str, Any]] = []
    for item in selected:
        decision = rewrite_by_sent[item["sent_no"]]
        item["rewrite"] = decision
        item["stages"]["rewrite_normalization"] = _stage_value(
            decision["normalized"], ops
        )
        if not decision["decision"]["selected"]:
            item["rewrite_text"] = None
            continue
        target_chars = max(
            1,
            int(
                math.ceil(
                    ops.mul(
                        float(item["char_len"]), config.compression_ratio
                    )
                )
            ),
        )
        rewritten_text, kept_count = compress_tokens(
            item["_tokens"], target_chars
        )
        kept_tokens = item["_tokens"][:kept_count]
        ref_set = set(doc.ref_summary_units)
        item["rewrite_text"] = rewritten_text
        output_units.append(
            {
                "sent_no": item["sent_no"],
                "original_chars": item["char_len"],
                "target_chars": target_chars,
                "rewritten_text": rewritten_text,
                "rewritten_chars": len(rewritten_text),
                "kept_tokens": list(kept_tokens),
                "ref_hits": sorted(set(kept_tokens) & ref_set),
                "smoothed_score": item["smoothed_score"],
                "normalized_score": decision["normalized"],
                "normalized_fixed_raw": decision["normalized_fixed_raw"],
            }
        )

    output_units.sort(
        key=lambda item: (-float(item["normalized_score"]), int(item["sent_no"]))
    )
    output_text = " ".join(item["rewritten_text"] for item in output_units)
    quality = _quality_metrics(
        doc=doc,
        rewrite_units=output_units,
        output_text=output_text,
        config=config,
        ops=ops,
    )

    sentence_decisions = [
        {
            "sent_no": item["sent_no"],
            "strict": item["admission"]["strict"],
            "tolerance": item["admission"]["tolerance"],
        }
        for item in sentence_results
    ]
    rewrite_decisions = [
        {
            "sent_no": item["sent_no"],
            "strict": item["decision"]["strict"],
            "tolerance": item["decision"]["tolerance"],
        }
        for item in normalization["decisions"]
    ]
    sentence_policy = _policy_impact(sentence_decisions, "sent_no")
    rewrite_policy = _policy_impact(rewrite_decisions, "sent_no")
    policy_changed = (
        sentence_policy["affected_count"] + rewrite_policy["affected_count"]
    )
    for item in sentence_results:
        item.pop("_tokens", None)

    return {
        "doc_id": doc.doc_id,
        "ref_summary_units": list(doc.ref_summary_units),
        "original_chars": doc.original_chars,
        "sentence_split": {
            "format": "pre_split_jsonl_records",
            "sentence_count": len(doc.sentences),
            "sent_nos": [sentence.sent_no for sentence in doc.sentences],
        },
        "word_frequency": {
            "controlled_vocab_size": len(vocab),
            "total_controlled_tokens": total_controlled,
            "denominator": frequency_denominator,
            "denominator_protected": not frequency_unprotected,
            "counts": {
                word: controlled_tokens.get(word, 0) for word in sorted(vocab)
            },
            "normalized": {
                word: normalized_freqs[word] for word in sorted(vocab)
            },
        },
        "sentences": sentence_results,
        "selection": {
            "budget_chars": config.length_budget,
            "used_chars": used_chars,
            "selected_sent_nos": [
                item["sent_no"]
                for item in sorted(selected, key=lambda row: row["sent_no"])
            ],
            "selected_in_score_order": [
                item["sent_no"] for item in selected
            ],
            "budget_skipped": budget_skipped,
            "selection_rule": "score_desc_sent_no_asc_then_length_budget",
            "selected_sent_no_set": sorted(selected_sent_nos),
        },
        "rewrite_normalization": {
            "denominator": normalization["denominator"],
            "temperature": normalization["temperature"],
            "decisions": normalization["decisions"],
        },
        "rewrite_units": output_units,
        "rewritten_text": output_text,
        "quality": quality,
        "threshold_diagnostics": {
            "configured_policy": config.threshold_policy,
            "tolerance_abs": config.tolerance_abs,
            "sentence_admission": sentence_policy,
            "rewrite_admission": rewrite_policy,
            "changed_by_policy": policy_changed > 0,
            "marker": "POLICY_SENSITIVE" if policy_changed else "STABLE",
        },
        "stages_present": list(STAGE_ORDER),
    }


def run_precision(
    documents: Iterable[DocumentInput],
    config: PipelineConfig,
    mode: str,
    *,
    input_errors: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    if mode not in ("float64", "float32", "fixed"):
        raise _error(
            ErrorCode.UNKNOWN_PRECISION,
            "config",
            "*",
            f"unknown precision mode: {mode}",
            {"precision_mode": mode},
        )
    tier_precision = replace(config.precision, mode=mode)
    ops = PrecisionOps.from_config(tier_precision, config.subnormal_floor)
    outputs: dict[str, Any] = {}
    errors: list[dict[str, Any]] = list(input_errors)
    for doc in documents:
        try:
            outputs[doc.doc_id] = _run_document(doc, config, mode)
        except PipelineError as exc:
            errors.append(exc.to_dict())
        except Exception as exc:
            errors.append(
                _error(
                    ErrorCode.UNATTRIBUTED,
                    "unknown",
                    doc.doc_id,
                    f"unattributed exception: {type(exc).__name__}: {exc}",
                    {"exception_type": type(exc).__name__},
                ).to_dict()
            )
    return {
        "precision_record": ops.record(),
        "documents": outputs,
        "errors": sort_errors(errors),
        "document_ids": sorted(outputs),
    }


def _empty_spectrum(mode: str) -> dict[str, Any]:
    return {
        "precision": mode,
        "reference_precision": "float64",
        "stages": {
            stage: {
                "samples": 0,
                "sum_abs_deviation": 0.0,
                "mean_abs_deviation": 0.0,
                "max_abs_deviation": 0.0,
                "relative_deviation": 0.0,
                "relative_unbounded": False,
                "relative_denominator": 0.0,
                "error_interval": {
                    "low_abs": 0.0,
                    "high_abs": 0.0,
                    "method": "observed_abs_min_max",
                },
                "amplification": {
                    "value": 1.0,
                    "unbounded": False,
                    "denominator_stage": None,
                    "formula": "sum_abs_deviation(k)/sum_abs_deviation(k-1)",
                },
                "chain_amplification": {
                    "value": 1.0,
                    "unbounded": False,
                    "formula": "sum_abs_deviation(k)/sum_abs_deviation(frequency_norm)",
                },
            }
            for stage in STAGE_ORDER
        },
        "dominant_source": "none",
        "dominant_rule": "largest sum_abs_deviation across traceable stages",
        "per_sentence": [],
        "per_document": {},
    }


def _aligned_stage_value(
    sentence_row: dict[str, Any],
    stage: str,
) -> float | None:
    stage_row = sentence_row["stages"][stage]
    value = stage_row["value"]
    return None if value is None else float(value)


def _build_error_spectrum(
    reference: dict[str, Any],
    actual: dict[str, Any],
    config: PipelineConfig,
    mode: str,
) -> dict[str, Any]:
    ops = PrecisionOps.from_config(replace(config.precision, mode=mode), config.subnormal_floor)
    spectrum = _empty_spectrum(mode)
    samples: list[dict[str, Any]] = []
    stage_values: dict[str, list[tuple[float, float, dict[str, Any]]]] = {
        stage: [] for stage in STAGE_ORDER
    }
    ref_docs = reference["documents"]
    actual_docs = actual["documents"]
    common_docs = sorted(set(ref_docs) & set(actual_docs))
    for doc_id in common_docs:
        ref_doc = ref_docs[doc_id]
        actual_doc = actual_docs[doc_id]
        ref_by_sent = {
            int(item["sent_no"]): item for item in ref_doc["sentences"]
        }
        actual_by_sent = {
            int(item["sent_no"]): item for item in actual_doc["sentences"]
        }
        for sent_no in sorted(set(ref_by_sent) | set(actual_by_sent)):
            ref_sentence = ref_by_sent.get(sent_no)
            actual_sentence = actual_by_sent.get(sent_no)
            if ref_sentence is None or actual_sentence is None:
                samples.append(
                    {
                        "doc_id": doc_id,
                        "sent_no": sent_no,
                        "stage": "structure",
                        "reference": 1.0 if ref_sentence else 0.0,
                        "actual": 1.0 if actual_sentence else 0.0,
                        "abs_deviation": 1.0,
                        "relative_deviation": 1.0,
                        "direction": "sentence_presence_mismatch",
                    }
                )
                continue
            for stage in STAGE_ORDER:
                ref_value = _aligned_stage_value(ref_sentence, stage)
                actual_value = _aligned_stage_value(actual_sentence, stage)
                if ref_value is None and actual_value is None:
                    continue
                if ref_value is None:
                    ref_value = 0.0
                if actual_value is None:
                    actual_value = 0.0
                abs_dev = abs(actual_value - ref_value)
                rel_denom = max(abs(ref_value), config.subnormal_floor)
                sample = {
                    "doc_id": doc_id,
                    "sent_no": sent_no,
                    "stage": stage,
                    "reference": ref_value,
                    "actual": actual_value,
                    "abs_deviation": abs_dev,
                    "relative_deviation": abs_dev / rel_denom,
                    "direction": (
                        "equal"
                        if abs_dev == 0.0
                        else ("actual_higher" if actual_value > ref_value else "actual_lower")
                    ),
                }
                if actual_sentence["stages"][stage]["fixed_raw"] is not None:
                    raw = int(actual_sentence["stages"][stage]["fixed_raw"])
                    expected_ref_raw = ops.raw(ref_value)
                    sample["fixed_raw"] = raw
                    sample["quantized_reference_raw"] = expected_ref_raw
                    sample["quantization_delta_from_reference"] = (
                        ops.dequantize(raw) - ops.dequantize(expected_ref_raw)
                    )
                    sample["dequantized_actual"] = ops.dequantize(raw)
                samples.append(sample)
                stage_values[stage].append((ref_value, actual_value, sample))

    previous_sum = 0.0
    frequency_sum = 0.0
    stage_stats: dict[str, Any] = {}
    for stage in STAGE_ORDER:
        rows = stage_values[stage]
        abs_values = [abs(actual - ref) for ref, actual, _ in rows]
        signed_sum = sum(actual - ref for ref, actual, _ in rows)
        total_abs = sum(abs_values)
        denominator = sum(abs(ref) for ref, _, _ in rows)
        if stage == "frequency_norm":
            frequency_sum = total_abs
        if previous_sum == 0.0:
            if total_abs == 0.0:
                amplification_value: float | None = 1.0
                unbounded = False
            else:
                amplification_value = None
                unbounded = True
        else:
            amplification_value = total_abs / previous_sum
            unbounded = False
        if frequency_sum == 0.0:
            chain_value: float | None = 1.0 if total_abs == 0.0 else None
            chain_unbounded = total_abs != 0.0
        else:
            chain_value = total_abs / frequency_sum
            chain_unbounded = False
        stage_stats[stage] = {
            "samples": len(rows),
            "sum_abs_deviation": total_abs,
            "mean_abs_deviation": (
                total_abs / len(rows) if rows else 0.0
            ),
            "max_abs_deviation": max(abs_values, default=0.0),
            "signed_mean_deviation": signed_sum / len(rows) if rows else 0.0,
            "relative_deviation": (
                total_abs / denominator
                if denominator > 0.0
                else (0.0 if total_abs == 0.0 else None)
            ),
            "relative_unbounded": denominator == 0.0 and total_abs > 0.0,
            "relative_denominator": denominator,
            "error_interval": {
                "low_abs": min(abs_values, default=0.0),
                "high_abs": max(abs_values, default=0.0),
                "method": "observed_abs_min_max",
            },
            "amplification": {
                "value": amplification_value,
                "unbounded": unbounded,
                "denominator_stage": (
                    STAGE_ORDER[STAGE_ORDER.index(stage) - 1]
                    if STAGE_ORDER.index(stage) > 0
                    else None
                ),
                "formula": "sum_abs_deviation(k)/sum_abs_deviation(k-1)",
            },
            "chain_amplification": {
                "value": chain_value,
                "unbounded": chain_unbounded,
                "formula": "sum_abs_deviation(k)/sum_abs_deviation(frequency_norm)",
            },
        }
        previous_sum = total_abs

    non_empty = {
        stage: stats["sum_abs_deviation"]
        for stage, stats in stage_stats.items()
        if stats["samples"] > 0
    }
    dominant = (
        max(non_empty, key=lambda stage: (non_empty[stage], -STAGE_ORDER.index(stage)))
        if non_empty and max(non_empty.values()) > 0.0
        else "none"
    )
    spectrum["stages"] = stage_stats
    spectrum["dominant_source"] = dominant
    spectrum["per_sentence"] = samples

    per_document: dict[str, Any] = {}
    for doc_id in common_docs:
        rows = [sample for sample in samples if sample["doc_id"] == doc_id]
        per_document[doc_id] = {
            "samples": len(rows),
            "sum_abs_deviation": sum(
                float(sample["abs_deviation"]) for sample in rows
            ),
            "max_abs_deviation": max(
                (float(sample["abs_deviation"]) for sample in rows),
                default=0.0,
            ),
            "stages": {},
        }
        for stage in STAGE_ORDER:
            stage_rows = [sample for sample in rows if sample["stage"] == stage]
            abs_values = [
                float(sample["abs_deviation"]) for sample in stage_rows
            ]
            per_document[doc_id]["stages"][stage] = {
                "samples": len(stage_rows),
                "sum_abs_deviation": sum(abs_values),
                "max_abs_deviation": max(abs_values, default=0.0),
                "error_interval": {
                    "low_abs": min(abs_values, default=0.0),
                    "high_abs": max(abs_values, default=0.0),
                    "method": "observed_abs_min_max",
                },
            }
    spectrum["per_document"] = per_document
    return spectrum


def _threshold_summary(precisions: dict[str, Any]) -> dict[str, Any]:
    per_precision: dict[str, Any] = {}
    for mode, result in precisions.items():
        sentence_affected = 0
        rewrite_affected = 0
        marked_docs = []
        for doc_id, doc in sorted(result["documents"].items()):
            diagnostics = doc["threshold_diagnostics"]
            sentence_affected += diagnostics["sentence_admission"]["affected_count"]
            rewrite_affected += diagnostics["rewrite_admission"]["affected_count"]
            if diagnostics["changed_by_policy"]:
                marked_docs.append(doc_id)
        per_precision[mode] = {
            "configured_policy": (
                next(iter(result["documents"].values()))["threshold_diagnostics"][
                    "configured_policy"
                ]
                if result["documents"]
                else "unknown"
            ),
            "sentence_affected_count": sentence_affected,
            "rewrite_affected_count": rewrite_affected,
            "marked": sentence_affected + rewrite_affected > 0,
            "marker": (
                "POLICY_SENSITIVE"
                if sentence_affected + rewrite_affected > 0
                else "STABLE"
            ),
            "affected_doc_ids": marked_docs,
            "direction_summary": {
                "rejected_to_admitted": sentence_affected,
                "admitted_to_rejected": 0,
            },
        }
    return per_precision


def run_all_precisions(
    documents: Iterable[DocumentInput],
    config: PipelineConfig,
    *,
    input_errors: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    documents = tuple(documents)
    shared_input_errors = tuple(input_errors)
    precisions = {
        mode: run_precision(
            documents,
            config,
            mode,
            input_errors=shared_input_errors,
        )
        for mode in ("float64", "float32", "fixed")
    }
    reference = precisions["float64"]
    spectra = {
        mode: _build_error_spectrum(reference, precisions[mode], config, mode)
        for mode in ("float64", "float32", "fixed")
    }
    report: dict[str, Any] = {
        "schema_version": 1,
        "config_version": config.version,
        "config_checksum": sha256_object(config.config_payload()),
        "config": config.to_dict(include_vocab=True),
        "precisions": precisions,
        "error_spectra": spectra,
        "threshold_diagnostics": _threshold_summary(precisions),
        "input_errors": sort_errors(shared_input_errors),
        "error_documents": sorted(
            {
                error.get("doc_id", "*")
                for error in (
                    list(shared_input_errors)
                    + [
                        item
                        for result in precisions.values()
                        for item in result["errors"]
                    ]
                )
            }
        ),
        "attribution_rules": {
            "reference": "float64 within the same snapshot",
            "stage_order": list(STAGE_ORDER),
            "error_interval": "observed absolute deviation [min,max]",
            "amplification": "sum_abs_deviation(k)/sum_abs_deviation(k-1)",
            "chain_amplification": (
                "sum_abs_deviation(k)/sum_abs_deviation(frequency_norm)"
            ),
            "dominant_source": (
                "largest sum_abs_deviation across traceable stages"
            ),
        },
        "unattributed_errors": [
            error
            for result in precisions.values()
            for error in result["errors"]
            if error.get("code") == ErrorCode.UNATTRIBUTED
        ],
    }
    rounded = round_tree(report, config.output_precision)
    report = rounded
    report["run_hash"] = sha256_object(report)
    return report


def run_from_files(
    input_path: str | Path,
    config: PipelineConfig,
) -> tuple[dict[str, Any], list[DocumentInput], list[dict[str, Any]]]:
    documents, errors = load_documents(input_path)
    report = run_all_precisions(documents, config, input_errors=errors)
    return report, documents, errors

