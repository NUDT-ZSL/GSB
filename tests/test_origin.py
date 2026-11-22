from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from cdn_edge_workbench.models import CacheEntry
from cdn_edge_workbench.origin import (
    LocalOriginServer,
    OriginRequestError,
    request_origin_with_retries,
)
from cdn_edge_workbench.snapshots import load_samples


def entry_for(path: str, etag: str, version: int) -> CacheEntry:
    return CacheEntry(
        node="edge-test",
        path=path,
        digest_algorithm="sha256",
        content_digest=etag.split(":", 1)[1].rstrip('"'),
        content_version=version,
        etag=etag,
        last_verified_clock=100,
        expires_at_clock=110,
        cache_generation=1,
        source_version="v2",
        source_ref="test",
        raw_time={},
        effective_ttl_seconds=10,
    )


def test_conditional_304_and_200(samples_dir: Path) -> None:
    loaded = load_samples(samples_dir)
    resource = loaded.resources["/assets/app.js"]
    current = entry_for(resource.path, resource.etag, resource.content_version)
    stale = current.with_changes(
        etag='"sha256:' + "0" * 64 + '"',
        content_digest="0" * 64,
        content_version=1,
    )
    with LocalOriginServer(loaded.resources, policy=loaded.policy) as server:
        response_304 = request_origin_with_retries(
            base_url=server.base_url,
            entry=current,
            policy=loaded.policy,
        )
        response_200 = request_origin_with_retries(
            base_url=server.base_url,
            entry=stale,
            policy=loaded.policy,
        )
    assert response_304.status == 304
    assert response_304.body == b""
    assert response_200.status == 200
    assert response_200.headers["etag"] == resource.etag
    assert response_200.body == resource.body


def test_malformed_headers_and_timeout_are_distinct(samples_dir: Path) -> None:
    loaded = load_samples(samples_dir)
    resource = loaded.resources["/api/config.json"]
    entry = entry_for(resource.path, resource.etag, resource.content_version)
    policy = replace(
        loaded.policy,
        origin_request_budget_ms=80,
        origin_retry_budget=0,
    )
    with LocalOriginServer(loaded.resources, policy=policy) as server:
        server.set_behavior(resource.path, "malformed_headers")
        with pytest.raises(OriginRequestError) as malformed:
            request_origin_with_retries(
                base_url=server.base_url,
                entry=entry,
                policy=policy,
            )
        assert malformed.value.code.value == "E1205_ORIGIN_MALFORMED_HEADERS"
    with LocalOriginServer(loaded.resources, policy=policy) as server:
        server.set_behavior(resource.path, "request_timeout", delay_ms=220)
        with pytest.raises(OriginRequestError) as timeout:
            request_origin_with_retries(
                base_url=server.base_url,
                entry=entry,
                policy=policy,
            )
        assert timeout.value.code.value == "E1204_ORIGIN_REQUEST_TIMEOUT"


def test_connection_close_is_classified(samples_dir: Path) -> None:
    loaded = load_samples(samples_dir)
    resource = loaded.resources["/assets/app.js"]
    entry = entry_for(resource.path, resource.etag, resource.content_version)
    policy = replace(loaded.policy, origin_retry_budget=0)
    with LocalOriginServer(loaded.resources, policy=policy) as server:
        server.set_behavior(resource.path, "connection_refused")
        with pytest.raises(OriginRequestError) as error:
            request_origin_with_retries(
                base_url=server.base_url,
                entry=entry,
                policy=policy,
            )
    assert error.value.code.value == "E1206_ORIGIN_CONNECTION_REFUSED"

