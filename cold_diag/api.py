from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from .policy_store import PolicyPackageManager
from .runner import run_scenario, summarize_result
from .sample_data import generate_sample
from .store import StateStore

app = FastAPI(
    title="Serverless Cold-Start Diagnostic Engine",
    version="1.0.0",
    description="Deterministic cold-start, concurrency, fixed-point ledger, and recovery diagnostics.",
)


class GenerateRequest(BaseModel):
    data_dir: str = "data"
    seed: int = 20261127


class DiagnoseRequest(BaseModel):
    data_dir: str = "data"
    db_path: str = ":memory:"


class PolicyApplyRequest(BaseModel):
    package_path: str
    db_path: str = "artifacts/policy.sqlite"
    clock_ms: int = 0


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "cold-diag"}


@app.post("/samples/generate")
def samples_generate(request: GenerateRequest) -> dict[str, str]:
    return generate_sample(request.data_dir, seed=request.seed)


@app.post("/diagnose")
def diagnose(request: DiagnoseRequest) -> dict[str, Any]:
    result = run_scenario(request.data_dir, db_path=request.db_path)
    summary = summarize_result(result)
    summary["canonical_state_sha256"] = __import__("hashlib").sha256(
        result["engine"].canonical_state_bytes()
    ).hexdigest()
    result["engine"].close()
    return summary


@app.post("/policy/apply-package")
def policy_apply_package(request: PolicyApplyRequest) -> dict[str, Any]:
    store = StateStore(request.db_path)
    try:
        manager = PolicyPackageManager(store)
        return manager.apply_package(request.package_path, clock_ms=request.clock_ms)
    finally:
        store.close()


@app.get("/policy/active-snapshot")
def policy_active_snapshot(db_path: str = "artifacts/policy.sqlite") -> dict[str, Any]:
    store = StateStore(db_path)
    try:
        pointer = store.get_active_policy_snapshot()
        if not pointer:
            return {"active": None}
        snapshot = store.get_policy_snapshot(pointer["snapshot_id"])
        return {"active": pointer, "snapshot": snapshot}
    finally:
        store.close()
