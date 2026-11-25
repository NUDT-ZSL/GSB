from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .canonical import atomic_write_json, read_json
from .errors import WorkbenchError, error_record
from .probes import run_diagnostic_probes
from .runtime import Workbench
from .selfcheck import run_selfcheck


STATIC_INDEX = Path(__file__).parent / "static" / "index.html"


def _read_optional_json(path: Path) -> Any:
    return read_json(path) if path.exists() else None


def create_handler(state_dir: str | Path) -> type[BaseHTTPRequestHandler]:
    state = Path(state_dir)

    class DiagnosticsHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, value: Any, status: int = 200) -> None:
            body = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self) -> None:
            body = STATIC_INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, WorkbenchError):
                self._send_json(exc.to_dict(), exc.http_status)
            else:
                self._send_json(error_record(exc, source="web"), 500)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            try:
                if path in {"/", "/index.html"}:
                    self._send_html()
                elif path == "/api/overview":
                    self._send_json(self._overview())
                elif path == "/api/jobs":
                    self._send_json(self._jobs())
                elif path == "/api/cache":
                    job_id = query.get("job_id", [""])[0]
                    self._send_json(Workbench(state).query_cache(job_id))
                elif path == "/api/ledgers":
                    self._send_json(self._ledgers())
                elif path == "/api/codes":
                    self._send_json({"codes": Workbench(state).recent_codes(80)})
                elif path == "/api/diagnoses":
                    self._send_json(self._diagnoses())
                elif path == "/api/selfcheck":
                    self._send_json(_read_optional_json(state / "selfcheck.json") or {"ok": None, "checks": []})
                elif path == "/api/incremental":
                    self._send_json(_read_optional_json(state / "incremental-report.json") or {})
                else:
                    self._send_json({"ok": False, "code": "E_NOT_FOUND", "path": path}, 404)
            except Exception as exc:  # noqa: BLE001
                self._handle_error(exc)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8") or "{}")
                if path == "/api/selfcheck":
                    report = run_selfcheck(state)
                    atomic_write_json(state / "selfcheck.json", report)
                    self._send_json(report)
                elif path == "/api/probes":
                    bench = Workbench(state)
                    report = run_diagnostic_probes(bench)
                    self._send_json(report)
                elif path == "/api/migrate":
                    bench = Workbench(state)
                    direction = body.get("direction", "v1-to-v2")
                    committed = bench.load_committed_state()
                    enrichment = {
                        job_id: {
                            "input_hash": job.get("input_hash", ""),
                            "result_hash": job.get("result_hash", ""),
                        }
                        for job_id, job in committed.get("jobs", {}).items()
                    }
                    if direction == "v1-to-v2":
                        report = bench.ledger.migrate_v1_to_v2(enrichment)
                    else:
                        report = bench.ledger.migrate_v2_to_v1(allow_version_regression=True)
                    self._send_json(report)
                else:
                    self._send_json({"ok": False, "code": "E_NOT_FOUND", "path": path}, 404)
            except Exception as exc:  # noqa: BLE001
                self._handle_error(exc)

        def _overview(self) -> dict[str, Any]:
            bench = Workbench(state)
            return {
                "overview": _read_optional_json(state / "overview.json"),
                "incremental": _read_optional_json(state / "incremental-report.json"),
                "byte_equivalence": _read_optional_json(state / "byte-equivalence.json"),
                "recovery": _read_optional_json(state / "recovery-report.json"),
                "budget": bench.budget.to_dict(),
                "snapshot_counter": _read_optional_json(state / "snapshots" / "counter.json"),
            }

        def _jobs(self) -> dict[str, Any]:
            committed = Workbench(state).load_committed_state()
            jobs = []
            for job_id, job in sorted(committed.get("jobs", {}).items()):
                jobs.append(
                    {
                        "job_id": job_id,
                        "revision": job.get("revision"),
                        "result_hash": job.get("result_hash"),
                        "input_hash": job.get("input_hash"),
                        "spec": job.get("spec"),
                        "nodes": {
                            name: {
                                "key": node.get("key"),
                                "payload_hash": node.get("payload_hash"),
                                "last_hit": node.get("last_hit"),
                            }
                            for name, node in job.get("nodes", {}).items()
                            if isinstance(node, dict)
                        },
                    }
                )
            return {"run_id": committed.get("run_id"), "jobs": jobs}

        def _ledgers(self) -> dict[str, Any]:
            return {
                "v1": _read_optional_json(state / "ledgers" / "v1" / "ledger-v1.json"),
                "v2": _read_optional_json(state / "ledgers" / "v2" / "ledger-v2-index.json"),
                "migration_v1_to_v2": _read_optional_json(state / "ledgers" / "migration-report-v1-to-v2.json"),
                "migration_v2_to_v1": _read_optional_json(state / "ledgers" / "migration-report-v2-to-v1.json"),
            }

        def _diagnoses(self) -> dict[str, Any]:
            diagnosis_dir = state / "diagnostics"
            reports = []
            if diagnosis_dir.exists():
                for path in sorted(diagnosis_dir.glob("*.json")):
                    data = _read_optional_json(path)
                    if isinstance(data, dict):
                        reports.append({"name": path.name, "data": data})
            return {"reports": reports}

    return DiagnosticsHandler


def serve(state_dir: str | Path, *, host: str = "127.0.0.1", port: int = 8765) -> dict[str, Any]:
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), create_handler(state))
    url = f"http://{host}:{port}/"
    print(json.dumps({"serving": url, "state_dir": str(state.resolve())}, ensure_ascii=False))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return {"exit_code": 0, "url": url}
