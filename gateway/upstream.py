"""Upstream replica clients.

SimulatedUpstream is a pure, deterministic function used by the engine so
that runs are bit-reproducible. HttpUpstream talks to the local loopback
test service (gateway.upstream_server) and maps transport/contract
failures onto the same E_xxx business codes. Both share the digest
contract: sha256(f"{service}|{request_id}|{tokens}").
"""
import hashlib
import json
import urllib.error
import urllib.request

from . import errors
from .errors import GatewayError


def expected_digest(service, request_id, tokens):
    return hashlib.sha256(
        f"{service}|{request_id}|{tokens}".encode("utf-8")).hexdigest()


def _check_budget(service_ms, per_request_budget_ms, request_id):
    if service_ms > per_request_budget_ms:
        raise GatewayError(
            errors.E_UPSTREAM_BUDGET,
            "upstream response exceeded the per-request budget",
            request_id=request_id, response_ms=service_ms,
            budget_ms=per_request_budget_ms)


class SimulatedUpstream:
    """Deterministic in-process upstream used by the engine."""

    def call(self, service, request_id, tokens, service_ms,
             per_request_budget_ms, fault=None):
        if fault == "conn_refused":
            raise GatewayError(
                errors.E_UPSTREAM_CONN_REFUSED,
                "upstream replica refused the connection",
                request_id=request_id, service=service, replica="sim-0")
        if fault == "budget_exceeded":
            service_ms = per_request_budget_ms + 1
        _check_budget(service_ms, per_request_budget_ms, request_id)
        digest = expected_digest(service, request_id, tokens)
        if fault == "digest_mismatch":
            raise GatewayError(
                errors.E_UPSTREAM_DIGEST,
                "upstream response digest mismatch",
                request_id=request_id, service=service,
                expected=digest, actual="0" * 64)
        return {"digest": digest, "response_ms": service_ms}


class HttpUpstream:
    """Client for the local loopback test service (127.0.0.1 only)."""

    def __init__(self, port):
        self.base_url = f"http://127.0.0.1:{port}/infer"

    def call(self, service, request_id, tokens, service_ms,
             per_request_budget_ms, fault=None):
        payload = json.dumps({
            "service": service,
            "request_id": request_id,
            "tokens": tokens,
            "service_ms": service_ms,
            "fault": fault,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (ConnectionRefusedError, urllib.error.URLError) as exc:
            raise GatewayError(
                errors.E_UPSTREAM_CONN_REFUSED,
                "upstream replica refused the connection",
                request_id=request_id, service=service,
                detail=str(exc))
        response_ms = int(body.get("response_ms", 0))
        _check_budget(response_ms, per_request_budget_ms, request_id)
        digest = expected_digest(service, request_id, tokens)
        if body.get("digest") != digest:
            raise GatewayError(
                errors.E_UPSTREAM_DIGEST,
                "upstream response digest mismatch",
                request_id=request_id, service=service,
                expected=digest, actual=body.get("digest"))
        return {"digest": digest, "response_ms": response_ms}
