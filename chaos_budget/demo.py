from __future__ import annotations

from typing import Any, Dict, List

from .engine import Engine
from .reporting import build_database_status, verify_database
from .storage import Database


def execute_manifest_workflow(engine: Engine, bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    manifest_steps = bundle.get("manifest", {}).get("workflow", [])

    for step in manifest_steps:
        command = step["command"]
        if command == "load":
            steps.append({"command": command, "ok": True, "mode": "already_initialized"})
        elif command == "run":
            result = engine.run_scheduling(
                int(step["clock_ms"]),
                experiment_id=step.get("experiment_id"),
                round_id=step.get("round_id"),
                allow_recovered=bool(step.get("allow_recovered", False)),
            )
            steps.append({"command": command, "clock_ms": step["clock_ms"], "result": result})
        elif command == "observe":
            clock_ms = step.get("clock_ms")
            events = bundle["observations"]
            if clock_ms is not None:
                events = [event for event in events if event["clock_ms"] <= int(clock_ms)]
            results = [engine.submit_observation(event) for event in events]
            steps.append(
                {
                    "command": command,
                    "clock_ms": clock_ms,
                    "submitted_or_replayed": len(results),
                    "last_results": results[-3:],
                }
            )
        elif command == "resume":
            result = engine.resume(
                int(step.get("clock_ms", engine.db.watermark())),
                experiment_id=step.get("experiment_id"),
            )
            steps.append({"command": command, "result": result})
        elif command == "verify":
            report = verify_database(engine.db, bundle["fingerprint"])
            steps.append({"command": command, "ok": report["ok"]})
        else:
            steps.append({"command": command, "ok": False, "error": "unknown workflow command"})
    return steps


def run_manifest_workflow(bundle: Dict[str, Any], *, db_path: str = ":memory:") -> Dict[str, Any]:
    db = Database.initialize(db_path, bundle, reset=True)
    engine = Engine(db, bundle)
    steps = execute_manifest_workflow(engine, bundle)

    report = verify_database(db, bundle["fingerprint"])
    status = build_database_status(db)
    return {"db": db, "steps": steps, "verify": report, "status": status}
