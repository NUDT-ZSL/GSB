from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from .errors import ErrorCode, fail
from .models import (
    Arc,
    ArcGraph,
    BidirectionalChain,
    CrossSentenceRef,
    FormatVersion,
    SentenceGraph,
)
from .source import SourceLike, read_source

V1_FIELDS = ("token_id", "head", "deprel")
V2_FIELDS = (
    "token_id",
    "head",
    "deprel",
    "confidence_low",
    "confidence_high",
)


@dataclass(frozen=True)
class LoadedDocument:
    graph: ArcGraph
    version: FormatVersion
    source_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version.value,
            "source_name": self.source_name,
            "sent_ids": self.graph.sent_ids(),
            "node_count": self.graph.node_count(),
        }


def _format_corrupt(
    message: str,
    *,
    line: int | None,
    field: str,
    **extra: Any,
) -> None:
    details: dict[str, Any] = {"field": field}
    if line is not None:
        details["line"] = line
    details.update(extra)
    fail(ErrorCode.FORMAT_CORRUPT, message, **details)


def _unsupported(version: str, *, line: int | None, field: str = "version") -> None:
    details: dict[str, Any] = {"field": field, "version": version}
    if line is not None:
        details["line"] = line
    fail(
        ErrorCode.VERSION_UNSUPPORTED,
        f"unsupported dependency arc format version: {version!r}",
        **details,
    )


def _line_for(text: str, needle: str) -> int:
    index = text.find(needle)
    if index < 0:
        return 1
    return text.count("\n", 0, index) + 1


def _parse_int(
    raw: str,
    *,
    line: int,
    field: str,
    sent_id: str | None = None,
) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        _format_corrupt(
            f"field {field!r} is not an integer",
            line=line,
            field=field,
            sent_id=sent_id,
            value=raw,
        )
        raise AssertionError("unreachable")


def _parse_float(
    raw: str,
    *,
    line: int,
    field: str,
    sent_id: str | None = None,
) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _format_corrupt(
            f"field {field!r} is not numeric",
            line=line,
            field=field,
            sent_id=sent_id,
            value=raw,
        )
        raise AssertionError("unreachable")
    if not math.isfinite(value):
        _format_corrupt(
            f"field {field!r} must be finite",
            line=line,
            field=field,
            sent_id=sent_id,
            value=raw,
        )
    return value


def _json_int(
    value: Any,
    *,
    sent_id: str,
    field: str,
    line: int,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _format_corrupt(
            f"field {field!r} is not an integer",
            line=line,
            field=field,
            sent_id=sent_id,
            value=value,
        )
    return value


def _json_float(
    value: Any,
    *,
    sent_id: str,
    field: str,
    line: int,
) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        _format_corrupt(
            f"field {field!r} is not numeric",
            line=line,
            field=field,
            sent_id=sent_id,
            value=value,
        )
    number = float(value)
    if not math.isfinite(number):
        _format_corrupt(
            f"field {field!r} must be finite",
            line=line,
            field=field,
            sent_id=sent_id,
            value=value,
        )
    return number


def _json_str(
    value: Any,
    *,
    sent_id: str,
    field: str,
    line: int,
) -> str:
    if not isinstance(value, str) or not value:
        _format_corrupt(
            f"field {field!r} must be a non-empty string",
            line=line,
            field=field,
            sent_id=sent_id,
            value=value,
        )
    return value


def _json_bool(
    value: Any,
    *,
    sent_id: str,
    field: str,
    line: int,
) -> bool:
    if not isinstance(value, bool):
        _format_corrupt(
            f"field {field!r} must be boolean",
            line=line,
            field=field,
            sent_id=sent_id,
            value=value,
        )
    return value


def _require_object(
    value: Any,
    *,
    sent_id: str | None,
    field: str,
    line: int,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _format_corrupt(
            f"field {field!r} must be an object",
            line=line,
            field=field,
            sent_id=sent_id,
        )
    return value


def _required(
    mapping: dict[str, Any],
    key: str,
    *,
    sent_id: str | None,
    field_prefix: str,
    line: int,
) -> Any:
    if key not in mapping:
        field_name = f"{field_prefix}.{key}" if field_prefix else key
        _format_corrupt(
            f"required field {field_name!r} is missing",
            line=line,
            field=field_name,
            sent_id=sent_id,
        )
    return mapping[key]


def _validate_confidence(
    low: float,
    high: float,
    *,
    line: int,
    sent_id: str,
    field_prefix: str,
) -> None:
    if not (0.0 <= low <= high <= 1.0):
        _format_corrupt(
            "confidence interval must satisfy 0 <= low <= high <= 1",
            line=line,
            field=f"{field_prefix}.confidence",
            sent_id=sent_id,
            confidence_low=low,
            confidence_high=high,
        )


def _detect_version(text: str, source_name: str) -> FormatVersion:
    cleaned = text.lstrip("\ufeff")
    stripped = cleaned.lstrip()
    if not stripped:
        _format_corrupt(
            "dependency arc source is empty",
            line=1,
            field="format",
            source=source_name,
        )
    if stripped.startswith("{"):
        return FormatVersion.V3
    first_line = stripped.splitlines()[0].strip()
    if first_line.startswith("#"):
        header_match = re.match(
            r"^#\s*(?:format|version|deparc[-_ ]?version)\b(.*)$",
            first_line,
            flags=re.IGNORECASE,
        )
        if header_match:
            remainder = header_match.group(1).strip()
            if not remainder.startswith("="):
                _format_corrupt(
                    "format header is truncated or malformed",
                    line=1,
                    field="format",
                    header=first_line,
                )
            value = remainder[1:].strip()
            if not value:
                _format_corrupt(
                    "format header is truncated or malformed",
                    line=1,
                    field="format",
                    header=first_line,
                )
            return _parse_declared_version(value, line=1)
        short_match = re.fullmatch(r"#\s*(v\d+)\s*", first_line, flags=re.IGNORECASE)
        if short_match:
            return _parse_declared_version(short_match.group(1), line=1)
        if re.match(r"^#\s*format\b", first_line, flags=re.IGNORECASE):
            _format_corrupt(
                "format header is truncated or malformed",
                line=1,
                field="format",
                header=first_line,
            )
    for index, raw_line in enumerate(cleaned.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("{"):
            return FormatVersion.V3
        if "token_id=" in line or "confidence_low=" in line:
            return FormatVersion.V2
        if "|" in line:
            return FormatVersion.V1
        _unsupported("<unresolved>", line=index)
    _format_corrupt(
        "no format header or recognizable field set was found",
        line=1,
        field="format",
        source=source_name,
    )
    raise AssertionError("unreachable")


def _parse_declared_version(raw: str, *, line: int) -> FormatVersion:
    normalized = raw.strip().lower()
    if normalized == "v":
        _format_corrupt(
            "format header is truncated or malformed",
            line=line,
            field="format",
            header=raw,
        )
    if normalized in ("v1", "1"):
        return FormatVersion.V1
    if normalized in ("v2", "2"):
        return FormatVersion.V2
    if normalized in ("v3", "3"):
        return FormatVersion.V3
    _unsupported(raw, line=line)
    raise AssertionError("unreachable")


def detect_format_version(source: SourceLike) -> FormatVersion:
    source_text = read_source(source)
    return _detect_version(source_text.text, source_text.name)


def parse_format(
    source: SourceLike,
    *,
    format_hint: str | FormatVersion | None = None,
) -> LoadedDocument:
    source_text = read_source(source)
    text = source_text.text.lstrip("\ufeff")
    detected = _detect_version(text, source_text.name)
    version = FormatVersion.parse(format_hint) if format_hint is not None else detected
    if format_hint is not None and version != detected:
        _format_corrupt(
            "explicit format hint conflicts with the format header/field set",
            line=1,
            field="format",
            hint=version.value,
            detected=detected.value,
        )
    if version is FormatVersion.V1 or version is FormatVersion.V2:
        graph = _parse_v1_or_v2(text, version=version, source_name=source_text.name)
    else:
        graph = _parse_v3(text, source_name=source_text.name)
    return LoadedDocument(graph=graph, version=version, source_name=source_text.name)


def _parse_v1_or_v2(
    text: str,
    *,
    version: FormatVersion,
    source_name: str,
) -> ArcGraph:
    graph = ArcGraph(format_version=version, source_name=source_name)
    current_sent_id: str | None = None
    current_arcs: dict[int, Arc] = {}
    current_root: int | None = None
    seen_root_metadata = False
    sentence_start_line: int | None = None
    seen_sent_ids: set[str] = set()

    def flush(end_line: int) -> None:
        nonlocal current_sent_id, current_arcs, current_root
        nonlocal seen_root_metadata, sentence_start_line
        if current_sent_id is None:
            return
        if not current_arcs:
            _format_corrupt(
                "sentence contains no dependency arcs",
                line=sentence_start_line,
                field="tokens",
                sent_id=current_sent_id,
            )
        if version is FormatVersion.V2:
            if not seen_root_metadata:
                _format_corrupt(
                    "v2 sentence is missing its explicit root marker",
                    line=sentence_start_line,
                    field="root",
                    sent_id=current_sent_id,
                )
            if current_root is None:
                _format_corrupt(
                    "v2 root marker is missing",
                    line=sentence_start_line,
                    field="root",
                    sent_id=current_sent_id,
                )
        sentence = SentenceGraph(
            sent_id=current_sent_id,
            arcs=current_arcs,
            root_marker=current_root if version is FormatVersion.V2 else None,
            root_source="observed" if version is FormatVersion.V2 else "inferred",
        )
        graph.add(sentence)
        seen_sent_ids.add(current_sent_id)
        current_sent_id = None
        current_arcs = {}
        current_root = None
        seen_root_metadata = False
        sentence_start_line = None

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if re.match(r"^#\s*format\b", line, flags=re.IGNORECASE):
                continue
            sent_match = re.fullmatch(r"#\s*sent_id\s*=\s*(.+?)\s*", line)
            if sent_match:
                flush(line_no - 1)
                sent_id = sent_match.group(1)
                if sent_id in seen_sent_ids:
                    fail(
                        ErrorCode.DUP_SENT,
                        "duplicate sent_id in dependency arc source",
                        field="sent_id",
                        line=line_no,
                        sent_id=sent_id,
                    )
                current_sent_id = sent_id
                sentence_start_line = line_no
                continue
            root_match = re.fullmatch(r"#\s*root\s*=\s*(.+?)\s*", line)
            if root_match:
                if version is not FormatVersion.V2:
                    _format_corrupt(
                        "v1 does not support an explicit root metadata field",
                        line=line_no,
                        field="root",
                    )
                if current_sent_id is None:
                    _format_corrupt(
                        "root metadata appears before sent_id",
                        line=line_no,
                        field="root",
                    )
                if seen_root_metadata:
                    _format_corrupt(
                        "v2 sentence declares more than one root marker",
                        line=line_no,
                        field="root",
                        sent_id=current_sent_id,
                    )
                current_root = _parse_int(
                    root_match.group(1),
                    line=line_no,
                    field="root",
                    sent_id=current_sent_id,
                )
                seen_root_metadata = True
                continue
            continue
        if current_sent_id is None:
            _format_corrupt(
                "dependency arc row appears before sent_id metadata",
                line=line_no,
                field="sent_id",
            )
        if version is FormatVersion.V1:
            parts = line.split("|")
            if len(parts) != 3:
                _format_corrupt(
                    "v1 row must contain exactly three pipe-separated fields",
                    line=line_no,
                    field="fields",
                    sent_id=current_sent_id,
                    expected=3,
                    actual=len(parts),
                )
            token_id = _parse_int(
                parts[0],
                line=line_no,
                field="token_id",
                sent_id=current_sent_id,
            )
            head = _parse_int(
                parts[1],
                line=line_no,
                field="head",
                sent_id=current_sent_id,
            )
            deprel = parts[2].strip()
            if not deprel:
                _format_corrupt(
                    "v1 deprel field is empty",
                    line=line_no,
                    field="deprel",
                    sent_id=current_sent_id,
                    token_id=token_id,
                )
            arc = Arc(token_id=token_id, head=head, deprel=deprel)
        else:
            parts = line.split("|")
            fields: dict[str, str] = {}
            for part in parts:
                if "=" not in part:
                    _format_corrupt(
                        "v2 row contains a field without '='",
                        line=line_no,
                        field="fields",
                        sent_id=current_sent_id,
                        value=part,
                    )
                key, value = part.split("=", 1)
                key = key.strip()
                if not key:
                    _format_corrupt(
                        "v2 row contains an empty field name",
                        line=line_no,
                        field="fields",
                        sent_id=current_sent_id,
                    )
                if key in fields:
                    _format_corrupt(
                        "v2 row contains a duplicate field",
                        line=line_no,
                        field=key,
                        sent_id=current_sent_id,
                    )
                fields[key] = value.strip()
            missing = [field for field in V2_FIELDS if field not in fields]
            if missing:
                _format_corrupt(
                    "v2 row is missing required fields",
                    line=line_no,
                    field=missing[0],
                    sent_id=current_sent_id,
                    missing_fields=missing,
                )
            token_id = _parse_int(
                fields["token_id"],
                line=line_no,
                field="token_id",
                sent_id=current_sent_id,
            )
            head = _parse_int(
                fields["head"],
                line=line_no,
                field="head",
                sent_id=current_sent_id,
            )
            deprel = fields["deprel"]
            if not deprel:
                _format_corrupt(
                    "v2 deprel field is empty",
                    line=line_no,
                    field="deprel",
                    sent_id=current_sent_id,
                    token_id=token_id,
                )
            low = _parse_float(
                fields["confidence_low"],
                line=line_no,
                field="confidence_low",
                sent_id=current_sent_id,
            )
            high = _parse_float(
                fields["confidence_high"],
                line=line_no,
                field="confidence_high",
                sent_id=current_sent_id,
            )
            _validate_confidence(
                low,
                high,
                line=line_no,
                sent_id=current_sent_id,
                field_prefix="arc",
            )
            arc = Arc(
                token_id=token_id,
                head=head,
                deprel=deprel,
                confidence_low=low,
                confidence_high=high,
                field_sources={
                    "confidence_low": "observed:v2",
                    "confidence_high": "observed:v2",
                },
            )
        if arc.token_id in current_arcs:
            fail(
                ErrorCode.TOKEN_SEQ,
                "duplicate token_id in sentence",
                field="token_id",
                line=line_no,
                sent_id=current_sent_id,
                token_id=arc.token_id,
            )
        current_arcs[arc.token_id] = arc
    flush(len(text.splitlines()))
    if not graph.sentences:
        _format_corrupt(
            "dependency arc source contains no sentences",
            line=1,
            field="sent_id",
        )
    return graph


def _parse_v3(text: str, *, source_name: str) -> ArcGraph:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        _format_corrupt(
            "v3 dependency arc source is not valid JSON",
            line=exc.lineno,
            field="root",
            column=exc.colno,
        )
    if not isinstance(data, dict):
        _format_corrupt(
            "v3 root must be a JSON object",
            line=1,
            field="root",
        )
    if data.get("format") != "deparc":
        _format_corrupt(
            "v3 source must declare format='deparc'",
            line=_line_for(text, '"format"'),
            field="format",
            actual=data.get("format"),
        )
    raw_version = data.get("version")
    if not isinstance(raw_version, str) or not raw_version:
        _format_corrupt(
            "v3 source must declare a non-empty version",
            line=_line_for(text, '"version"'),
            field="version",
        )
    if raw_version.lower() not in ("v3", "3"):
        _unsupported(raw_version, line=_line_for(text, '"version"'))
    sentences = data.get("sentences")
    if not isinstance(sentences, list) or not sentences:
        _format_corrupt(
            "v3 sentences must be a non-empty list",
            line=_line_for(text, '"sentences"'),
            field="sentences",
        )
    graph = ArcGraph(format_version=FormatVersion.V3, source_name=source_name)
    for sent_index, raw_sentence in enumerate(sentences):
        sentence_line = _line_for(text, '"sent_id"')
        sent_obj = _require_object(
            raw_sentence,
            sent_id=None,
            field=f"sentences[{sent_index}]",
            line=sentence_line,
        )
        sent_id = _json_str(
            _required(
                sent_obj,
                "sent_id",
                sent_id=None,
                field_prefix=f"sentences[{sent_index}]",
                line=sentence_line,
            ),
            sent_id=f"<sentences[{sent_index}]>",
            field="sent_id",
            line=sentence_line,
        )
        if sent_id in graph.sentences:
            fail(
                ErrorCode.DUP_SENT,
                "duplicate sent_id in v3 source",
                field="sent_id",
                line=sentence_line,
                sent_id=sent_id,
            )
        root_marker = _json_int(
            _required(
                sent_obj,
                "root",
                sent_id=sent_id,
                field_prefix=f"sentences[{sent_index}]",
                line=sentence_line,
            ),
            sent_id=sent_id,
            field="root",
            line=sentence_line,
        )
        raw_tokens = _required(
            sent_obj,
            "tokens",
            sent_id=sent_id,
            field_prefix=f"sentences[{sent_index}]",
            line=sentence_line,
        )
        if not isinstance(raw_tokens, list) or not raw_tokens:
            _format_corrupt(
                "v3 sentence tokens must be a non-empty list",
                line=sentence_line,
                field=f"sentences[{sent_index}].tokens",
                sent_id=sent_id,
            )
        arcs: dict[int, Arc] = {}
        token_line = _line_for(text, '"tokens"')
        for token_index, raw_token in enumerate(raw_tokens):
            prefix = f"sentences[{sent_index}].tokens[{token_index}]"
            token_obj = _require_object(
                raw_token,
                sent_id=sent_id,
                field=prefix,
                line=token_line,
            )
            token_id = _json_int(
                _required(
                    token_obj,
                    "token_id",
                    sent_id=sent_id,
                    field_prefix=prefix,
                    line=token_line,
                ),
                sent_id=sent_id,
                field=f"{prefix}.token_id",
                line=token_line,
            )
            head = _json_int(
                _required(
                    token_obj,
                    "head",
                    sent_id=sent_id,
                    field_prefix=prefix,
                    line=token_line,
                ),
                sent_id=sent_id,
                field=f"{prefix}.head",
                line=token_line,
            )
            deprel = _json_str(
                _required(
                    token_obj,
                    "deprel",
                    sent_id=sent_id,
                    field_prefix=prefix,
                    line=token_line,
                ),
                sent_id=sent_id,
                field=f"{prefix}.deprel",
                line=token_line,
            )
            confidence = _require_object(
                _required(
                    token_obj,
                    "confidence",
                    sent_id=sent_id,
                    field_prefix=prefix,
                    line=token_line,
                ),
                sent_id=sent_id,
                field=f"{prefix}.confidence",
                line=token_line,
            )
            low = _json_float(
                _required(
                    confidence,
                    "low",
                    sent_id=sent_id,
                    field_prefix=f"{prefix}.confidence",
                    line=token_line,
                ),
                sent_id=sent_id,
                field=f"{prefix}.confidence.low",
                line=token_line,
            )
            high = _json_float(
                _required(
                    confidence,
                    "high",
                    sent_id=sent_id,
                    field_prefix=f"{prefix}.confidence",
                    line=token_line,
                ),
                sent_id=sent_id,
                field=f"{prefix}.confidence.high",
                line=token_line,
            )
            _validate_confidence(
                low,
                high,
                line=token_line,
                sent_id=sent_id,
                field_prefix=prefix,
            )
            checks = _require_object(
                _required(
                    token_obj,
                    "dependency_checks",
                    sent_id=sent_id,
                    field_prefix=prefix,
                    line=token_line,
                ),
                sent_id=sent_id,
                field=f"{prefix}.dependency_checks",
                line=token_line,
            )
            forward = _json_bool(
                _required(
                    checks,
                    "forward",
                    sent_id=sent_id,
                    field_prefix=f"{prefix}.dependency_checks",
                    line=token_line,
                ),
                sent_id=sent_id,
                field=f"{prefix}.dependency_checks.forward",
                line=token_line,
            )
            reverse = _json_bool(
                _required(
                    checks,
                    "reverse",
                    sent_id=sent_id,
                    field_prefix=f"{prefix}.dependency_checks",
                    line=token_line,
                ),
                sent_id=sent_id,
                field=f"{prefix}.dependency_checks.reverse",
                line=token_line,
            )
            if token_id in arcs:
                fail(
                    ErrorCode.TOKEN_SEQ,
                    "duplicate token_id in v3 sentence",
                    field="token_id",
                    sent_id=sent_id,
                    token_id=token_id,
                    line=token_line,
                )
            arcs[token_id] = Arc(
                token_id=token_id,
                head=head,
                deprel=deprel,
                confidence_low=low,
                confidence_high=high,
                forward_check=forward,
                reverse_check=reverse,
                field_sources={
                    "confidence_low": "observed:v3",
                    "confidence_high": "observed:v3",
                    "forward_check": "observed:v3",
                    "reverse_check": "observed:v3",
                },
            )
        chains = _parse_v3_chains(
            sent_obj,
            sent_id=sent_id,
            token_ids=set(arcs),
            text=text,
            sent_index=sent_index,
        )
        refs = _parse_v3_refs(
            sent_obj,
            sent_id=sent_id,
            token_ids=set(arcs),
            text=text,
            sent_index=sent_index,
        )
        graph.add(
            SentenceGraph(
                sent_id=sent_id,
                arcs=arcs,
                root_marker=root_marker,
                root_source="observed",
                bidirectional_chains=chains,
                cross_sentence_refs=refs,
            )
        )
    for sent_id, sentence in graph.sentences.items():
        for ref in sentence.cross_sentence_refs:
            target = graph.sentences.get(ref.target_sent_id)
            if target is None:
                _format_corrupt(
                    "cross-sentence reference targets an unknown sent_id",
                    line=_line_for(text, f'"{sent_id}"'),
                    field="cross_sentence_refs.target_sent_id",
                    sent_id=sent_id,
                    token_id=ref.source_token_id,
                    target_sent_id=ref.target_sent_id,
                )
            if ref.target_token_id not in target.arcs:
                _format_corrupt(
                    "cross-sentence reference targets an unknown token_id",
                    line=_line_for(text, f'"{sent_id}"'),
                    field="cross_sentence_refs.target_token_id",
                    sent_id=sent_id,
                    token_id=ref.source_token_id,
                    target_sent_id=ref.target_sent_id,
                    target_token_id=ref.target_token_id,
                )
    return graph


def _parse_v3_chains(
    sent_obj: dict[str, Any],
    *,
    sent_id: str,
    token_ids: set[int],
    text: str,
    sent_index: int,
) -> list[BidirectionalChain]:
    field_name = "bidirectional_arc_chains"
    raw_chains = _required(
        sent_obj,
        field_name,
        sent_id=sent_id,
        field_prefix=f"sentences[{sent_index}]",
        line=_line_for(text, f'"{field_name}"'),
    )
    if not isinstance(raw_chains, list):
        _format_corrupt(
            "v3 bidirectional_arc_chains must be a list",
            line=_line_for(text, f'"{field_name}"'),
            field=field_name,
            sent_id=sent_id,
        )
    chains: list[BidirectionalChain] = []
    seen_chain_ids: set[str] = set()
    line = _line_for(text, f'"{field_name}"')
    for index, raw_chain in enumerate(raw_chains):
        prefix = f"sentences[{sent_index}].{field_name}[{index}]"
        obj = _require_object(
            raw_chain,
            sent_id=sent_id,
            field=prefix,
            line=line,
        )
        chain_id = _json_str(
            _required(
                obj,
                "chain_id",
                sent_id=sent_id,
                field_prefix=prefix,
                line=line,
            ),
            sent_id=sent_id,
            field=f"{prefix}.chain_id",
            line=line,
        )
        if chain_id in seen_chain_ids:
            _format_corrupt(
                "duplicate chain_id in sentence",
                line=line,
                field=f"{prefix}.chain_id",
                sent_id=sent_id,
                value=chain_id,
            )
        seen_chain_ids.add(chain_id)
        raw_token_ids = _required(
            obj,
            "token_ids",
            sent_id=sent_id,
            field_prefix=prefix,
            line=line,
        )
        if not isinstance(raw_token_ids, list) or not raw_token_ids:
            _format_corrupt(
                "chain token_ids must be a non-empty list",
                line=line,
                field=f"{prefix}.token_ids",
                sent_id=sent_id,
            )
        members: list[int] = []
        for item_index, raw_token_id in enumerate(raw_token_ids):
            token_id = _json_int(
                raw_token_id,
                sent_id=sent_id,
                field=f"{prefix}.token_ids[{item_index}]",
                line=line,
            )
            if token_id not in token_ids:
                _format_corrupt(
                    "bidirectional chain references an unknown token_id",
                    line=line,
                    field=f"{prefix}.token_ids[{item_index}]",
                    sent_id=sent_id,
                    token_id=token_id,
                )
            members.append(token_id)
        forward = _json_bool(
            _required(
                obj,
                "forward",
                sent_id=sent_id,
                field_prefix=prefix,
                line=line,
            ),
            sent_id=sent_id,
            field=f"{prefix}.forward",
            line=line,
        )
        reverse = _json_bool(
            _required(
                obj,
                "reverse",
                sent_id=sent_id,
                field_prefix=prefix,
                line=line,
            ),
            sent_id=sent_id,
            field=f"{prefix}.reverse",
            line=line,
        )
        chains.append(
            BidirectionalChain(
                chain_id=chain_id,
                token_ids=tuple(members),
                forward=forward,
                reverse=reverse,
            )
        )
    return chains


def _parse_v3_refs(
    sent_obj: dict[str, Any],
    *,
    sent_id: str,
    token_ids: set[int],
    text: str,
    sent_index: int,
) -> list[CrossSentenceRef]:
    field_name = "cross_sentence_refs"
    raw_refs = _required(
        sent_obj,
        field_name,
        sent_id=sent_id,
        field_prefix=f"sentences[{sent_index}]",
        line=_line_for(text, f'"{field_name}"'),
    )
    if not isinstance(raw_refs, list):
        _format_corrupt(
            "v3 cross_sentence_refs must be a list",
            line=_line_for(text, f'"{field_name}"'),
            field=field_name,
            sent_id=sent_id,
        )
    refs: list[CrossSentenceRef] = []
    line = _line_for(text, f'"{field_name}"')
    for index, raw_ref in enumerate(raw_refs):
        prefix = f"sentences[{sent_index}].{field_name}[{index}]"
        obj = _require_object(
            raw_ref,
            sent_id=sent_id,
            field=prefix,
            line=line,
        )
        source_token_id = _json_int(
            _required(
                obj,
                "source_token_id",
                sent_id=sent_id,
                field_prefix=prefix,
                line=line,
            ),
            sent_id=sent_id,
            field=f"{prefix}.source_token_id",
            line=line,
        )
        if source_token_id not in token_ids:
            _format_corrupt(
                "cross-sentence reference source token does not exist",
                line=line,
                field=f"{prefix}.source_token_id",
                sent_id=sent_id,
                token_id=source_token_id,
            )
        target_sent_id = _json_str(
            _required(
                obj,
                "target_sent_id",
                sent_id=sent_id,
                field_prefix=prefix,
                line=line,
            ),
            sent_id=sent_id,
            field=f"{prefix}.target_sent_id",
            line=line,
        )
        target_token_id = _json_int(
            _required(
                obj,
                "target_token_id",
                sent_id=sent_id,
                field_prefix=prefix,
                line=line,
            ),
            sent_id=sent_id,
            field=f"{prefix}.target_token_id",
            line=line,
        )
        relation = _json_str(
            _required(
                obj,
                "relation",
                sent_id=sent_id,
                field_prefix=prefix,
                line=line,
            ),
            sent_id=sent_id,
            field=f"{prefix}.relation",
            line=line,
        )
        confidence: float | None = None
        if "confidence" in obj:
            confidence = _json_float(
                obj["confidence"],
                sent_id=sent_id,
                field=f"{prefix}.confidence",
                line=line,
            )
            if not (0.0 <= confidence <= 1.0):
                _format_corrupt(
                    "cross-sentence confidence must be within [0, 1]",
                    line=line,
                    field=f"{prefix}.confidence",
                    sent_id=sent_id,
                    value=confidence,
                )
        refs.append(
            CrossSentenceRef(
                source_token_id=source_token_id,
                target_sent_id=target_sent_id,
                target_token_id=target_token_id,
                relation=relation,
                confidence=confidence,
            )
        )
    return refs


def _require_confidence(arc: Arc, sent_id: str) -> tuple[float, float]:
    if arc.confidence_low is None or arc.confidence_high is None:
        fail(
            ErrorCode.INCONSISTENT,
            "arc is missing confidence values required by the target format",
            category="MISSING_CONFIDENCE",
            sent_id=sent_id,
            token_id=arc.token_id,
            format_version="v2/v3",
            field="confidence",
        )
    return arc.confidence_low, arc.confidence_high


def _require_checks(arc: Arc, sent_id: str) -> tuple[bool, bool]:
    if arc.forward_check is None or arc.reverse_check is None:
        fail(
            ErrorCode.INCONSISTENT,
            "arc is missing dependency check flags required by v3",
            category="MISSING_DEPENDENCY_CHECK",
            sent_id=sent_id,
            token_id=arc.token_id,
            format_version="v3",
            field="dependency_checks",
        )
    return arc.forward_check, arc.reverse_check


def _require_root(sentence: SentenceGraph, format_version: str) -> int:
    root = sentence.effective_root()
    if root is None:
        fail(
            ErrorCode.INCONSISTENT,
            "sentence has no unambiguous root marker for serialization",
            category="MISSING_ROOT_MARKER",
            sent_id=sentence.sent_id,
            format_version=format_version,
            field="root",
        )
    return root


def serialize_graph(
    graph: ArcGraph,
    *,
    version: str | FormatVersion | None = None,
) -> str:
    target = FormatVersion.parse(version or graph.format_version or "v3")
    if target is FormatVersion.V1:
        return _serialize_v1(graph)
    if target is FormatVersion.V2:
        return _serialize_v2(graph)
    return _serialize_v3(graph)


def _serialize_v1(graph: ArcGraph) -> str:
    lines = ["# format=v1"]
    for sent_id, sentence in graph.sentences.items():
        lines.append(f"# sent_id={sent_id}")
        for arc in sentence.ordered_arcs():
            lines.append(f"{arc.token_id}|{arc.head}|{arc.deprel}")
    return "\n".join(lines) + "\n"


def _serialize_v2(graph: ArcGraph) -> str:
    lines = ["# format=v2"]
    for sent_id, sentence in graph.sentences.items():
        root = _require_root(sentence, "v2")
        lines.append(f"# sent_id={sent_id}")
        lines.append(f"# root={root}")
        for arc in sentence.ordered_arcs():
            low, high = _require_confidence(arc, sent_id)
            lines.append(
                "|".join(
                    (
                        f"token_id={arc.token_id}",
                        f"head={arc.head}",
                        f"deprel={arc.deprel}",
                        f"confidence_low={low:.6f}",
                        f"confidence_high={high:.6f}",
                    )
                )
            )
    return "\n".join(lines) + "\n"


def _serialize_v3(graph: ArcGraph) -> str:
    sentences: list[dict[str, Any]] = []
    for sent_id, sentence in graph.sentences.items():
        root = _require_root(sentence, "v3")
        tokens: list[dict[str, Any]] = []
        for arc in sentence.ordered_arcs():
            low, high = _require_confidence(arc, sent_id)
            forward, reverse = _require_checks(arc, sent_id)
            tokens.append(
                {
                    "token_id": arc.token_id,
                    "head": arc.head,
                    "deprel": arc.deprel,
                    "confidence": {"low": low, "high": high},
                    "dependency_checks": {
                        "forward": forward,
                        "reverse": reverse,
                    },
                }
            )
        sentences.append(
            {
                "sent_id": sent_id,
                "root": root,
                "tokens": tokens,
                "bidirectional_arc_chains": [
                    chain.to_dict() for chain in sentence.bidirectional_chains
                ],
                "cross_sentence_refs": [
                    ref.to_dict() for ref in sentence.cross_sentence_refs
                ],
            }
        )
    payload = {
        "format": "deparc",
        "version": "v3",
        "sentences": sentences,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
