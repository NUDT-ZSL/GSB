"""Deterministic structured mutations of parsed request bytes."""

from __future__ import annotations

import re
from typing import Any

from .models import MutationSpec, ParsedMessage
from .util import BusinessError
from .codes import E_INPUT


def apply_mutation(message: ParsedMessage, spec: MutationSpec) -> bytes:
    operation = spec.operation
    params = spec.parameters
    target = spec.target
    if operation in {"", "noop", "none", "identity"}:
        return message.raw
    if operation in {"raw", "replace_raw", "set_raw"}:
        return _bytes_parameter(params, ("hex", "raw_hex", "value_hex"))
    if operation in {"replace", "replace_bytes"}:
        offset = int(params.get("offset", 0))
        length = int(params.get("length", 0))
        replacement = _bytes_parameter(params, ("hex", "replacement_hex", "value_hex"))
        if offset < 0 or length < 0 or offset + length > len(message.raw):
            raise BusinessError(
                E_INPUT,
                "mutation replacement is outside the request",
                {"mutation_id": spec.mutation_id, "offset": offset, "length": length},
            )
        return message.raw[:offset] + replacement + message.raw[offset + length :]
    if operation in {"duplicate_header", "duplicate", "same_name_header"}:
        return _duplicate_header(message.raw, params)
    if operation in {"set_header", "replace_header"}:
        return _set_header(message.raw, params, append_if_missing=True)
    if operation in {"remove_header", "delete_header"}:
        return _remove_header(message.raw, params)
    if operation in {"content_length", "set_content_length"}:
        normalized = dict(params)
        normalized.setdefault("name", "Content-Length")
        return _set_header(message.raw, normalized, append_if_missing=True)
    if operation in {"transfer_encoding", "set_transfer_encoding", "te_cl_conflict"}:
        normalized = dict(params)
        normalized.setdefault("name", "Transfer-Encoding")
        normalized.setdefault("value", "chunked")
        return _set_header(message.raw, normalized, append_if_missing=True)
    if operation in {"chunk_length", "chunk_size", "distort_chunk_length"}:
        return _mutate_chunk_length(message.raw, params)
    if operation in {"append_trailer", "trailer", "trailer_injection"}:
        return _append_trailer(message.raw, params)
    if operation in {"request_boundary", "boundary", "boundary_injection"}:
        return _mutate_boundary(message.raw, params)
    if operation in {"append", "append_bytes"}:
        return message.raw + _bytes_parameter(params, ("hex", "value_hex", "append_hex"))
    if operation in {"prepend", "prepend_bytes"}:
        return _bytes_parameter(params, ("hex", "value_hex", "prepend_hex")) + message.raw

    target_defaults = {
        "content_length": "content_length",
        "transfer_encoding": "transfer_encoding",
        "same_name_header": "duplicate_header",
        "duplicate_header": "duplicate_header",
        "chunk_length": "chunk_length",
        "trailer": "append_trailer",
        "tails": "append_trailer",
        "request_boundary": "request_boundary",
        "boundary": "request_boundary",
    }
    if target in target_defaults and operation in {"", target}:
        redirected = MutationSpec(
            mutation_id=spec.mutation_id,
            connection_id=spec.connection_id,
            request_index=spec.request_index,
            target=spec.target,
            operation=target_defaults[target],
            parameters=spec.parameters,
            delay_ms=spec.delay_ms,
            source_file=spec.source_file,
            record_index=spec.record_index,
            seed=spec.seed,
        )
        return apply_mutation(message, redirected)
    raise BusinessError(
        E_INPUT,
        "unsupported mutation operation",
        {
            "mutation_id": spec.mutation_id,
            "operation": operation,
            "target": target,
        },
    )


def _bytes_parameter(params: dict[str, Any], names: tuple[str, ...]) -> bytes:
    for name in names:
        if name in params:
            value = params[name]
            if isinstance(value, bytes):
                return value
            if not isinstance(value, str):
                raise ValueError(f"{name} must be hexadecimal text")
            compact = "".join(value.split())
            if len(compact) % 2:
                raise ValueError(f"{name} must have even length")
            return bytes.fromhex(compact)
    raise ValueError("mutation needs a hexadecimal byte parameter")


def _head_and_body(raw: bytes) -> tuple[bytes, bytes, bytes]:
    index = raw.find(b"\r\n\r\n")
    separator = b"\r\n\r\n"
    if index < 0:
        index = raw.find(b"\n\n")
        separator = b"\n\n"
    if index < 0:
        return raw, separator, b""
    return raw[:index], separator, raw[index + len(separator) :]


def _newline(raw: bytes) -> bytes:
    return b"\r\n" if b"\r\n" in raw else b"\n"


def _header_lines(head: bytes) -> list[bytes]:
    newline = _newline(head)
    return head.split(newline)


def _header_name(line: bytes) -> str | None:
    if b":" not in line:
        return None
    try:
        return line.split(b":", 1)[0].decode("ascii").strip().casefold()
    except UnicodeDecodeError:
        return None


def _duplicate_header(raw: bytes, params: dict[str, Any]) -> bytes:
    name = str(params.get("name", params.get("header", "Content-Length")))
    head, separator, body = _head_and_body(raw)
    newline = _newline(raw)
    lines = _header_lines(head)
    wanted = name.casefold()
    occurrence = int(params.get("occurrence", params.get("index", 0)))
    matches = [index for index, line in enumerate(lines[1:], start=1) if _header_name(line) == wanted]
    if matches:
        source_index = matches[min(max(occurrence, 0), len(matches) - 1)]
        source = lines[source_index]
        if "value" in params:
            original_name = source.split(b":", 1)[0]
            source = original_name + b": " + str(params["value"]).encode("latin-1")
        lines.insert(source_index + 1, source)
    else:
        value = str(params.get("value", ""))
        lines.append(name.encode("ascii") + b": " + value.encode("latin-1"))
    return newline.join(lines) + separator + body


def _set_header(raw: bytes, params: dict[str, Any], *, append_if_missing: bool) -> bytes:
    name = str(params.get("name", params.get("header", "")))
    if not name:
        raise ValueError("header name is required")
    value = params.get("value")
    if value is None:
        value = params.get("new_value", "")
    value_text = str(value)
    head, separator, body = _head_and_body(raw)
    newline = _newline(raw)
    lines = _header_lines(head)
    wanted = name.casefold()
    replaced = False
    for index in range(1, len(lines)):
        if _header_name(lines[index]) == wanted:
            original_name = lines[index].split(b":", 1)[0]
            lines[index] = original_name + b": " + value_text.encode("latin-1")
            replaced = True
            break
    if not replaced and append_if_missing:
        lines.append(name.encode("ascii") + b": " + value_text.encode("latin-1"))
    return newline.join(lines) + separator + body


def _remove_header(raw: bytes, params: dict[str, Any]) -> bytes:
    name = str(params.get("name", params.get("header", "")))
    if not name:
        raise ValueError("header name is required")
    head, separator, body = _head_and_body(raw)
    newline = _newline(raw)
    wanted = name.casefold()
    lines = [lines[0]] if head else []
    for line in _header_lines(head)[1:]:
        if _header_name(line) != wanted:
            lines.append(line)
    return newline.join(lines) + separator + body


def _mutate_chunk_length(raw: bytes, params: dict[str, Any]) -> bytes:
    head, separator, body = _head_and_body(raw)
    newline = _newline(raw)
    header_text = head.lower()
    if b"transfer-encoding" not in header_text or b"chunked" not in header_text:
        raw_body = body
        head = _set_header(
            head + separator,
            {"name": "Transfer-Encoding", "value": "chunked"},
            append_if_missing=True,
        ).rsplit(separator, 1)[0]
        body = (
            f"{len(raw_body):x}".encode("ascii")
            + newline
            + raw_body
            + newline
            + b"0"
            + newline
            + newline
        )
    lines = body.split(newline)
    chunk_line_indexes: list[int] = []
    index = 0
    while index < len(lines):
        match = re.match(rb"^\s*([0-9A-Fa-f]+)(?:\s*;.*)?$", lines[index])
        if match is None:
            break
        size = int(match.group(1), 16)
        chunk_line_indexes.append(index)
        index += 1
        if size == 0:
            break
        if index >= len(lines):
            break
        index += 1
        if index < len(lines) and lines[index] == b"":
            index += 1
    if not chunk_line_indexes:
        raise ValueError("no chunk-size line found")
    occurrence = int(params.get("occurrence", params.get("index", 0)))
    chosen = chunk_line_indexes[min(max(occurrence, 0), len(chunk_line_indexes) - 1)]
    old_line = lines[chosen]
    size_part, semicolon, extension = old_line.partition(b";")
    old_size = int(size_part.strip(), 16)
    if "value" in params:
        value = params["value"]
        if isinstance(value, str) and value.strip().lower().startswith("0x"):
            replacement = value.strip()[2:]
        elif isinstance(value, int):
            replacement = f"{value:x}"
        else:
            replacement = str(value)
    elif "delta" in params:
        replacement = f"{max(0, old_size + int(params['delta'])):x}"
    elif "truncate_hex" in params and bool(params["truncate_hex"]):
        replacement = size_part.strip().decode("ascii")[:-1] or "x"
    elif "append" in params:
        replacement = size_part.strip().decode("ascii") + str(params["append"])
    else:
        replacement = f"{old_size + 1:x}"
    lines[chosen] = replacement.encode("ascii") + (
        semicolon + extension if semicolon else b""
    )
    return head + separator + newline.join(lines)


def _append_trailer(raw: bytes, params: dict[str, Any]) -> bytes:
    head, separator, body = _head_and_body(raw)
    newline = _newline(raw)
    header_text = head.lower()
    if b"transfer-encoding" not in header_text or b"chunked" not in header_text:
        head = _set_header(
            head + separator,
            {"name": "Transfer-Encoding", "value": "chunked"},
            append_if_missing=True,
        ).rsplit(separator, 1)[0]
        body = (
            f"{len(body):x}".encode("ascii")
            + newline
            + body
            + newline
            + b"0"
            + newline
        )
    trailer_name = str(
        params.get("name", params.get("header", "X-Smuggle-Trailer"))
    )
    trailer_value = str(params.get("value", "tail"))
    marker = b"0" + newline
    index = body.rfind(marker)
    if index < 0:
        raise ValueError("chunked body has no terminal zero chunk")
    after_zero = index + len(marker)
    trailer_line = trailer_name.encode("ascii") + b": " + trailer_value.encode("latin-1")
    body = body[:after_zero] + trailer_line + newline + body[after_zero:]
    return head + separator + body


def _mutate_boundary(raw: bytes, params: dict[str, Any]) -> bytes:
    newline = _newline(raw)
    action = str(params.get("action", "append")).casefold()
    if "append_hex" in params or "append" in params:
        suffix = _parameter_bytes(params, "append_hex", "append")
    else:
        suffix = b""
    if "prepend_hex" in params or "prepend" in params:
        prefix = _parameter_bytes(params, "prepend_hex", "prepend")
    else:
        prefix = b""
    if "raw_hex" in params:
        return _bytes_parameter(params, ("raw_hex",))
    if action in {"truncate", "cut"}:
        point = int(params.get("at", params.get("length", len(raw))))
        point = max(0, min(point, len(raw)))
        return raw[:point]
    if action in {"inject", "inject_after"}:
        point = int(params.get("at", len(raw)))
        point = max(0, min(point, len(raw)))
        injected = _bytes_parameter(
            params,
            ("inject_hex", "request_hex", "append_hex", "hex"),
        )
        return raw[:point] + injected + raw[point:]
    if action == "append_request":
        request_hex = str(
            params.get(
                "request_hex",
                (
                    "GET /smuggled HTTP/1.1"
                    + newline.decode("ascii")
                    + "Host: local"
                    + newline.decode("ascii")
                    + newline.decode("ascii")
                ).encode("ascii").hex(),
            )
        )
        return raw + prefix + suffix + bytes.fromhex(request_hex)
    return prefix + raw + suffix


def _parameter_bytes(params: dict[str, Any], hex_name: str, text_name: str) -> bytes:
    if hex_name in params:
        return _bytes_parameter(params, (hex_name,))
    if text_name in params:
        value = params[text_name]
        if isinstance(value, bytes):
            return value
        return str(value).encode("latin-1")
    return b""
