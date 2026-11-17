from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from .config import ContentState, Policy
from .errors import EdgeError, ProtocolError, error_payload, exit_code_for
from .ledger import ActiveState, Ledger, content_batch_id
from .tcp import JsonLineServer, RequestResult
from .util import JsonLogger, emit_json_line, read_json_file, utc_now_text


class SourceRuntime:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.started_at = utc_now_text()
        self.started_mono = time.monotonic()
        self.request_count = 0
        self.evolution_count = 0

    def bootstrap(
        self,
        policy_doc: Mapping[str, Any] | None,
        content_doc: Mapping[str, Any] | None,
    ) -> tuple[ActiveState, bool]:
        return self.ledger.bootstrap(policy_doc, content_doc)

    def _active(self) -> ActiveState:
        return self.ledger.active_state()

    def status(self) -> dict[str, Any]:
        health = self.ledger.health()
        return {
            "service": "content-source",
            "started_at": self.started_at,
            "uptime_seconds": round(time.monotonic() - self.started_mono, 3),
            "request_count": self.request_count,
            "evolution_count": self.evolution_count,
            **health,
        }

    def catalog(self, expected_version: int | None = None) -> dict[str, Any]:
        active = self._active()
        if expected_version is not None:
            active = self.ledger.assert_version(expected_version)
        return {
            "ledger_version": active.ledger_version,
            "policy_version": active.policy_version,
            "content_set_hash": active.content_set_hash,
            "content_version": active.content.content_version,
            "contents": [
                {
                    "path": path,
                    "etag": value.etag,
                    "media_type": value.media_type,
                    "size": len(value.body),
                    "sha256": value.body_sha256,
                    "sources": list(value.sources),
                    "rule": value.rule,
                }
                for path, value in sorted(active.content.effective.items())
            ],
        }

    def content(self, path: str, expected_version: int | None = None) -> dict[str, Any]:
        active = self._active()
        if expected_version is not None:
            active = self.ledger.assert_version(expected_version)
        value = active.content.effective.get(path)
        if value is None:
            raise EdgeError(
                "E_NOT_FOUND",
                f"content path {path!r} does not exist",
                location=f"content[{path!r}]",
            )
        return {
            "ledger_version": active.ledger_version,
            "policy_version": active.policy_version,
            "content_set_hash": active.content_set_hash,
            "path": value.path,
            "etag": value.etag,
            "body": value.body_text(),
            "body_encoding": value.body_encoding,
            "media_type": value.media_type,
            "sha256": value.body_sha256,
            "sources": list(value.sources),
            "rule": value.rule,
        }

    def evolve(
        self,
        *,
        path: str,
        etag: str,
        body: str,
        body_encoding: str,
        expected_version: int,
    ) -> dict[str, Any]:
        active = self.ledger.assert_version(expected_version)
        old_value = active.content.effective.get(path)
        if old_value is None:
            raise EdgeError(
                "E_NOT_FOUND",
                f"content path {path!r} does not exist",
                location=f"evolve[{path!r}]",
            )
        if old_value.etag == etag:
            raise EdgeError(
                "E_POLICY",
                "content evolution requires a new ETag",
                location=f"evolve[{path!r}].etag",
                current_etag=old_value.etag,
            )
        new_content = active.content.with_evolved_base(path, etag, body, body_encoding)
        ordered = new_content.affected_order(path)
        propagation: list[tuple[str, int, str]] = []
        for affected_path, depth in ordered:
            if depth == 0:
                relation = "trigger"
            elif depth == 1:
                relation = "direct-dependent"
            else:
                relation = "indirect-dependent"
            propagation.append((affected_path, depth, relation))
        planned_batch_id = content_batch_id(
            active.ledger_version + 1,
            path,
            new_content.content_set_hash,
        )
        new_version, entry_versions = self.ledger.commit_content_transition(
            trigger_path=path,
            new_content=new_content,
            batch_id=planned_batch_id,
            propagation=propagation,
            detail={
                "old_etag": old_value.etag,
                "new_etag": etag,
                "trigger_path": path,
            },
        )
        self.evolution_count += 1
        return {
            "ledger_version": new_version,
            "previous_ledger_version": active.ledger_version,
            "batch_id": planned_batch_id,
            "trigger_path": path,
            "old_etag": old_value.etag,
            "new_etag": etag,
            "content_set_hash": new_content.content_set_hash,
            "affected": [
                {
                    "seq": seq,
                    "path": affected_path,
                    "depth": depth,
                    "relation": relation,
                    "etag": new_content.effective[affected_path].etag,
                    "entry_version": entry_versions[affected_path],
                }
                for seq, (affected_path, depth, relation) in enumerate(propagation)
            ],
            "entry_versions": entry_versions,
        }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local deterministic content source (JSON-line/TCP)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--content", required=True)
    parser.add_argument("--log", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logger = JsonLogger(args.log, "source")
    server: JsonLineServer | None = None
    runtime: SourceRuntime | None = None
    try:
        ledger = Ledger(args.db)
        runtime = SourceRuntime(ledger)
        if ledger.has_state():
            policy_doc = None
            content_doc = None
        else:
            policy_doc = read_json_file(args.policy)
            content_doc = read_json_file(args.content)

        def handle(request: dict[str, Any]) -> dict[str, Any] | RequestResult:
            assert runtime is not None
            runtime.request_count += 1
            op = request.get("op")
            if op == "ping":
                return {
                    "service": "content-source",
                    "pid": __import__("os").getpid(),
                    "ledger_version": ledger.current_version() if ledger.has_state() else None,
                }
            if op == "status":
                return runtime.status()
            if op == "catalog":
                version = request.get("ledger_version")
                return runtime.catalog(version if isinstance(version, int) else None)
            if op == "content":
                path = request.get("path")
                if not isinstance(path, str):
                    raise ProtocolError("content op requires string path", location="request.path")
                version = request.get("ledger_version")
                if not isinstance(version, int):
                    raise ProtocolError(
                        "content op requires integer ledger_version",
                        location="request.ledger_version",
                    )
                return runtime.content(path, version)
            if op == "evolve":
                path = request.get("path")
                etag = request.get("etag")
                body = request.get("body")
                body_encoding = request.get("body_encoding", "utf-8")
                version = request.get("ledger_version")
                if not isinstance(path, str):
                    raise ProtocolError("evolve op requires string path", location="request.path")
                if not isinstance(etag, str):
                    raise ProtocolError("evolve op requires string etag", location="request.etag")
                if not isinstance(body, str):
                    raise ProtocolError("evolve op requires string body", location="request.body")
                if not isinstance(body_encoding, str):
                    raise ProtocolError(
                        "body_encoding must be a string", location="request.body_encoding"
                    )
                if not isinstance(version, int):
                    raise ProtocolError(
                        "evolve op requires integer ledger_version",
                        location="request.ledger_version",
                    )
                result = runtime.evolve(
                    path=path,
                    etag=etag,
                    body=body,
                    body_encoding=body_encoding,
                    expected_version=version,
                )
                logger.info(
                    "content_evolved",
                    ledger_version=result["ledger_version"],
                    batch_id=result["batch_id"],
                    trigger_path=path,
                    old_etag=result["old_etag"],
                    new_etag=result["new_etag"],
                    affected=result["affected"],
                )
                return result
            if op == "shutdown":
                logger.info(
                    "exit_report",
                    graceful=True,
                    service="content-source",
                    request_count=runtime.request_count,
                    evolution_count=runtime.evolution_count,
                    ledger_version=ledger.current_version(),
                )
                return RequestResult(
                    {"message": "source shutting down", "graceful": True},
                    stop_after_response=True,
                )
            raise ProtocolError(
                f"unsupported source op {op!r}",
                location="request.op",
                supported=["ping", "status", "catalog", "content", "evolve", "shutdown"],
            )

        server = JsonLineServer(args.host, args.port, handle, logger, "source")
        server.start()
        active, created = runtime.bootstrap(policy_doc, content_doc)
        logger.info(
            "state_ready",
            bootstrap_created=created,
            ledger_version=active.ledger_version,
            policy_version=active.policy_version,
            content_set_hash=active.content_set_hash,
            db=str(Path(args.db).resolve()),
        )
        server.serve_forever()
        return 0
    except EdgeError as exc:
        logger.error(
            "business_error",
            code=exc.code,
            message=exc.message,
            location=exc.location,
            details=exc.details,
        )
        emit_json_line({"ok": False, "component": "source", "error": exc.to_dict()}, sys.stderr)
        if server is not None:
            server.request_stop()
        return exit_code_for(exc)
    except KeyboardInterrupt:
        logger.warning("interrupted", graceful=False)
        if server is not None:
            server.request_stop()
        return 130
    except Exception as exc:  # pragma: no cover - defensive process boundary
        logger.error(
            "internal_error",
            code="E_INTERNAL",
            message=str(exc),
            exception=exc.__class__.__name__,
        )
        emit_json_line(
            {"ok": False, "component": "source", "error": error_payload(exc)}, sys.stderr
        )
        if server is not None:
            server.request_stop()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
