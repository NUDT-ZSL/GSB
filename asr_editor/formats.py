"""Canonical serialization, hashing, sidecar metadata and atomic file I/O."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Origin, Token, Transcript, Utterance
from .timeutil import seconds_text, srt_time_text

_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


class FormatWriteError(ValueError):
    pass


class SidecarError(ValueError):
    pass


@dataclass(slots=True)
class JsonNumber:
    lexical: str


def _check_text(value: str) -> None:
    if _SURROGATE_RE.search(value):
        raise FormatWriteError("cannot serialize invalid UTF-8 text")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def content_hash(value: Any) -> str:
    return sha256_text(canonical_json(value))


def utterance_sentence_times(utterance: Utterance) -> tuple[int, int]:
    if utterance.tokens:
        return utterance.tokens[0].start_ms, utterance.tokens[-1].end_ms
    return utterance.start_ms, utterance.end_ms


def normalized_text(text: str) -> str:
    return " ".join(text.split())


def v2_content_payload(transcript: Transcript) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for utterance in transcript.utterances:
        start_ms, end_ms = utterance_sentence_times(utterance)
        payload.append(
            {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": normalized_text(utterance.text),
            }
        )
    return payload


def v2_content_hash(transcript: Transcript) -> str:
    return content_hash(v2_content_payload(transcript))


def token_metadata_payload(transcript: Transcript) -> list[dict[str, Any]]:
    return [
        {
            "text": token.text,
            "start_ms": token.start_ms,
            "end_ms": token.end_ms,
            "inferred": token.inferred,
        }
        for utterance in transcript.utterances
        for token in utterance.tokens
    ]


def token_metadata_hash(transcript: Transcript) -> str:
    return content_hash(token_metadata_payload(transcript))


def transcript_fingerprint(transcript: Transcript) -> str:
    """Hash semantic content, excluding filesystem/provenance-only fields.

    Re-parsing an equivalent v1 file reconstructs different line numbers and
    source paths; those must not force a spurious rollback.
    """

    payload = []
    for utterance in transcript.utterances:
        start_ms, end_ms = utterance_sentence_times(utterance)
        payload.append(
            {
                "id": utterance.uid,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": normalized_text(utterance.text),
                "tokens": [
                    {
                        "text": token.text,
                        "start_ms": token.start_ms,
                        "end_ms": token.end_ms,
                        "inferred": token.inferred,
                    }
                    for token in utterance.tokens
                ],
            }
        )
    return content_hash({"language": transcript.language, "utterances": payload})


def build_sidecar(transcript: Transcript) -> dict[str, Any]:
    utterances: list[dict[str, Any]] = []
    for utterance in transcript.utterances:
        start_ms, end_ms = utterance_sentence_times(utterance)
        utterances.append(
            {
                "id": utterance.uid,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": normalized_text(utterance.text),
                "tokens": [
                    {
                        "text": token.text,
                        "start_ms": token.start_ms,
                        "end_ms": token.end_ms,
                        "inferred": token.inferred,
                    }
                    for token in utterance.tokens
                ],
            }
        )
    return {
        "schema_version": 1,
        "format": "asr-interop-token-sidecar",
        "source_version": 1,
        "v2_content_hash": v2_content_hash(transcript),
        "token_metadata_hash": token_metadata_hash(transcript),
        "utterances": utterances,
    }


def sidecar_file_hash(sidecar: dict[str, Any]) -> str:
    return content_hash(sidecar)


def read_sidecar(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise SidecarError(f"cannot read sidecar {source}: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SidecarError(f"sidecar is not UTF-8: {exc}") from exc
    if _SURROGATE_RE.search(text):
        raise SidecarError("sidecar contains invalid UTF-8 text")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SidecarError(
            f"invalid sidecar JSON at line {exc.lineno}: {exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise SidecarError("sidecar must be a JSON object")
    if (
        data.get("schema_version") != 1
        or data.get("format") != "asr-interop-token-sidecar"
    ):
        raise SidecarError("unsupported sidecar schema")
    if not isinstance(data.get("v2_content_hash"), str):
        raise SidecarError("sidecar is missing v2_content_hash")
    if not isinstance(data.get("token_metadata_hash"), str):
        raise SidecarError("sidecar is missing token_metadata_hash")
    if not isinstance(data.get("utterances"), list):
        raise SidecarError("sidecar utterances must be a list")
    sidecar_token_payload = [
        {
            "text": token.get("text"),
            "start_ms": token.get("start_ms"),
            "end_ms": token.get("end_ms"),
            "inferred": token.get("inferred"),
        }
        for utterance in data["utterances"]
        if isinstance(utterance, dict)
        for token in utterance.get("tokens", [])
        if isinstance(token, dict)
    ]
    if data.get("token_metadata_hash") != content_hash(sidecar_token_payload):
        raise SidecarError("sidecar token metadata hash mismatch")
    return data


def _validate_sidecar_utterance(item: Any, index: int) -> None:
    if not isinstance(item, dict):
        raise SidecarError(f"sidecar utterance {index} is not an object")
    for key in ("text",):
        if not isinstance(item.get(key), str):
            raise SidecarError(f"sidecar utterance {index} has invalid {key}")
    for key in ("start_ms", "end_ms"):
        if not isinstance(item.get(key), int):
            raise SidecarError(f"sidecar utterance {index} has invalid {key}")
    if item["end_ms"] < item["start_ms"]:
        raise SidecarError(f"sidecar utterance {index} has reversed times")
    tokens = item.get("tokens")
    if not isinstance(tokens, list) or not tokens:
        raise SidecarError(f"sidecar utterance {index} has no tokens")
    previous_end = -1
    for token_index, token in enumerate(tokens):
        if not isinstance(token, dict):
            raise SidecarError(
                f"sidecar utterance {index} token {token_index} is not an object"
            )
        if not isinstance(token.get("text"), str) or not token["text"].strip():
            raise SidecarError(
                f"sidecar utterance {index} token {token_index} has empty text"
            )
        for key in ("start_ms", "end_ms"):
            if not isinstance(token.get(key), int):
                raise SidecarError(
                    f"sidecar utterance {index} token {token_index} has invalid {key}"
                )
        if token["start_ms"] < 0 or token["end_ms"] < token["start_ms"]:
            raise SidecarError(
                f"sidecar utterance {index} token {token_index} has invalid times"
            )
        if token["start_ms"] < previous_end:
            raise SidecarError(f"sidecar utterance {index} has overlapping tokens")
        previous_end = token["end_ms"]
        if not isinstance(token.get("inferred"), bool):
            raise SidecarError(
                f"sidecar utterance {index} token {token_index} has invalid inferred flag"
            )


def restore_tokens_from_sidecar(
    transcript: Transcript, sidecar: dict[str, Any]
) -> tuple[bool, str]:
    """Restore token segmentation and timestamps when the content hash matches."""

    if sidecar.get("v2_content_hash") != v2_content_hash(transcript):
        return False, "v2 content hash mismatch"
    sidecar_utterances = sidecar.get("utterances")
    if not isinstance(sidecar_utterances, list) or len(sidecar_utterances) != len(
        transcript.utterances
    ):
        return False, "utterance count mismatch"

    restored: list[Utterance] = []
    for index, (utterance, side_utt) in enumerate(
        zip(transcript.utterances, sidecar_utterances)
    ):
        _validate_sidecar_utterance(side_utt, index)
        if normalized_text(str(side_utt.get("text", ""))) != normalized_text(
            utterance.text
        ):
            return False, f"utterance {index} text mismatch"
        tokens: list[Token] = []
        for token_index, token_data in enumerate(side_utt["tokens"]):
            token_text = str(token_data["text"])
            tokens.append(
                Token(
                    text=token_text,
                    start_ms=int(token_data["start_ms"]),
                    end_ms=int(token_data["end_ms"]),
                    inferred=bool(token_data["inferred"]),
                    origin=Origin(
                        source_version=2,
                        start_ms=None,
                        end_ms=None,
                        text=token_text,
                        source_file=utterance.origin.source_file
                        if utterance.origin
                        else None,
                        source_line=utterance.origin.source_line
                        if utterance.origin
                        else None,
                    ),
                )
            )
        if normalized_text(" ".join(token.text for token in tokens)) != normalized_text(
            utterance.text
        ):
            return False, f"utterance {index} token text mismatch"
        restored.append(
            Utterance(
                uid=utterance.uid,
                start_ms=tokens[0].start_ms,
                end_ms=tokens[-1].end_ms,
                tokens=tokens,
                origin=utterance.origin,
            )
        )

    transcript.utterances = restored
    transcript.source_version = 1
    transcript.timing_origin = "v2-sidecar-restored"
    return True, "sidecar hash and text matched"


def render_pretty_json(value: Any, level: int = 0) -> str:
    indent = "  " * level
    child_indent = "  " * (level + 1)
    if isinstance(value, JsonNumber):
        return value.lexical
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = []
        for key, item in value.items():
            rendered_key = json.dumps(str(key), ensure_ascii=False)
            items.append(
                f"{child_indent}{rendered_key}: {render_pretty_json(item, level + 1)}"
            )
        return "{\n" + ",\n".join(items) + f"\n{indent}" + "}"
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        items = [
            f"{child_indent}{render_pretty_json(item, level + 1)}" for item in value
        ]
        return "[\n" + ",\n".join(items) + f"\n{indent}" + "]"
    if isinstance(value, str):
        _check_text(value)
        return json.dumps(value, ensure_ascii=False)
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return json.dumps(value)
    raise TypeError(f"cannot render JSON value of type {type(value).__name__}")


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_text(path: str | Path, text: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = destination.stat().st_mode & 0o777
    except OSError:
        mode = 0o644
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_path, mode)
        except OSError:
            pass
        os.replace(temp_path, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(path: str | Path, value: Any) -> None:
    text = render_pretty_json(value) + "\n"
    atomic_write_text(path, text)


def serialize_v1(transcript: Transcript) -> str:
    utterances: list[Any] = []
    for utterance in transcript.utterances:
        _check_text(utterance.uid)
        if not utterance.tokens:
            raise FormatWriteError(f"utterance {utterance.uid!r} has no tokens")
        tokens: list[Any] = []
        for token in utterance.tokens:
            if not token.text.strip():
                raise FormatWriteError("token text cannot be empty")
            _check_text(token.text)
            if token.start_ms < 0 or token.end_ms < token.start_ms:
                raise FormatWriteError(f"invalid token time in {utterance.uid!r}")
            tokens.append(
                {
                    "text": token.text,
                    "start": JsonNumber(seconds_text(token.start_ms)),
                    "end": JsonNumber(seconds_text(token.end_ms)),
                }
            )
        start_ms, end_ms = utterance_sentence_times(utterance)
        utterance_payload = {
            "id": utterance.uid,
            "start": JsonNumber(seconds_text(start_ms)),
            "end": JsonNumber(seconds_text(end_ms)),
            "text": utterance.text,
            "tokens": tokens,
        }
        utterances.append(utterance_payload)
    payload = {
        "version": 1,
        "language": transcript.language or "und",
        "utterances": utterances,
    }
    return render_pretty_json(payload) + "\n"


def serialize_v2(transcript: Transcript) -> str:
    lines: list[str] = []
    for index, utterance in enumerate(transcript.utterances, start=1):
        start_ms, end_ms = utterance_sentence_times(utterance)
        text = normalized_text(utterance.text)
        if not text:
            raise FormatWriteError(f"utterance {utterance.uid!r} has empty text")
        _check_text(text)
        lines.extend(
            [
                str(index),
                f"{srt_time_text(start_ms)} --> {srt_time_text(end_ms)}",
                text,
                "",
            ]
        )
    return "\n".join(lines)


def write_v1(path: str | Path, transcript: Transcript) -> None:
    atomic_write_text(path, serialize_v1(transcript))


def write_v2(path: str | Path, transcript: Transcript) -> None:
    atomic_write_text(path, serialize_v2(transcript))


def write_sidecar(path: str | Path, sidecar: dict[str, Any]) -> None:
    atomic_write_json(path, sidecar)


def write_outputs(
    transcript: Transcript,
    v1_path: str | Path,
    v2_path: str | Path,
    sidecar_path: str | Path,
) -> dict[str, Any]:
    sidecar = build_sidecar(transcript)
    # Snapshot-first callers commit the snapshot before these outputs; writes
    # remain individually atomic and UTF-8 only.
    write_v1(v1_path, transcript)
    write_v2(v2_path, transcript)
    write_sidecar(sidecar_path, sidecar)
    return sidecar
