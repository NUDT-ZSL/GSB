"""Byte-driven connection reassembly and HTTP/1.x state machines."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from .codes import E_MIX_CONN, E_MSG_GAP
from .models import (
    ConnectionResult,
    FlowRecord,
    ParsedMessage,
    StateTransition,
)
from .rules import Evaluation, RuleEngine

MAX_HEADER_BYTES = 64 * 1024
MAX_CHUNK_LINE = 4096


@dataclass
class StreamSlice:
    seq: int
    start: int
    end: int
    source_file: str
    record_index: int


@dataclass
class ReassembledStream:
    connection_id: str
    direction: str
    data: bytes
    slices: list[StreamSlice]
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def seq_range(self, start: int, end: int) -> tuple[int, int]:
        matching = [
            item
            for item in self.slices
            if item.end > start and item.start < max(end, start + 1)
        ]
        if not matching:
            return 0, 0
        return min(item.seq for item in matching), max(item.seq for item in matching)


def reassemble_connection(
    records: Iterable[FlowRecord],
    *,
    expected_connection_id: str | None = None,
) -> dict[tuple[str, str], ReassembledStream]:
    by_stream: dict[tuple[str, str], list[FlowRecord]] = defaultdict(list)
    all_records = list(records)
    connection_ids = {record.connection_id for record in all_records}
    if expected_connection_id is None and len(connection_ids) > 1:
        mixed = sorted(connection_ids)
    else:
        mixed = []
    for record in all_records:
        by_stream[(record.connection_id, record.direction)].append(record)

    result: dict[tuple[str, str], ReassembledStream] = {}
    for (connection_id, direction), items in sorted(by_stream.items()):
        stream = ReassembledStream(
            connection_id=connection_id,
            direction=direction,
            data=b"",
            slices=[],
        )
        if mixed:
            stream.errors.append(
                {
                    "code": E_MIX_CONN,
                    "message": "records from multiple connection identifiers were mixed",
                    "expected_connection_id": expected_connection_id,
                    "connection_ids": mixed,
                }
            )
        if expected_connection_id is not None and connection_id != expected_connection_id:
            stream.errors.append(
                {
                    "code": E_MIX_CONN,
                    "message": "record does not belong to the expected connection",
                    "expected_connection_id": expected_connection_id,
                    "actual_connection_id": connection_id,
                }
            )
        for record in items:
            if record.connection_hint and record.connection_hint != connection_id:
                stream.errors.append(
                    {
                        "code": E_MIX_CONN,
                        "message": "payload connection hint differs from envelope",
                        "expected_connection_id": connection_id,
                        "payload_connection_id": record.connection_hint,
                        **record.location(),
                    }
                )

        first_by_seq: dict[int, FlowRecord] = {}
        duplicate_groups: dict[int, list[FlowRecord]] = defaultdict(list)
        for record in sorted(items, key=lambda item: item.arrival_index):
            if record.seq not in first_by_seq:
                first_by_seq[record.seq] = record
            else:
                duplicate_groups[record.seq].append(record)
        for seq in sorted(duplicate_groups):
            original = first_by_seq[seq]
            duplicates = duplicate_groups[seq]
            stream.duplicates.append(
                {
                    "seq": seq,
                    "duplicate_count": len(duplicates),
                    "first": original.location(),
                    "duplicates": [item.location() for item in duplicates],
                    "payload_conflict": any(
                        item.payload != original.payload for item in duplicates
                    ),
                    "truth": "FIRST_OCCURRENCE",
                }
            )

        unique = [first_by_seq[seq] for seq in sorted(first_by_seq)]
        if unique:
            minimum = unique[0].seq
            maximum = unique[-1].seq
            present = set(first_by_seq)
            missing = [seq for seq in range(minimum, maximum + 1) if seq not in present]
            if missing:
                stream.errors.append(
                    {
                        "code": E_MSG_GAP,
                        "message": "missing message sequence numbers",
                        "missing_ranges": _ranges(missing),
                        "missing": missing,
                    }
                )

        chunks: list[bytes] = []
        offset = 0
        for record in unique:
            chunks.append(record.payload)
            stream.slices.append(
                StreamSlice(
                    seq=record.seq,
                    start=offset,
                    end=offset + len(record.payload),
                    source_file=record.source_file,
                    record_index=record.record_index,
                )
            )
            offset += len(record.payload)
        stream.data = b"".join(chunks)
        result[(connection_id, direction)] = stream
    return result


def _ranges(values: list[int]) -> list[list[int]]:
    if not values:
        return []
    ranges: list[list[int]] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append([start, previous])
        start = previous = value
    ranges.append([start, previous])
    return ranges


class HTTPStreamParser:
    """A small deterministic HTTP/1.x request parser whose state changes are data-driven."""

    def __init__(
        self,
        stream: ReassembledStream,
        *,
        rule_engine: RuleEngine | None = None,
    ):
        self.stream = stream
        self.data = stream.data
        self.rule_engine = rule_engine
        self.offset = 0
        self.request_index = 0
        self.connection_transitions: list[StateTransition] = []
        self.rule_hits: list[Any] = []
        self.suppressed_hits: list[dict[str, Any]] = []
        self.candidate_rules: set[str] = set()
        self.anomalies: set[str] = set()

    def parse(self) -> tuple[list[ParsedMessage], list[StateTransition]]:
        messages: list[ParsedMessage] = []
        if self.stream.errors:
            return messages, self.connection_transitions
        while self.offset < len(self.data):
            if self.data[self.offset :].strip(b"\r\n \t") == b"":
                break
            request_start = self.offset
            message = self._parse_one()
            if message is None:
                break
            messages.append(message)
            self.request_index += 1
            if not message.complete or message.end_offset <= request_start:
                self.offset = max(self.offset, message.end_offset)
                break
        return messages, self.connection_transitions

    def _parse_one(self) -> ParsedMessage | None:
        start = self.offset
        context: dict[str, Any] = {
            "header_names": set(),
            "header_values": {},
            "duplicate_header": False,
            "framing": set(),
            "anomalies": set(),
            "transfer_encoding": set(),
            "trailer_present": False,
            "chunk_zero": False,
            "chunk_extension": False,
        }
        message_anomalies: set[str] = set()
        trace_start = len(self.connection_transitions)
        line, line_end, complete = self._read_line(self.offset, MAX_HEADER_BYTES)
        if not complete or line is None:
            return None
        start_line = line.decode("latin-1", errors="replace")
        start_line_end = line_end
        self._emit(
            "START_LINE",
            "START_LINE_DONE",
            start_line_end,
            "HEADERS",
            context,
        )
        pieces = start_line.strip().split()
        method = pieces[0] if len(pieces) >= 1 else ""
        target = pieces[1] if len(pieces) >= 2 else ""
        version = pieces[2] if len(pieces) >= 3 else ""
        if len(pieces) != 3 or not version.startswith("HTTP/"):
            self.anomalies.add("malformed_start_line")
            message_anomalies.add("malformed_start_line")
            context["anomalies"].add("malformed_start_line")
        context["method"] = method
        context["request_target"] = target

        self.offset = line_end
        headers: list[tuple[str, str]] = []
        header_end = self.offset
        headers_complete = True
        while True:
            header_line_start = self.offset
            header_line, header_line_end, header_complete = self._read_line(
                self.offset, MAX_HEADER_BYTES
            )
            if not header_complete or header_line is None:
                self.anomalies.add("incomplete_headers")
                message_anomalies.add("incomplete_headers")
                context["anomalies"].add("incomplete_headers")
                headers_complete = False
                header_end = len(self.data)
                self.offset = header_end
                break
            self.offset = header_line_end
            header_end = header_line_end
            if header_line == b"":
                break
            name, value = self._parse_header(header_line)
            if name is None:
                self.anomalies.add("malformed_header")
                message_anomalies.add("malformed_header")
                context["anomalies"].add("malformed_header")
                continue
            headers.append((name, value))
            context["header_names"].add(name.casefold())
            context["header_values"].setdefault(name.casefold(), []).append(value)
            context["duplicate_header"] = any(
                len(items) > 1 for items in context["header_values"].values()
            )
            self._emit(
                "HEADERS",
                "HEADER_FIELD",
                header_line_start,
                "HEADERS",
                context,
            )

        framing, content_length, transfer_encoding, chunks_meta = self._framing(headers)
        context["framing"].update(framing["framing_candidates"])
        context["transfer_encoding"].update(transfer_encoding)
        for anomaly in framing["anomalies"]:
            self.anomalies.add(anomaly)
            message_anomalies.add(anomaly)
            context["anomalies"].add(anomaly)
        self._emit("HEADERS", "HEADERS_DONE", header_end, "BODY", context)

        body_parts: list[bytes] = []
        chunk_sizes: list[int] = []
        chunks_meta: list[dict[str, Any]] = []
        trailers: list[tuple[str, str]] = []
        message_complete = headers_complete

        if not headers_complete:
            self.offset = len(self.data)
        elif framing["mode"] == "CHUNKED":
            while True:
                chunk_line_start = self.offset
                chunk_line, chunk_line_end, chunk_complete = self._read_line(
                    self.offset, MAX_CHUNK_LINE
                )
                if not chunk_complete or chunk_line is None:
                    self.anomalies.add("incomplete_chunk_size")
                    message_anomalies.add("incomplete_chunk_size")
                    context["anomalies"].add("incomplete_chunk_size")
                    message_complete = False
                    self.offset = len(self.data)
                    break
                size_text, extension = self._parse_chunk_size(chunk_line)
                context["chunk_extension"] = bool(extension)
                if size_text is None:
                    self.anomalies.add("chunk_size_invalid")
                    message_anomalies.add("chunk_size_invalid")
                    context["anomalies"].add("chunk_size_invalid")
                    chunks_meta.append(
                        {
                            "index": len(chunks_meta),
                            "size": None,
                            "extension": extension,
                            "offset": chunk_line_start,
                        }
                    )
                    self._emit(
                        "CHUNK_SIZE",
                        "CHUNK_SIZE_INVALID",
                        chunk_line_start,
                        "CHUNK_SIZE",
                        context,
                    )
                    message_complete = False
                    self.offset = chunk_line_end
                    break
                size = size_text
                chunk_sizes.append(size)
                chunks_meta.append(
                    {
                        "index": len(chunks_meta),
                        "size": size,
                        "extension": extension,
                        "offset": chunk_line_start,
                    }
                )
                context["chunk_zero"] = size == 0
                self._emit(
                    "CHUNK_SIZE",
                    "CHUNK_SIZE",
                    chunk_line_start,
                    "TRAILERS" if size == 0 else "CHUNK_DATA",
                    context,
                )
                self.offset = chunk_line_end
                if size == 0:
                    break
                if self.offset + size > len(self.data):
                    self.anomalies.add("incomplete_chunk_data")
                    message_anomalies.add("incomplete_chunk_data")
                    context["anomalies"].add("incomplete_chunk_data")
                    body_parts.append(self.data[self.offset :])
                    self.offset = len(self.data)
                    message_complete = False
                    break
                body_parts.append(self.data[self.offset : self.offset + size])
                self.offset += size
                self._emit(
                    "CHUNK_DATA",
                    "CHUNK_DATA_COMPLETE",
                    self.offset,
                    "CHUNK_DATA",
                    context,
                )
                if self.data[self.offset : self.offset + 2] != b"\r\n":
                    self.anomalies.add("chunk_data_missing_crlf")
                    message_anomalies.add("chunk_data_missing_crlf")
                    context["anomalies"].add("chunk_data_missing_crlf")
                    message_complete = False
                    break
                self.offset += 2

            if message_complete:
                while True:
                    trailer_line, trailer_line_end, trailer_complete = self._read_line(
                        self.offset, MAX_HEADER_BYTES
                    )
                    if not trailer_complete or trailer_line is None:
                        self.anomalies.add("incomplete_trailers")
                        message_anomalies.add("incomplete_trailers")
                        context["anomalies"].add("incomplete_trailers")
                        message_complete = False
                        self.offset = len(self.data)
                        break
                    self.offset = trailer_line_end
                    if trailer_line == b"":
                        context["trailer_present"] = bool(trailers)
                        self._emit(
                            "TRAILERS",
                            "MESSAGE_END",
                            self.offset,
                            "MESSAGE_END",
                            context,
                        )
                        break
                    trailer_name, trailer_value = self._parse_header(trailer_line)
                    if trailer_name is None:
                        self.anomalies.add("malformed_trailer")
                        message_anomalies.add("malformed_trailer")
                        context["anomalies"].add("malformed_trailer")
                        continue
                    trailers.append((trailer_name, trailer_value))
                    context["trailer_present"] = True
                    self._emit(
                        "TRAILERS",
                        "TRAILER_FIELD",
                        trailer_line_end,
                        "TRAILERS",
                        context,
                    )
        elif framing["mode"] == "CONTENT_LENGTH":
            length = content_length or 0
            available = len(self.data) - self.offset
            take = min(length, available)
            body_parts.append(self.data[self.offset : self.offset + take])
            self.offset += take
            if take < length:
                self.anomalies.add("incomplete_content_length_body")
                message_anomalies.add("incomplete_content_length_body")
                context["anomalies"].add("incomplete_content_length_body")
                message_complete = False
            else:
                self._emit(
                    "BODY",
                    "BODY_COMPLETE",
                    self.offset,
                    "MESSAGE_END",
                    context,
                )
        else:
            self._emit("BODY", "BODY_COMPLETE", self.offset, "MESSAGE_END", context)

        if message_complete and framing["mode"] != "CHUNKED":
            self._emit(
                "MESSAGE_END",
                "MESSAGE_END",
                self.offset,
                "MESSAGE_END",
                context,
            )
        end = self.offset
        seq_start, seq_end = self.stream.seq_range(start, end)
        body = b"".join(body_parts)
        interpretation = {
            "framing": framing["framing"],
            "framing_candidates": framing["framing_candidates"],
            "content_length": content_length,
            "content_length_values": framing["content_length_values"],
            "transfer_encoding": list(transfer_encoding),
            "duplicate_headers": {
                name: len(values)
                for name, values in sorted(context["header_values"].items())
                if len(values) > 1
            },
            "body_length": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "chunks": chunks_meta,
            "trailers": [list(item) for item in trailers],
            "request_boundary": [start, end],
            "next_boundary": end,
            "raw_length": end - start,
            "complete": message_complete,
        }
        trace = tuple(self.connection_transitions[trace_start:])
        return ParsedMessage(
            connection_id=self.stream.connection_id,
            direction=self.stream.direction,
            request_index=self.request_index,
            start_offset=start,
            end_offset=end,
            raw=self.data[start:end],
            state_trace=trace,
            start_line=start_line,
            method=method,
            target=target,
            version=version,
            headers=tuple(headers),
            framing=framing["framing"],
            content_length=content_length,
            transfer_encoding=transfer_encoding,
            body=body,
            chunk_sizes=tuple(chunk_sizes),
            trailers=tuple(trailers),
            next_boundary=end,
            interpretation=interpretation,
            anomalies=tuple(sorted(message_anomalies)),
            complete=message_complete,
            flow_seq_start=seq_start,
            flow_seq_end=seq_end,
        )

    def _emit(
        self,
        state: str,
        event: str,
        offset: int,
        next_state: str,
        context: dict[str, Any],
    ) -> None:
        transition = StateTransition(
            state=state,
            event=event,
            next_state=next_state,
            offset=offset,
        )
        self.connection_transitions.append(transition)
        if self.rule_engine is None:
            return
        evaluation: Evaluation = self.rule_engine.evaluate(
            state=state,
            event=event,
            byte_offset=offset,
            context=context,
        )
        self.rule_hits.extend(evaluation.hits)
        self.suppressed_hits.extend(evaluation.suppressed)
        self.candidate_rules.update(evaluation.candidate_rules)

    def _read_line(
        self,
        offset: int,
        maximum: int,
    ) -> tuple[bytes | None, int, bool]:
        if offset > len(self.data):
            return None, offset, False
        crlf = self.data.find(b"\r\n", offset)
        lf = self.data.find(b"\n", offset)
        if crlf < 0 and lf < 0:
            return None, len(self.data), False
        if crlf >= 0 and (lf < 0 or crlf <= lf):
            end = crlf
            next_offset = crlf + 2
        else:
            end = lf
            next_offset = lf + 1
            self.anomalies.add("bare_lf")
        if end - offset > maximum:
            return None, next_offset, False
        return self.data[offset:end], next_offset, True

    @staticmethod
    def _parse_header(line: bytes) -> tuple[str | None, str]:
        if b":" not in line:
            return None, ""
        name_bytes, value_bytes = line.split(b":", 1)
        try:
            name = name_bytes.decode("ascii").strip()
        except UnicodeDecodeError:
            return None, ""
        if not name or any(character.isspace() for character in name):
            return None, ""
        return name, value_bytes.decode("latin-1").strip()

    @staticmethod
    def _parse_chunk_size(line: bytes) -> tuple[int | None, str]:
        text = line.decode("latin-1", errors="replace").strip()
        if not text:
            return None, ""
        size_part, separator, extension = text.partition(";")
        size_part = size_part.strip()
        if not re.fullmatch(r"[0-9A-Fa-f]+", size_part):
            return None, extension.strip() if separator else ""
        return int(size_part, 16), extension.strip() if separator else ""

    @staticmethod
    def _framing(
        headers: list[tuple[str, str]],
    ) -> tuple[dict[str, Any], int | None, tuple[str, ...], list[dict[str, Any]]]:
        content_lengths: list[int] = []
        content_length_values: list[str] = []
        transfer_values: list[str] = []
        anomalies: list[str] = []
        for name, value in headers:
            lowered = name.casefold()
            if lowered == "content-length":
                content_length_values.append(value)
                for token in value.split(","):
                    token = token.strip()
                    if not token.isdigit():
                        anomalies.append("content_length_invalid")
                        continue
                    content_lengths.append(int(token))
            elif lowered == "transfer-encoding":
                transfer_values.extend(
                    token.strip().casefold()
                    for token in value.split(",")
                    if token.strip()
                )
        transfer_encoding = tuple(transfer_values)
        distinct_lengths = sorted(set(content_lengths))
        if len(distinct_lengths) > 1:
            anomalies.append("content_length_conflict")
        if len(content_length_values) > 1:
            anomalies.append("duplicate_content_length")
        chunked = bool(transfer_values) and transfer_values[-1] == "chunked"
        if transfer_values and not chunked:
            anomalies.append("transfer_encoding_without_final_chunked")
        if content_length_values and transfer_values:
            anomalies.append("te_cl_coexist")
        if chunked:
            candidates = ["CHUNKED"]
            mode = "CHUNKED"
            if content_length_values:
                candidates.append("CONTENT_LENGTH")
                framing_label = "CHUNKED_WITH_CONTENT_LENGTH"
            else:
                framing_label = "CHUNKED"
        elif content_length_values:
            candidates = ["CONTENT_LENGTH"]
            mode = "CONTENT_LENGTH"
            framing_label = (
                "CONTENT_LENGTH_CONFLICT"
                if len(distinct_lengths) > 1
                else "CONTENT_LENGTH"
            )
        else:
            candidates = ["NONE"]
            mode = "NONE"
            framing_label = "NONE"
        content_length = content_lengths[0] if content_lengths else None
        chunks_meta: list[dict[str, Any]] = []
        return (
            {
                "mode": mode,
                "framing": framing_label,
                "framing_candidates": candidates,
                "anomalies": anomalies,
                "content_length_values": content_length_values,
            },
            content_length,
            transfer_encoding,
            chunks_meta,
        )


def build_connection_results(
    flows: Iterable[FlowRecord],
    *,
    rule_engine: RuleEngine | None = None,
) -> dict[str, ConnectionResult]:
    flows_list = list(flows)
    grouped: dict[str, list[FlowRecord]] = defaultdict(list)
    for record in flows_list:
        grouped[record.connection_id].append(record)
    results: dict[str, ConnectionResult] = {}
    for connection_id in sorted(grouped):
        records = grouped[connection_id]
        streams = reassemble_connection(
            records,
            expected_connection_id=connection_id,
        )
        result = ConnectionResult(connection_id=connection_id)
        for (stream_connection_id, direction), stream in sorted(streams.items()):
            if stream_connection_id != connection_id:
                continue
            result.duplicates.extend(stream.duplicates)
            result.errors.extend(stream.errors)
            if direction != "C2S":
                continue
            parser = HTTPStreamParser(stream, rule_engine=rule_engine)
            messages, transitions = parser.parse()
            result.messages.extend(messages)
            result.transitions.extend(transitions)
            result.rule_hits.extend(parser.rule_hits)
            result.suppressed_hits.extend(parser.suppressed_hits)
            for rule_name in sorted(parser.candidate_rules):
                result.candidate_rule_evaluations.setdefault(rule_name, []).append(
                    f"{connection_id}:{stream.direction}"
                )
        results[connection_id] = result
    return results


def parse_mutated_request(
    raw: bytes,
    *,
    connection_id: str,
    rule_engine: RuleEngine | None = None,
) -> tuple[list[ParsedMessage], list[StateTransition], list[Any], list[dict[str, Any]], set[str], list[str]]:
    stream = ReassembledStream(
        connection_id=connection_id,
        direction="C2S",
        data=raw,
        slices=[
            StreamSlice(
                seq=0,
                start=0,
                end=len(raw),
                source_file="<mutation>",
                record_index=0,
            )
        ],
    )
    parser = HTTPStreamParser(stream, rule_engine=rule_engine)
    messages, transitions = parser.parse()
    errors = [item for message in messages for item in ()]
    if not messages:
        errors.append("E_PARSE")
    return (
        messages,
        transitions,
        parser.rule_hits,
        parser.suppressed_hits,
        parser.candidate_rules,
        errors,
    )


def local_interpretation(messages: list[ParsedMessage]) -> dict[str, Any]:
    if not messages:
        return {
            "framing": "UNPARSEABLE",
            "framing_candidates": ["UNPARSEABLE"],
            "request_boundary": [0, 0],
            "body_length": 0,
            "chunks": [],
            "trailers": [],
            "duplicate_headers": {},
            "complete": False,
        }
    first = messages[0]
    result = dict(first.interpretation)
    result["request_count"] = len(messages)
    result["all_boundaries"] = [
        message.interpretation.get("request_boundary") for message in messages
    ]
    result["anomalies"] = sorted(
        {anomaly for message in messages for anomaly in message.anomalies}
    )
    return result
