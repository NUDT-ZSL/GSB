"""End-to-end deterministic demo for Windows local execution.

Runs every acceptance scenario against fresh work directories and writes a
machine-readable report plus human summary under reports/.
"""

from __future__ import annotations

import json
import shutil
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.cli import build_parser  # noqa: E402
from gateway.injection import (  # noqa: E402
    inject_corrupt_policy,
    inject_token_version_conflict,
    inject_zero_window,
    restore,
    validate_injected_inputs,
)
from gateway.ledger import Ledger  # noqa: E402
from gateway.runner import evolve_policy, recover, run_file  # noqa: E402
from gateway.server import run_server  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
WORK = ROOT / "workdir"
REPORTS = ROOT / "reports"


def reset(name: str) -> Path:
    target = WORK / name
    shutil.rmtree(target, ignore_errors=True)
    return target


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    REPORTS.mkdir(exist_ok=True)
    report: dict = {"steps": []}

    # 1. cold-start file processing for every sample, with replay verification
    section("1. cold-start processing + replay")
    for sample in ("basic", "boundary", "signature", "large", "bursty"):
        workdir = reset(sample)
        result = run_file(
            SAMPLES / sample / "routes.json",
            SAMPLES / sample / "tokens.json",
            SAMPLES / sample / "requests.ndjson",
            workdir,
            batch_size=100 if sample in ("large", "bursty") else 4,
            replay_after=True,
        )
        step = {
            "sample": sample,
            "decision_counts": result.engine.decision_codes(),
            "consistency": result.consistency,
            "selection": [
                {"route": item["route"], "selected_path": item["selected_path"],
                 "tie_broken": item["tie_broken"]}
                for item in result.selection
            ],
            "replay": result.recovery["replay"],
        }
        report["steps"].append(step)
        print(sample, step["decision_counts"],
              "consistent=", step["consistency"]["consistent"],
              "divergences=", step["consistency"]["divergence_count"])

    # 2. loopback TCP server + mock client
    section("2. loopback TCP JSON-lines server")
    tcp_workdir = reset("tcp")
    port = _free_port()
    server = run_server(
        port, tcp_workdir,
        SAMPLES / "basic" / "routes.json",
        SAMPLES / "basic" / "tokens.json",
    )
    time.sleep(0.3)
    try:
        from tools.mock_client import replay

        responses = replay("127.0.0.1", port, SAMPLES / "basic" / "requests.ndjson")
    finally:
        server.shutdown()
        server.server_close()
    tcp_counts: dict[str, int] = {}
    for item in responses:
        tcp_counts[item["code"]] = tcp_counts.get(item["code"], 0) + 1
    report["steps"].append({"sample": "tcp", "port": port, "decision_counts": tcp_counts})
    print("tcp", tcp_counts)

    # 3. forced termination: leave an open (never committed) batch
    section("3. forced termination + recovery")
    crash_workdir = reset("crash")
    run_file(
        SAMPLES / "basic" / "routes.json",
        SAMPLES / "basic" / "tokens.json",
        SAMPLES / "basic" / "requests.ndjson",
        crash_workdir,
        batch_size=5,
    )
    ledger = Ledger(crash_workdir / "ledger.db")
    orphan = ledger.begin_batch((1, 1))  # process killed before commit
    ledger.close()
    recovery = recover(crash_workdir, replay=True)
    report["steps"].append({"sample": "crash_recovery", **recovery})
    print("unknown batches:", recovery["unknown_batches"],
          "complete:", recovery["complete_batches"],
          "replay:", recovery["replay"]["status"])
    assert recovery["unknown_batches"] == [orphan]

    # continue accepting new requests after recovery
    continued = run_file(
        SAMPLES / "basic" / "routes.json",
        SAMPLES / "basic" / "tokens.json",
        SAMPLES / "basic" / "requests.ndjson",
        crash_workdir,
        batch_size=5,
        replay_after=True,
    )
    print("post-recovery replay:", continued.recovery["replay"]["status"])

    # 4. policy evolution with wholesale state replacement
    section("4. policy version evolution")
    evolve_workdir = reset("evolve")
    run_file(
        SAMPLES / "basic" / "routes.json",
        SAMPLES / "basic" / "tokens.json",
        SAMPLES / "basic" / "requests.ndjson",
        evolve_workdir,
        batch_size=4,
    )
    routes_v2 = evolve_workdir / "routes_v2.json"
    tokens_v2 = evolve_workdir / "tokens_v2.json"
    routes_data = json.loads((SAMPLES / "basic" / "routes.json").read_text(encoding="utf-8"))
    tokens_data = json.loads((SAMPLES / "basic" / "tokens.json").read_text(encoding="utf-8"))
    routes_data["version"] = 2
    tokens_data["version"] = 2
    routes_v2.write_text(json.dumps(routes_data), encoding="utf-8")
    tokens_v2.write_text(json.dumps(tokens_data), encoding="utf-8")
    evolution = evolve_policy(
        evolve_workdir, routes_v2, tokens_v2,
        SAMPLES / "basic" / "requests.ndjson", batch_size=4,
    )
    report["steps"].append({"sample": "evolution", **evolution})
    print("replacement:", evolution["replacement"])

    # 5. fault injections -> stable business codes
    section("5. fault injections")
    injection_root = reset("injections")
    injection_root.mkdir(parents=True, exist_ok=True)
    backup_dir = injection_root / "backups"
    ledger = Ledger(injection_root / "ledger.db")
    injection_results = []
    scenarios = (
        ("corrupt_policy", "routes.json", inject_corrupt_policy),
        ("token_version_conflict", "tokens.json", inject_token_version_conflict),
        ("zero_window", "routes.json", inject_zero_window),
    )
    for kind, file_name, injector in scenarios:
        routes_path = injection_root / "routes.json"
        token_path = injection_root / "tokens.json"
        shutil.copy2(SAMPLES / "basic" / "routes.json", routes_path)
        shutil.copy2(SAMPLES / "basic" / "tokens.json", token_path)
        injector(routes_path if file_name == "routes.json" else token_path, backup_dir)
        error = validate_injected_inputs(kind, routes_path, token_path, backup_dir)
        ledger.log_event(error.code, f"injection_{kind}",
                         {"message": error.message, "file": error.file})
        injection_results.append({"kind": kind, "code": error.code,
                                  "message": error.message})
        backup = backup_dir / file_name
        restore(backup, routes_path if file_name == "routes.json" else token_path)
    ledger.close()
    report["steps"].append({"sample": "injections", "results": injection_results})
    for item in injection_results:
        print(item["kind"], "->", item["code"])

    # 6. E_SPEC validation samples
    section("6. invalid spec rejection")
    spec_results = []
    for bad_file, loader_name in (
        (SAMPLES / "invalid" / "routes_duplicate.json", "routes"),
        (SAMPLES / "invalid" / "routes_bad_window.json", "routes"),
    ):
        from gateway.loader import load_routes

        try:
            load_routes(bad_file)
        except Exception as exc:  # BusinessError
            spec_results.append({"file": bad_file.name, "code": exc.code,
                                 "line": exc.line, "message": exc.message})
    from gateway.loader import load_requests

    try:
        load_requests(SAMPLES / "invalid" / "requests_non_monotonic.ndjson")
    except Exception as exc:
        spec_results.append({"file": "requests_non_monotonic.ndjson",
                             "code": exc.code, "line": exc.line,
                             "message": exc.message})
    report["steps"].append({"sample": "invalid_spec", "results": spec_results})
    for item in spec_results:
        print(item["file"], "->", item["code"], "line", item["line"])

    out = REPORTS / "demo_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nreport written: {out}")
    return 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    raise SystemExit(main())
