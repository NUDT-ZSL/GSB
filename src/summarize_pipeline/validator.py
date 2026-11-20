from __future__ import annotations

import math
from dataclasses import replace
from typing import Any, Iterable

from .errors import ErrorCode
from .models import DocumentInput, PipelineConfig
from .pipeline import STAGE_ORDER, run_all_precisions
from .precision import PrecisionOps, quantize_scalar
from .textproc import tokenize
from .utils import canonical_json, sha256_object


def _issue(
    category: str,
    doc_id: str,
    message: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": ErrorCode.INCONSISTENT,
        "category": category,
        "stage": "validation",
        "doc_id": doc_id,
        "message": message,
        "detail": detail or {},
    }


def _close(left: float, right: float, tolerance: float) -> bool:
    return abs(float(left) - float(right)) <= tolerance


def _decision(value: float, threshold: float, policy: str, tolerance: float) -> bool:
    if policy == "strict":
        return value > threshold
    return value >= threshold - tolerance


def _validate_doc(
    *,
    mode: str,
    doc_id: str,
    doc: dict[str, Any],
    config: PipelineConfig,
    output_tolerance: float,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    ops = PrecisionOps.from_config(
        replace(config.precision, mode=mode), config.subnormal_floor
    )
    sentences = doc.get("sentences", [])
    sent_numbers = [int(item["sent_no"]) for item in sentences]
    if len(set(sent_numbers)) != len(sent_numbers):
        issues.append(
            _issue("sentence_uniqueness", doc_id, "duplicate sent_no values")
        )
    if sent_numbers != list(range(len(sent_numbers))):
        issues.append(
            _issue(
                "sentence_continuity",
                doc_id,
                "sent_no values are not contiguous from zero",
                {"sent_nos": sent_numbers},
            )
        )

    components = ("position", "length", "frequency", "coverage")
    weights = config.weights.to_dict()
    for sentence in sentences:
        sent_no = int(sentence["sent_no"])
        for component in components:
            value = float(sentence["components"][component])
            if not math.isfinite(value):
                issues.append(
                    _issue(
                        "finite_component",
                        doc_id,
                        f"{component} is non-finite",
                        {"sent_no": sent_no},
                    )
                )
        weighted = ops.mul(weights["position"], sentence["components"]["position"])
        weighted = ops.add(
            weighted, ops.mul(weights["length"], sentence["components"]["length"])
        )
        weighted = ops.add(
            weighted,
            ops.mul(weights["frequency"], sentence["components"]["frequency"]),
        )
        weighted = ops.add(
            weighted,
            ops.mul(weights["coverage"], sentence["components"]["coverage"]),
        )
        if not _close(
            weighted, float(sentence["weighted_sum"]), output_tolerance
        ):
            issues.append(
                _issue(
                    "weighted_sum_recompute",
                    doc_id,
                    "weighted_sum differs from component recomputation",
                    {
                        "sent_no": sent_no,
                        "expected": weighted,
                        "actual": sentence["weighted_sum"],
                    },
                )
            )
        smoothed = ops.div(
            ops.add(weighted, config.smoothing),
            ops.add(1.0, config.smoothing),
        )
        if not _close(
            smoothed, float(sentence["smoothed_score"]), output_tolerance
        ):
            issues.append(
                _issue(
                    "smoothing_recompute",
                    doc_id,
                    "smoothed_score differs from weighted-sum recomputation",
                    {
                        "sent_no": sent_no,
                        "expected": smoothed,
                        "actual": sentence["smoothed_score"],
                    },
                )
            )
        expected_admission = _decision(
            float(sentence["smoothed_score"]),
            config.sentence_threshold,
            config.threshold_policy,
            config.tolerance_abs,
        )
        if bool(sentence["admission"]["selected"]) != expected_admission:
            issues.append(
                _issue(
                    "threshold_recompute",
                    doc_id,
                    "sentence admission decision is not reproducible",
                    {
                        "sent_no": sent_no,
                        "expected": expected_admission,
                        "actual": sentence["admission"]["selected"],
                    },
                )
            )
        if mode == "fixed":
            for stage, stage_row in sentence["stages"].items():
                raw = stage_row.get("fixed_raw")
                value = stage_row.get("value")
                if raw is None or value is None:
                    continue
                expected_raw = quantize_scalar(
                    float(value), config.precision.scale_factor, config.precision.rounding
                )
                if int(raw) != expected_raw:
                    issues.append(
                        _issue(
                            "fixed_point_recompute",
                            doc_id,
                            f"{stage} fixed raw value is not reproducible",
                            {
                                "sent_no": sent_no,
                                "expected_raw": expected_raw,
                                "actual_raw": raw,
                            },
                        )
                    )
                dequantized = int(raw) / float(config.precision.scale_factor)
                if not _close(dequantized, float(value), output_tolerance):
                    issues.append(
                        _issue(
                            "fixed_point_dequantize",
                            doc_id,
                            f"{stage} dequantized value differs from fixed_raw/scale",
                            {
                                "sent_no": sent_no,
                                "expected": dequantized,
                                "actual": value,
                            },
                        )
                    )

    candidates = [
        sentence
        for sentence in sentences
        if _decision(
            float(sentence["smoothed_score"]),
            config.sentence_threshold,
            config.threshold_policy,
            config.tolerance_abs,
        )
    ]
    candidates.sort(
        key=lambda item: (-float(item["smoothed_score"]), int(item["sent_no"]))
    )
    used_chars = 0
    expected_selected: list[int] = []
    for sentence in candidates:
        if used_chars + int(sentence["char_len"]) <= config.length_budget:
            used_chars += int(sentence["char_len"])
            expected_selected.append(int(sentence["sent_no"]))
    actual_selected = [
        int(value) for value in doc["selection"]["selected_in_score_order"]
    ]
    if expected_selected != actual_selected:
        issues.append(
            _issue(
                "selection_recompute",
                doc_id,
                "selected sentence sequence is not reproducible",
                {"expected": expected_selected, "actual": actual_selected},
            )
        )
    expected_sorted = sorted(expected_selected)
    if [int(value) for value in doc["selection"]["selected_sent_nos"]] != expected_sorted:
        issues.append(
            _issue(
                "selection_set_recompute",
                doc_id,
                "selected_sent_nos does not match recomputed selection",
                {"expected": expected_sorted, "actual": doc["selection"]["selected_sent_nos"]},
            )
        )
    if [
        int(value) for value in doc["selection"]["selected_sent_no_set"]
    ] != expected_sorted:
        issues.append(
            _issue(
                "selection_set_recompute",
                doc_id,
                "selected_sent_no_set does not match recomputed selection",
            )
        )
    if int(doc["selection"]["budget_chars"]) != config.length_budget:
        issues.append(
            _issue(
                "selection_budget_record",
                doc_id,
                "recorded selection budget differs from configuration",
                {
                    "expected": config.length_budget,
                    "actual": doc["selection"]["budget_chars"],
                },
            )
        )
    expected_selected_set = set(expected_selected)
    for sentence in sentences:
        expected_flag = int(sentence["sent_no"]) in expected_selected_set
        if bool(sentence["selected"]) != expected_flag:
            issues.append(
                _issue(
                    "sentence_selected_flag",
                    doc_id,
                    "sentence selected flag differs from selection set",
                    {"sent_no": sentence["sent_no"]},
                )
            )
    if int(doc["selection"]["used_chars"]) != used_chars:
        issues.append(
            _issue(
                "budget_recompute",
                doc_id,
                "used budget differs from sentence char_len recomputation",
                {
                    "expected": used_chars,
                    "actual": doc["selection"]["used_chars"],
                },
            )
        )

    sentence_by_no = {int(item["sent_no"]): item for item in sentences}
    selected_sentences = [
        sentence_by_no[sent_no] for sent_no in expected_selected
    ]
    denominator_row = doc["rewrite_normalization"]["denominator"]
    expected_denominator = ops.sum(
        [float(item["smoothed_score"]) for item in selected_sentences]
    )
    if not _close(
        expected_denominator,
        float(denominator_row["raw_sum"]),
        output_tolerance,
    ):
        issues.append(
            _issue(
                "rewrite_denominator_recompute",
                doc_id,
                "rewrite denominator is not the selected score sum",
                {
                    "expected": expected_denominator,
                    "actual": denominator_row["raw_sum"],
                },
            )
        )
    if not denominator_row.get("source"):
        issues.append(
            _issue(
                "rewrite_denominator_source",
                doc_id,
                "rewrite denominator source is missing",
            )
        )

    normalization = doc["rewrite_normalization"]
    adjusted_denominator = float(normalization["denominator"]["adjusted"])
    exponent = float(normalization["temperature"]["exponent"])
    temperature_denominator = float(normalization["temperature"]["denominator"])
    for decision in normalization["decisions"]:
        sent_no = int(decision["sent_no"])
        sentence = sentence_by_no[sent_no]
        expected_adjusted = ops.div_protected(
            ops.add(float(sentence["smoothed_score"]), config.smoothing),
            adjusted_denominator,
        )[0]
        if not _close(
            expected_adjusted, float(decision["adjusted"]), output_tolerance
        ):
            issues.append(
                _issue(
                    "rewrite_adjustment_recompute",
                    doc_id,
                    "temperature-adjusted base score is not reproducible",
                    {
                        "sent_no": sent_no,
                        "expected": expected_adjusted,
                        "actual": decision["adjusted"],
                    },
                )
            )
        powered = ops.power(expected_adjusted, exponent)
        expected_normalized = ops.div_protected(
            powered, temperature_denominator
        )[0]
        if not _close(
            expected_normalized,
            float(decision["normalized"]),
            output_tolerance,
        ):
            issues.append(
                _issue(
                    "rewrite_normalization_recompute",
                    doc_id,
                    "normalized rewrite score is not reproducible",
                    {
                        "sent_no": sent_no,
                        "expected": expected_normalized,
                        "actual": decision["normalized"],
                    },
                )
            )
        expected_rewrite_decision = _decision(
            float(decision["normalized"]),
            config.rewrite_threshold,
            config.threshold_policy,
            config.tolerance_abs,
        )
        if bool(decision["decision"]["selected"]) != expected_rewrite_decision:
            issues.append(
                _issue(
                    "rewrite_threshold_recompute",
                    doc_id,
                    "rewrite admission decision is not reproducible",
                    {
                        "sent_no": sent_no,
                        "expected": expected_rewrite_decision,
                        "actual": decision["decision"]["selected"],
                    },
                )
            )

    rewrite_units = doc["rewrite_units"]
    ref_set = set(doc["ref_summary_units"])
    for unit in rewrite_units:
        sent_no = int(unit["sent_no"])
        decision_row = next(
            (
                item
                for item in normalization["decisions"]
                if int(item["sent_no"]) == sent_no
            ),
            None,
        )
        if decision_row is None:
            issues.append(
                _issue(
                    "rewrite_unit_link",
                    doc_id,
                    "rewrite unit has no normalization decision",
                    {"sent_no": sent_no},
                )
            )
            continue
        if not _close(
            float(unit["normalized_score"]),
            float(decision_row["normalized"]),
            output_tolerance,
        ):
            issues.append(
                _issue(
                    "rewrite_unit_score",
                    doc_id,
                    "rewrite unit score differs from normalization decision",
                    {"sent_no": sent_no},
                )
            )
        if len(unit["rewritten_text"]) != int(unit["rewritten_chars"]):
            issues.append(
                _issue(
                    "rewrite_char_count",
                    doc_id,
                    "rewritten_chars does not match rewritten_text",
                    {"sent_no": sent_no},
                )
            )
        text_tokens = list(tokenize(unit["rewritten_text"]))
        if text_tokens != list(unit["kept_tokens"]):
            issues.append(
                _issue(
                    "rewrite_tokens_recompute",
                    doc_id,
                    "kept_tokens cannot be rederived from rewritten_text",
                    {"sent_no": sent_no},
                )
            )
        expected_hits = sorted(set(text_tokens) & ref_set)
        if expected_hits != sorted(unit["ref_hits"]):
            issues.append(
                _issue(
                    "rewrite_ref_hits_recompute",
                    doc_id,
                    "ref_hits cannot be rederived from kept tokens",
                    {"sent_no": sent_no},
                )
            )

    recomputed_text = " ".join(unit["rewritten_text"] for unit in rewrite_units)
    if recomputed_text != doc["rewritten_text"]:
        issues.append(
            _issue(
                "rewritten_text_recompute",
                doc_id,
                "document rewritten_text is not the unit concatenation",
            )
        )
    ref_units = list(doc["ref_summary_units"])
    covered: set[str] = set()
    for unit in rewrite_units:
        covered.update(set(unit["ref_hits"]))
    expected_coverage = len(covered) / len(ref_units) if ref_units else 0.0
    expected_compression = (
        len(recomputed_text) / int(doc["original_chars"])
        if int(doc["original_chars"]) > 0
        else 0.0
    )
    target = config.compression_ratio
    expected_efficiency = max(
        0.0, 1.0 - abs(expected_compression - target) / target
    )
    expected_quality = 0.7 * expected_coverage + 0.3 * expected_efficiency
    quality = doc["quality"]
    for category, expected, actual in (
        ("quality_coverage_recompute", expected_coverage, quality["coverage"]),
        (
            "quality_compression_recompute",
            expected_compression,
            quality["compression_ratio"],
        ),
        (
            "quality_efficiency_recompute",
            expected_efficiency,
            quality["compression_efficiency"],
        ),
        (
            "quality_score_recompute",
            expected_quality,
            quality["quality_score"],
        ),
    ):
        if not _close(expected, float(actual), output_tolerance):
            issues.append(
                _issue(
                    category,
                    doc_id,
                    "quality metric is not independently reproducible",
                    {"expected": expected, "actual": actual},
                )
            )
    if sorted(covered) != sorted(quality["covered_ref_units"]):
        issues.append(
            _issue(
                "quality_coverage_units",
                doc_id,
                "covered_ref_units differs from unit ref_hits",
            )
        )
    if int(quality["output_chars"]) != len(recomputed_text):
        issues.append(
            _issue(
                "quality_output_chars_recompute",
                doc_id,
                "output_chars differs from rewritten text length",
                {
                    "expected": len(recomputed_text),
                    "actual": quality["output_chars"],
                },
            )
        )
    if int(quality["original_chars"]) != int(doc["original_chars"]):
        issues.append(
            _issue(
                "quality_original_chars_recompute",
                doc_id,
                "quality original_chars differs from document original_chars",
            )
        )

    diagnostics = doc["threshold_diagnostics"]
    strict_sentence = sum(
        1 for item in sentences if bool(item["admission"]["strict"])
    )
    tolerance_sentence = sum(
        1 for item in sentences if bool(item["admission"]["tolerance"])
    )
    strict_rewrite = sum(
        1
        for item in normalization["decisions"]
        if bool(item["decision"]["strict"])
    )
    tolerance_rewrite = sum(
        1
        for item in normalization["decisions"]
        if bool(item["decision"]["tolerance"])
    )
    sentence_decisions = [
        {
            "sent_no": int(item["sent_no"]),
            "strict": bool(item["admission"]["strict"]),
            "tolerance": bool(item["admission"]["tolerance"]),
        }
        for item in sentences
    ]
    rewrite_decisions = [
        {
            "sent_no": int(item["sent_no"]),
            "strict": bool(item["decision"]["strict"]),
            "tolerance": bool(item["decision"]["tolerance"]),
        }
        for item in normalization["decisions"]
    ]
    for category, expected_strict, expected_tolerance, actual in (
        (
            "sentence_threshold_diagnostics",
            strict_sentence,
            tolerance_sentence,
            diagnostics["sentence_admission"],
        ),
        (
            "rewrite_threshold_diagnostics",
            strict_rewrite,
            tolerance_rewrite,
            diagnostics["rewrite_admission"],
        ),
    ):
        if (
            int(actual["strict_admitted"]) != expected_strict
            or int(actual["tolerance_admitted"]) != expected_tolerance
        ):
            issues.append(
                _issue(
                    category,
                    doc_id,
                    "threshold policy diagnostics cannot be recomputed",
                    {
                        "expected_strict": expected_strict,
                        "expected_tolerance": expected_tolerance,
                        "actual": actual,
                    },
                )
            )
    for category, decision_rows, actual in (
        (
            "sentence_threshold_directions",
            sentence_decisions,
            diagnostics["sentence_admission"],
        ),
        (
            "rewrite_threshold_directions",
            rewrite_decisions,
            diagnostics["rewrite_admission"],
        ),
    ):
        expected_rejected = sorted(
            int(item["sent_no"])
            for item in decision_rows
            if not item["strict"] and item["tolerance"]
        )
        expected_admitted = sorted(
            int(item["sent_no"])
            for item in decision_rows
            if item["strict"] and not item["tolerance"]
        )
        if actual["directions"]["rejected_to_admitted"] != expected_rejected:
            issues.append(
                _issue(
                    category,
                    doc_id,
                    "rejected_to_admitted direction list is not reproducible",
                )
            )
        if actual["directions"]["admitted_to_rejected"] != expected_admitted:
            issues.append(
                _issue(
                    category,
                    doc_id,
                    "admitted_to_rejected direction list is not reproducible",
                )
            )
    return issues


def _validate_fixed_spectrum(
    *,
    doc_id: str,
    spectrum: dict[str, Any],
    config: PipelineConfig,
) -> list[dict[str, Any]]:
    issues = []
    rows = [
        row for row in spectrum.get("per_sentence", []) if row.get("doc_id") == doc_id
    ]
    for row in rows:
        if "fixed_raw" not in row:
            continue
        sent_no = int(row["sent_no"])
        reference = float(row["reference"])
        raw = int(row["fixed_raw"])
        expected_reference_raw = quantize_scalar(
            reference,
            config.precision.scale_factor,
            config.precision.rounding,
        )
        if int(row["quantized_reference_raw"]) != expected_reference_raw:
            issues.append(
                _issue(
                    "fixed_reference_quantization",
                    doc_id,
                    "quantized reference raw value cannot be rederived",
                    {
                        "sent_no": sent_no,
                        "stage": row["stage"],
                        "expected": expected_reference_raw,
                        "actual": row["quantized_reference_raw"],
                    },
                )
            )
        dequantized = raw / float(config.precision.scale_factor)
        expected_delta = (raw - expected_reference_raw) / float(
            config.precision.scale_factor
        )
        if not _close(
            dequantized,
            float(row["dequantized_actual"]),
            10.0 ** (-config.output_precision),
        ):
            issues.append(
                _issue(
                    "fixed_dequantization",
                    doc_id,
                    "fixed raw cannot be dequantized with the declared scale",
                    {"sent_no": sent_no, "stage": row["stage"]},
                )
            )
        if not _close(
            expected_delta,
            float(row["quantization_delta_from_reference"]),
            10.0 ** (-config.output_precision),
        ):
            issues.append(
                _issue(
                    "fixed_deviation_recompute",
                    doc_id,
                    "fixed deviation is not reconstructible from scale and rounding",
                    {
                        "sent_no": sent_no,
                        "stage": row["stage"],
                        "expected": expected_delta,
                        "actual": row["quantization_delta_from_reference"],
                    },
                )
            )
    return issues


def validate_report(
    report: dict[str, Any],
    config: PipelineConfig,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}
    output_tolerance = max(
        1e-8,
        20.0 * (10.0 ** (-config.output_precision)),
        (10.0 / config.precision.scale_factor)
        if config.precision.mode == "fixed"
        else 0.0,
    )
    expected_hash = report.get("run_hash")
    hash_payload = {key: value for key, value in report.items() if key != "run_hash"}
    recomputed_hash = sha256_object(hash_payload)
    if expected_hash != recomputed_hash:
        issues.append(
            _issue(
                "run_hash",
                "*",
                "run_hash does not match canonical report content",
                {"expected": recomputed_hash, "actual": expected_hash},
            )
        )
    if report.get("config_checksum") != sha256_object(config.config_payload()):
        issues.append(
            _issue(
                "config_checksum",
                "*",
                "report config checksum does not match the supplied config",
            )
        )

    precisions = report.get("precisions", {})
    reference = precisions.get("float64")
    if reference is None:
        issues.append(_issue("missing_reference", "*", "float64 precision is missing"))
        reference_ids: set[str] = set()
    else:
        reference_ids = set(reference.get("document_ids", []))
        if reference_ids != set(reference.get("documents", {})):
            issues.append(
                _issue(
                    "doc_id_set",
                    "*",
                    "float64 document_ids does not match documents",
                )
            )

    for mode in ("float64", "float32", "fixed"):
        result = precisions.get(mode)
        if result is None:
            issues.append(
                _issue("missing_precision", "*", f"precision {mode} is missing")
            )
            continue
        mode_ids = set(result.get("document_ids", []))
        if mode_ids != reference_ids:
            issues.append(
                _issue(
                    "doc_id_set",
                    "*",
                    "document id set differs from the float64 reference",
                    {
                        "precision": mode,
                        "expected": sorted(reference_ids),
                        "actual": sorted(mode_ids),
                    },
                )
            )
        if set(result.get("documents", {})) != mode_ids:
            issues.append(
                _issue(
                    "doc_id_set",
                    "*",
                    f"{mode} document_ids does not match documents",
                )
            )
        if result.get("precision_record", {}).get("tier") != mode:
            issues.append(
                _issue(
                    "precision_record",
                    "*",
                    f"{mode} precision record is missing or mismatched",
                )
            )
        for doc_id, doc in sorted(result.get("documents", {}).items()):
            if doc.get("doc_id") != doc_id:
                issues.append(
                    _issue(
                        "doc_id_mismatch",
                        doc_id,
                        "document key and internal doc_id differ",
                    )
                )
            issues.extend(
                _validate_doc(
                    mode=mode,
                    doc_id=doc_id,
                    doc=doc,
                    config=config,
                    output_tolerance=max(
                        output_tolerance,
                        1e-6 if mode == "float32" else 0.0,
                        (
                            100.0 / config.precision.scale_factor
                            if mode == "fixed"
                            else 0.0
                        ),
                    ),
                )
            )
        checks[f"{mode}_documents"] = len(result.get("documents", {}))

    spectra = report.get("error_spectra", {})
    for mode in ("float64", "float32", "fixed"):
        spectrum = spectra.get(mode)
        if spectrum is None:
            issues.append(
                _issue("missing_error_spectrum", "*", f"spectrum {mode} is missing")
            )
            continue
        spectrum_docs = set(spectrum.get("per_document", {}))
        if spectrum_docs != reference_ids:
            issues.append(
                _issue(
                    "doc_id_set",
                    "*",
                    f"{mode} error spectrum document set differs",
                    {
                        "expected": sorted(reference_ids),
                        "actual": sorted(spectrum_docs),
                    },
                )
            )
        sample_docs = {
            str(row["doc_id"]) for row in spectrum.get("per_sentence", [])
        }
        if not sample_docs.issubset(reference_ids):
            issues.append(
                _issue(
                    "doc_id_set",
                    "*",
                    f"{mode} error samples contain unknown document ids",
                    {"actual": sorted(sample_docs - reference_ids)},
                )
            )
        for stage in STAGE_ORDER:
            stats = spectrum.get("stages", {}).get(stage, {})
            interval = stats.get("error_interval", {})
            if float(interval.get("low_abs", 0.0)) > float(
                interval.get("high_abs", 0.0)
            ):
                issues.append(
                    _issue(
                        "error_interval_order",
                        "*",
                        f"{mode}/{stage} error interval is reversed",
                    )
                )
            for doc_id, doc_stats in spectrum.get("per_document", {}).items():
                doc_interval = doc_stats.get("stages", {}).get(stage, {}).get(
                    "error_interval", {}
                )
                if float(doc_interval.get("low_abs", 0.0)) > float(
                    doc_interval.get("high_abs", 0.0)
                ):
                    issues.append(
                        _issue(
                            "error_interval_order",
                            doc_id,
                            f"{mode}/{stage} per-document interval is reversed",
                        )
                    )
        if mode == "fixed":
            for doc_id in sorted(spectrum_docs):
                issues.extend(
                    _validate_fixed_spectrum(
                        doc_id=doc_id, spectrum=spectrum, config=config
                    )
                )

    issues.sort(
        key=lambda item: (
            str(item.get("doc_id", "")),
            str(item.get("category", "")),
            str(item.get("message", "")),
        )
    )
    payload = {
        "ok": not issues,
        "error": None
        if not issues
        else {"code": ErrorCode.INCONSISTENT, "stage": "validation"},
        "issues": issues,
        "issue_count": len(issues),
        "checks": checks,
        "output_tolerance": output_tolerance,
    }
    payload["validation_hash"] = sha256_object(payload)
    return payload


def verify_reproducibility(
    documents: Iterable[DocumentInput],
    config: PipelineConfig,
    *,
    input_errors: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    documents = tuple(documents)
    input_errors = tuple(input_errors)
    first = run_all_precisions(documents, config, input_errors=input_errors)
    second = run_all_precisions(documents, config, input_errors=input_errors)
    first_hash = first["run_hash"]
    second_hash = second["run_hash"]
    full_equal = canonical_json(first) == canonical_json(second)
    validation = validate_report(first, config)
    reproducibility = {
        "first_run_hash": first_hash,
        "second_run_hash": second_hash,
        "hash_match": first_hash == second_hash and full_equal,
        "full_output_equal": full_equal,
    }
    issues = list(validation["issues"])
    if not reproducibility["hash_match"]:
        issues.append(
            _issue(
                "reproducibility",
                "*",
                "two complete runs produced different output hashes",
                reproducibility,
            )
        )
    result = {
        "ok": not issues,
        "error": None
        if not issues
        else {"code": ErrorCode.INCONSISTENT, "stage": "validation"},
        "issues": issues,
        "issue_count": len(issues),
        "validation": validation,
        "reproducibility": reproducibility,
    }
    result["verification_hash"] = sha256_object(result)
    return result

