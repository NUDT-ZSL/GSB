from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from bs4 import BeautifulSoup

from app.db import canonical_json


class DetailParseError(Exception):
    def __init__(self, message: str, code: str = "E_PARSE"):
        super().__init__(message)
        self.code = code


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return canonical_json(value)
    return str(value).strip()


def _declared_meta(soup: BeautifulSoup, name: str) -> tuple[str | None, str | None]:
    wanted = name.lower()
    for meta in soup.find_all("meta"):
        meta_name = (meta.get("name") or meta.get("property") or "").strip().lower()
        if meta_name == wanted:
            value = (meta.get("content") or "").strip()
            if value:
                return value, f"meta[name={meta.get('name') or meta.get('property')}]"
    return None, None


def _html_fields(soup: BeautifulSoup) -> dict[str, str]:
    fields: dict[str, str] = {}
    for node in soup.select("[data-field]"):
        key = (node.get("data-field") or "").strip()
        if key:
            fields[key] = _text(node.get("data-value") or node.get_text(" ", strip=True))
    for term in soup.find_all("dt"):
        key = term.get_text(" ", strip=True)
        value_node = term.find_next_sibling("dd")
        if key and value_node is not None:
            fields.setdefault(key, _text(value_node.get_text(" ", strip=True)))
    for term in soup.select("[data-key]"):
        key = (term.get("data-key") or "").strip()
        if key:
            fields[key] = _text(term.get("data-value") or term.get_text(" ", strip=True))
    return fields


def _json_payload(soup: BeautifulSoup) -> dict[str, Any] | None:
    script = soup.find("script", id="audit-detail")
    if script is None:
        script = soup.find("script", attrs={"type": "application/json"})
    if script is None or not script.string:
        return None
    try:
        value = json.loads(script.string)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _business_id(soup: BeautifulSoup, payload: dict[str, Any] | None) -> str | None:
    if payload:
        for key in ("businessId", "business_id"):
            if payload.get(key):
                return str(payload[key])
    body = soup.find(attrs={"data-business-id": True})
    if body:
        return str(body.get("data-business-id"))
    meta_value, _ = _declared_meta(soup, "businessId")
    return meta_value


def _revision(
    soup: BeautifulSoup,
    payload: dict[str, Any] | None,
    fallback_revision: int,
) -> tuple[int, str]:
    if payload and payload.get("revision") is not None:
        try:
            return int(payload["revision"]), "script#audit-detail.revision"
        except (TypeError, ValueError):
            raise DetailParseError("revision in JSON payload is not an integer", "E_PARSE")
    meta_value, source = _declared_meta(soup, "auditRevision")
    if meta_value:
        try:
            return int(meta_value), source or "meta[auditRevision]"
        except ValueError:
            raise DetailParseError("meta auditRevision is not an integer", "E_PARSE")
    body = soup.find(attrs={"data-revision": True})
    if body:
        try:
            return int(body["data-revision"]), "data-revision"
        except (TypeError, ValueError):
            raise DetailParseError("data-revision is not an integer", "E_PARSE")
    return int(fallback_revision), "list-entry-fallback"


def _v1_fields(soup: BeautifulSoup) -> dict[str, str]:
    fields = _html_fields(soup)
    if not fields:
        raise DetailParseError("v1 detail has no field nodes", "E_PARSE")
    return fields


def _v2_fields(payload: dict[str, Any] | None, soup: BeautifulSoup) -> dict[str, str]:
    source: dict[str, Any] = {}
    if payload:
        record = payload.get("record") or payload.get("data") or {}
        if isinstance(record, dict):
            attributes = record.get("attributes")
            if isinstance(attributes, dict):
                source = attributes
            elif record:
                source = {key: value for key, value in record.items() if key != "schemaVersion"}
    if not source:
        source = _html_fields(soup)
    if not source:
        raise DetailParseError("v2 detail has no attributes", "E_PARSE")
    return {str(key): _text(value) for key, value in source.items()}


def parse_detail(
    html: str,
    *,
    fallback_business_id: str,
    fallback_schema_version: str,
    fallback_revision: int,
    source_url: str = "",
) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    payload = _json_payload(soup)
    declared, declaration_source = _declared_meta(soup, "schemaVersion")
    if not declared and payload:
        for key in ("schemaVersion", "schema_version"):
            if payload.get(key):
                declared = str(payload[key])
                declaration_source = "script#audit-detail.schemaVersion"
                break

    business_id = _business_id(soup, payload) or fallback_business_id
    revision, revision_source = _revision(soup, payload, fallback_revision)

    if not business_id:
        raise DetailParseError("detail does not identify a business id", "E_PARSE")

    if declared == "1.0":
        selected = "v1"
        fields = _v1_fields(soup)
        schema_version = declared
        unresolved = False
    elif declared == "2.0":
        selected = "v2"
        fields = _v2_fields(payload, soup)
        schema_version = declared
        unresolved = False
    else:
        selected = "raw-fallback"
        fields = _html_fields(soup)
        if payload:
            payload_fields = _v2_fields(payload, soup)
            for key, value in payload_fields.items():
                fields.setdefault(f"json.{key}", value)
        if not fields:
            text = soup.get_text("\n", strip=True)
            fields = {"raw.text": text}
        schema_version = declared if declared else "__missing__"
        unresolved = True

    source_parts = urlsplit(source_url)
    stable_query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(source_parts.query, keep_blank_values=True)
            if key in {"id", "wave", "variant"}
        ]
    )
    source_path = source_parts.path + (f"?{stable_query}" if stable_query else "")
    evidence = {
        "declared_schema_version": declared,
        "declaration_source": declaration_source,
        "selected_parser": selected,
        "parser_revision": 1,
        "revision_source": revision_source,
        "source_path": source_path,
        "fallback": selected == "raw-fallback",
        "html_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        "field_count": len(fields),
    }
    return {
        "business_id": business_id,
        "schema_version": schema_version,
        "declared_schema_version": declared,
        "revision": revision,
        "parser_version": selected,
        "unresolved": unresolved,
        "fields": dict(sorted(fields.items())),
        "schema_evidence": evidence,
    }
