"""Deterministic serializers for all supported config versions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

from .config_io import _checksum_json, _checksum_text
from .errors import IntentError
from .models import (
    DEFAULT_FALLBACK,
    DEFAULT_THRESHOLD,
    ConfigDocument,
    IntentSpec,
)


def _labels_header(controlled_labels: Iterable[str]) -> str:
    return ",".join(sorted(controlled_labels))


def _finalize_text_config(header_lines: list[str], body_lines: list[str]) -> str:
    prefix = "\n".join(header_lines) + "\n"
    body = "\n".join(body_lines) + "\n"
    digest = _checksum_text(prefix + body)
    return prefix + f"# checksum: {digest}\n" + body


def serialize_v1(document: ConfigDocument) -> str:
    header = [
        "# version: v1",
        f"# snapshot_version: {document.snapshot_version}",
        f"# controlled_labels: {_labels_header(document.controlled_labels)}",
        f"# fallback_intent: {document.global_fallback}",
    ]
    body: list[str] = []
    for index, spec in enumerate(document.intents):
        priority = spec.priority if spec.priority is not None else 1000 - index
        body.append(f"{spec.intent}|{','.join(spec.keywords)}|{priority}")
    return _finalize_text_config(header, body)


def serialize_v2(document: ConfigDocument) -> str:
    header = [
        "# version: v2",
        f"# snapshot_version: {document.snapshot_version}",
        f"# controlled_labels: {_labels_header(document.controlled_labels)}",
        f"# fallback_intent: {document.global_fallback}",
    ]
    body: list[str] = []
    for spec in document.intents:
        threshold = (
            spec.threshold if spec.threshold is not None else DEFAULT_THRESHOLD
        )
        fallback = spec.fallback_intent or document.global_fallback
        body.append(
            "|".join(
                (
                    f"intent={spec.intent}",
                    f"keywords={','.join(spec.keywords)}",
                    f"threshold={threshold:.6f}",
                    f"fallback_intent={fallback}",
                )
            )
        )
    return _finalize_text_config(header, body)


def _v3_intent_payload(spec: IntentSpec) -> dict[str, object]:
    if (
        spec.threshold is None
        or spec.fallback_intent is None
        or spec.label_path is None
        or spec.slots is None
        or spec.weight_vector is None
    ):
        raise IntentError(
            "E_CONFIG_INVALID",
            "写出 v3 前必须完成 v2 到 v3 迁移",
            stage="write",
            field="intent",
            intent=spec.intent,
        )
    if len(spec.weight_vector) != len(spec.keywords):
        raise IntentError(
            "E_CONFIG_INVALID",
            "weight_vector 长度与关键词数量不一致",
            stage="write",
            field="weight_vector",
            intent=spec.intent,
        )
    return {
        "intent": spec.intent,
        "keywords": list(spec.keywords),
        "threshold": spec.threshold,
        "fallback_intent": spec.fallback_intent,
        "label_path": list(spec.label_path),
        "slots": [slot.to_dict() for slot in spec.slots],
        "weight_vector": list(spec.weight_vector),
    }


def serialize_v3(document: ConfigDocument) -> str:
    payload: dict[str, object] = {
        "version": "v3",
        "snapshot_version": document.snapshot_version,
        "controlled_labels": sorted(document.controlled_labels),
        "fallback_intent": document.global_fallback,
        "intents": [_v3_intent_payload(spec) for spec in document.intents],
    }
    checksum = _checksum_json(payload)
    payload["checksum"] = checksum
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def serialize_document(document: ConfigDocument, version: str) -> str:
    if version == "v1":
        return serialize_v1(document)
    if version == "v2":
        return serialize_v2(document)
    if version == "v3":
        return serialize_v3(document)
    raise IntentError(
        "E_VERSION_UNSUPPORTED",
        f"不支持的写出版本：{version}",
        stage="write",
        version=version,
    )


def write_text_atomic(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, target)


def write_config_document(
    document: ConfigDocument,
    path: str | Path,
    *,
    version: str | None = None,
) -> ConfigDocument:
    target_version = version or document.source_version
    text = serialize_document(document, target_version)
    write_text_atomic(path, text)
    # Importing lazily avoids a reader/writer import cycle.
    from .config_io import read_config_document

    return read_config_document(path)
