"""Loading and strict validation of route, token-table and request files."""

from __future__ import annotations

import json
from pathlib import Path

from .errors import BusinessError, E_SPEC, spec_error
from .models import (
    POLICIES,
    Request,
    Route,
    SignatureRecord,
    TokenRecord,
    TokenTable,
)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise spec_error(f"cannot read file: {exc}", str(path)) from exc


def _load_json(path: Path) -> tuple[object, str]:
    text = _read_text(path)
    try:
        return json.loads(text), text
    except json.JSONDecodeError as exc:
        raise BusinessError(E_SPEC, f"invalid JSON: {exc.msg}", str(path), exc.lineno) from exc


def _locate_line(text: str, key: str, value: object) -> int:
    needle = f'"{key}"'
    for index, line in enumerate(text.splitlines(), start=1):
        if needle not in line:
            continue
        if isinstance(value, str) and json.dumps(value) in line:
            return index
        if isinstance(value, int) and not isinstance(value, bool):
            fragment = f'"{key}": {value}'
            alt = f'"{key}":{value}'
            if fragment in line or alt in line:
                return index
    return 0


def _require_fields(obj: dict, fields: tuple[str, ...], file: str, line: int) -> None:
    missing = [name for name in fields if name not in obj]
    if missing:
        raise spec_error(f"missing fields {missing}", file, line or None)


def load_routes(path: str | Path) -> tuple[int, list[Route]]:
    path = Path(path)
    data, text = _load_json(path)
    if not isinstance(data, dict):
        raise spec_error("route file root must be an object", str(path))
    if not _is_int(data.get("version")) or data["version"] < 1:
        line = _locate_line(text, "version", data.get("version"))
        raise spec_error("version must be a positive integer", str(path), line or None)
    raw_routes = data.get("routes")
    if not isinstance(raw_routes, list) or not raw_routes:
        raise spec_error("routes must be a non-empty list", str(path))

    routes: list[Route] = []
    seen_names: set[str] = set()
    int_fields = ("rate_per_sec", "burst", "window_ms", "max_in_window")
    for index, raw in enumerate(raw_routes):
        if not isinstance(raw, dict):
            raise spec_error(f"route #{index} must be an object", str(path), index + 1)
        line = _locate_line(text, "name", raw.get("name", "")) or index + 1
        _require_fields(
            raw,
            ("name", "prefix", "auth_policy", *int_fields),
            str(path),
            line,
        )
        name = raw["name"]
        if not isinstance(name, str) or not name.strip():
            raise spec_error("route name must be a non-empty string", str(path), line)
        if name in seen_names:
            raise spec_error(f"duplicate route name {name!r}", str(path), line)
        prefix = raw["prefix"]
        if not isinstance(prefix, str) or not prefix.startswith("/") or any(ch.isspace() for ch in prefix):
            raise spec_error(f"route {name!r} has illegal path prefix {prefix!r}", str(path), line)
        policy = raw["auth_policy"]
        if policy not in POLICIES:
            raise spec_error(f"route {name!r} has unknown auth policy {policy!r}", str(path), line)
        for field_name in int_fields:
            value = raw[field_name]
            if not _is_int(value) or value < 1:
                raise spec_error(
                    f"route {name!r} field {field_name} must be a positive integer",
                    str(path),
                    line,
                )
        seen_names.add(name)
        routes.append(
            Route(
                name=name,
                prefix=prefix,
                auth_policy=policy,
                rate_per_sec=raw["rate_per_sec"],
                burst=raw["burst"],
                window_ms=raw["window_ms"],
                max_in_window=raw["max_in_window"],
                source_line=line,
            )
        )
    return data["version"], routes


def load_token_table(path: str | Path) -> TokenTable:
    path = Path(path)
    data, text = _load_json(path)
    if not isinstance(data, dict):
        raise spec_error("token table root must be an object", str(path))
    if not _is_int(data.get("version")) or data["version"] < 1:
        line = _locate_line(text, "version", data.get("version"))
        raise spec_error("version must be a positive integer", str(path), line or None)

    table = TokenTable(version=data["version"])
    raw_tokens = data.get("tokens", [])
    if not isinstance(raw_tokens, list):
        raise spec_error("tokens must be a list", str(path))
    for index, raw in enumerate(raw_tokens):
        if not isinstance(raw, dict):
            raise spec_error(f"token #{index} must be an object", str(path), index + 1)
        line = _locate_line(text, "token", raw.get("token", "")) or index + 1
        _require_fields(raw, ("token", "client_id", "expires_at_ms"), str(path), line)
        token, client_id, expires = raw["token"], raw["client_id"], raw["expires_at_ms"]
        if not isinstance(token, str) or not token:
            raise spec_error("token must be a non-empty string", str(path), line)
        if not isinstance(client_id, str) or not client_id:
            raise spec_error("client_id must be a non-empty string", str(path), line)
        if not _is_int(expires) or expires < 0:
            raise spec_error("expires_at_ms must be a non-negative integer", str(path), line)
        if token in table.tokens:
            raise spec_error(f"duplicate token {token!r} (must be unique per client)", str(path), line)
        table.tokens[token] = TokenRecord(token, client_id, expires, line)

    raw_signatures = data.get("signatures", [])
    if not isinstance(raw_signatures, list):
        raise spec_error("signatures must be a list", str(path))
    for index, raw in enumerate(raw_signatures):
        if not isinstance(raw, dict):
            raise spec_error(f"signature #{index} must be an object", str(path), index + 1)
        line = _locate_line(text, "key_id", raw.get("key_id", "")) or index + 1
        _require_fields(raw, ("key_id", "client_id", "secret", "expires_at_ms"), str(path), line)
        key_id, client_id, secret, expires = (
            raw["key_id"],
            raw["client_id"],
            raw["secret"],
            raw["expires_at_ms"],
        )
        if not isinstance(key_id, str) or not key_id:
            raise spec_error("key_id must be a non-empty string", str(path), line)
        if not isinstance(client_id, str) or not client_id:
            raise spec_error("client_id must be a non-empty string", str(path), line)
        if not isinstance(secret, str) or not secret:
            raise spec_error("secret must be a non-empty string", str(path), line)
        if not _is_int(expires) or expires < 0:
            raise spec_error("expires_at_ms must be a non-negative integer", str(path), line)
        if key_id in table.signatures:
            raise spec_error(f"duplicate key_id {key_id!r}", str(path), line)
        table.signatures[key_id] = SignatureRecord(key_id, client_id, secret, expires, line)
    return table


def load_requests(path: str | Path, routes: list[Route] | None = None) -> list[Request]:
    path = Path(path)
    text = _read_text(path)
    route_names = {route.name for route in routes or []}
    requests: list[Request] = []
    seen_seqs: set[int] = set()
    last_ts: int | None = None
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise BusinessError(E_SPEC, f"invalid NDJSON: {exc.msg}", str(path), line_no) from exc
        if not isinstance(raw, dict):
            raise spec_error("request line must be a JSON object", str(path), line_no)
        _require_fields(raw, ("seq", "route", "client_id", "token", "ts_ms"), str(path), line_no)
        seq, route_name, client_id, token, ts_ms = (
            raw["seq"],
            raw["route"],
            raw["client_id"],
            raw["token"],
            raw["ts_ms"],
        )
        if not _is_int(seq) or seq < 1:
            raise spec_error("seq must be a positive integer", str(path), line_no)
        if seq in seen_seqs:
            raise spec_error(f"duplicate seq {seq}", str(path), line_no)
        if not isinstance(route_name, str) or not route_name:
            raise spec_error("route must be a non-empty string", str(path), line_no)
        if routes is not None and route_name not in route_names:
            raise spec_error(f"unknown route {route_name!r}", str(path), line_no)
        if not isinstance(client_id, str) or not client_id:
            raise spec_error("client_id must be a non-empty string", str(path), line_no)
        if not isinstance(token, str) or not token:
            raise spec_error("token must be a non-empty string", str(path), line_no)
        if not _is_int(ts_ms) or ts_ms < 0:
            raise spec_error("ts_ms must be a non-negative integer", str(path), line_no)
        if last_ts is not None and ts_ms < last_ts:
            raise spec_error(
                f"ts_ms {ts_ms} is earlier than previous {last_ts}; timestamps must be monotonic",
                str(path),
                line_no,
            )
        signed_at = raw.get("signed_at_ms")
        if signed_at is not None and (not _is_int(signed_at) or signed_at < 0):
            raise spec_error("signed_at_ms must be a non-negative integer", str(path), line_no)
        signature = raw.get("signature")
        if signature is not None and not isinstance(signature, str):
            raise spec_error("signature must be a string", str(path), line_no)
        seen_seqs.add(seq)
        last_ts = ts_ms
        requests.append(
            Request(
                seq=seq,
                route=route_name,
                client_id=client_id,
                token=token,
                ts_ms=ts_ms,
                signed_at_ms=signed_at,
                signature=signature,
                source_line=line_no,
            )
        )
    if not requests:
        raise spec_error("request stream is empty", str(path))
    return requests
