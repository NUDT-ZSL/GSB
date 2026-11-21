from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .config import load_config
from .errors import BusinessError, ErrorCode, ErrorCollector
from .models import load_labels, load_lexicon
from .pipeline import CascadePipeline
from .validate import read_input_lines, validate_offsets


def _scenario(
    name: str,
    code: str,
    *,
    doc_id: str | None = None,
    line_no: int | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "scenario": name,
        "expected_code": code,
        "actual_code": code,
        "doc_id": doc_id,
        "line_no": line_no,
        "detail": detail or {},
    }


def collect_error_scenarios(data_dir: str | Path) -> dict[str, Any]:
    data_dir = Path(data_dir)
    config = load_config(data_dir / "config_default.json")
    labels = load_labels(data_dir / "labels_v1.json")
    scenarios: list[dict[str, Any]] = []

    for name, filename, expected_code in (
        ("duplicate_doc_id", "invalid/dup_doc.jsonl", ErrorCode.E_DUP_DOC),
        ("line_no_gap", "invalid/line_no.jsonl", ErrorCode.E_LINE_NO),
        ("empty_text", "invalid/empty_text.jsonl", ErrorCode.E_TEXT_EMPTY),
        ("invalid_punct_class", "invalid/punct_class.jsonl", ErrorCode.E_INPUT_FORMAT),
    ):
        collector = ErrorCollector()
        read_input_lines(data_dir / filename, config, collector)
        first = collector.errors[0].to_dict() if collector.errors else {}
        scenarios.append(
            _scenario(
                name,
                str(first.get("code", "MISSING")),
                doc_id=first.get("doc_id"),
                line_no=first.get("line_no"),
                detail={"expected": expected_code, "all_codes": [e.code for e in collector.errors]},
            )
        )

    for name, filename, expected_code in (
        ("dict_format", "invalid/dict_format.json", ErrorCode.E_DICT_FORMAT),
        ("dict_label_out_of_set", "invalid/dict_label.json", ErrorCode.E_LABEL_INVALID),
    ):
        try:
            load_lexicon(data_dir / filename, labels)
            code = "NO_ERROR"
            detail: dict[str, Any] = {}
        except BusinessError as exc:
            code = exc.code
            detail = exc.details
        scenarios.append(
            _scenario(
                name,
                code,
                detail={"expected": expected_code, **detail},
            )
        )

    bad_offsets = json.loads(
        (data_dir / "invalid/bad_offsets.json").read_text(encoding="utf-8")
    )
    try:
        validate_offsets(
            bad_offsets["text"],
            bad_offsets["tokens"],
            doc_id=bad_offsets["doc_id"],
            line_no=bad_offsets["line_no"],
        )
        scenarios.append(
            _scenario(
                "offset_length_mismatch",
                "NO_ERROR",
                doc_id=bad_offsets["doc_id"],
                line_no=bad_offsets["line_no"],
            )
        )
    except BusinessError as exc:
        scenarios.append(
            _scenario(
                "offset_length_mismatch",
                exc.code,
                doc_id=exc.doc_id,
                line_no=exc.line_no,
                detail={"expected": ErrorCode.E_OFFSET_LEN, **exc.details},
            )
        )

    with tempfile.TemporaryDirectory(prefix="poscascade_unattributed_") as temp_dir:
        pipeline = CascadePipeline(
            input_path=data_dir / "lines.jsonl",
            dict_path=data_dir / "dict_v1.json",
            labels_path=data_dir / "labels_v1.json",
            config=data_dir / "config_default.json",
            store_path=Path(temp_dir) / "store.sqlite3",
            output_dir=Path(temp_dir) / "out",
            fault_injection="segment",
        )
        outcome = pipeline.run()
        scenarios.append(
            _scenario(
                "unattributed_fault",
                outcome.code,
                detail={
                    "expected": ErrorCode.E_UNATTRIBUTED,
                    "errors": outcome.errors,
                },
            )
        )

    all_match = all(
        item["actual_code"] == item["detail"].get("expected", item["actual_code"])
        for item in scenarios
    )
    return {
        "ok": all_match,
        "code": ErrorCode.OK if all_match else ErrorCode.E_INCONSISTENT,
        "scenario_count": len(scenarios),
        "scenarios": scenarios,
    }
