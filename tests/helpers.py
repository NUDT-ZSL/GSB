from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from supply_diag.manifest import parse_manifest


def content_bytes(content: Any) -> bytes:
    if isinstance(content, str):
        return content.encode("utf-8")
    return json.dumps(
        content, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def component(
    component_id: str,
    *,
    version: str = "1.0.0",
    dependencies: list[dict] | None = None,
    signature_status: str = "signed",
    algorithm: str = "sha256",
    content: Any | None = None,
    hmac_key: str = "test-key",
    digest: str | None = None,
) -> dict:
    if content is None:
        content = {"component": component_id, "version": version}
    payload = content_bytes(content)
    if digest is None:
        if algorithm == "sha256":
            digest = hashlib.sha256(payload).hexdigest()
        else:
            digest = hmac.new(
                hmac_key.encode("utf-8"), payload, hashlib.sha256
            ).hexdigest()
    result = {
        "component_id": component_id,
        "version": version,
        "hash_algorithm": algorithm,
        "content_hash": digest,
        "signature_status": signature_status,
        "dependencies": dependencies or [],
        "content": content,
    }
    if algorithm == "hmac-sha256":
        result["hmac_key"] = hmac_key
    return result


def manifest(
    components: list[dict],
    *,
    project_id: str = "project",
    manifest_version: str = "1.0.0",
):
    return parse_manifest(
        {
            "project_id": project_id,
            "manifest_version": manifest_version,
            "components": components,
        },
        source_path=f"<{project_id}>",
    )


def component_result(manifest_result: dict, component_id: str) -> dict:
    return next(
        component
        for component in manifest_result["components"]
        if component["component_id"] == component_id
    )

