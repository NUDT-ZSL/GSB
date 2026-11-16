"""FastAPI console backend exposing engine, snapshot, diff and rollback APIs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .diff import diff_versions
from .engine import Engine
from .errors import BusinessCode, MigrationError
from .injector import AnomalyScenario, load_anomalies, run_scenario
from .models import Rule, Sample
from .store import Store

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _paths() -> tuple[Path, Path]:
    sample_dir = Path(os.environ.get("LINEAGE_SAMPLE_DIR", PROJECT_ROOT / "samples"))
    db_path = Path(os.environ.get("LINEAGE_DB_PATH", PROJECT_ROOT / "data" / "lineage.db"))
    return sample_dir, db_path

HTTP_STATUS = {
    BusinessCode.E_RULE_ORDER: 409,
    BusinessCode.E_CYCLE: 422,
    BusinessCode.E_TARGET_MISSING: 404,
    BusinessCode.E_STALE_SOURCE: 409,
    BusinessCode.E_VERSION_REGRESS: 409,
    BusinessCode.E_INTERNAL: 500,
}


class RuleIn(BaseModel):
    rule_id: str
    rule_type: str
    source_ref: str | None = None
    target_ref: str | None = None
    new_type: str | None = None
    targets: list[str] = Field(default_factory=list)
    weights: list[int] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    seq: int | None = None
    depends_on: list[str] = Field(default_factory=list)

    def to_rule(self) -> Rule:
        return Rule(
            rule_id=self.rule_id,
            rule_type=self.rule_type,
            source_ref=self.source_ref,
            target_ref=self.target_ref,
            new_type=self.new_type,
            targets=tuple(self.targets),
            weights=tuple(self.weights),
            sources=tuple(self.sources),
            seq=self.seq,
            depends_on=tuple(self.depends_on),
        )


class ApplyIn(BaseModel):
    version: int
    rules: list[RuleIn]


class RollbackIn(BaseModel):
    target_version: int


class AnomalyIn(BaseModel):
    scenario_id: str = "ad_hoc"
    description: str = "request-parameter injected anomaly"
    version: int
    expected_code: str
    rules: list[RuleIn]


class DiffQueryIn(BaseModel):
    from_version: int
    to_version: int
    table: str | None = None
    field_ref: str | None = None


def create_app() -> FastAPI:
    app = FastAPI(title="Lineage Migration Validator", version="1.0.0")
    sample_dir, db_path = _paths()
    app.state.sample_dir = sample_dir
    app.state.db_path = db_path
    app.state.sample = Sample.load(sample_dir)
    app.state.store = Store(db_path)

    def engine(self) -> Engine:  # pragma: no cover - tiny helper
        return Engine(app.state.store)

    @app.exception_handler(MigrationError)
    async def migration_error_handler(_request, exc: MigrationError):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=HTTP_STATUS.get(exc.code, 500),
            content={"error": exc.to_dict()},
        )

    @app.get("/health")
    def health() -> dict[str, Any]:
        store: Store = app.state.store
        return {
            "status": "ok",
            "db_path": str(app.state.db_path),
            "initialized": store.is_initialized(),
            "last_version": store.last_version(),
        }

    @app.post("/sample/load")
    def load_sample() -> dict:
        """Rebuild the database from sample JSON (version 0 baseline)."""
        app.state.store.close()
        eng = Engine.bootstrap(app.state.db_path, app.state.sample)
        app.state.store = eng.store
        return {
            "status": "loaded",
            "last_version": app.state.store.last_version(),
            "field_count": len(app.state.store.live_fields()),
            "edge_count": len(app.state.store.live_edges()),
        }

    @app.post("/versions/apply")
    def apply_version(payload: ApplyIn) -> dict:
        try:
            result = Engine(app.state.store).apply_version(
                payload.version, [item.to_rule() for item in payload.rules]
            )
        except MigrationError:
            raise
        return result

    @app.post("/versions/replay")
    def replay_all() -> dict:
        """Replay every sample version from an empty database."""
        app.state.store.close()
        eng = Engine.bootstrap(app.state.db_path, app.state.sample)
        results = eng.replay_all(app.state.sample)
        app.state.store = eng.store
        return {"results": results, "last_version": app.state.store.last_version()}

    @app.get("/versions")
    def list_versions() -> dict:
        return {"versions": app.state.store.versions()}

    @app.get("/snapshots/{version}")
    def get_snapshot(version: int) -> dict:
        if app.state.store.get_version(version) is None:
            raise HTTPException(status_code=404, detail=f"snapshot {version} not found")
        return {
            "version": version,
            "fields": [item.__dict__ for item in app.state.store.snapshot_fields(version)],
            "edges": [item.__dict__ for item in app.state.store.snapshot_edges(version)],
        }

    @app.post("/diff")
    def get_diff(payload: DiffQueryIn) -> dict:
        try:
            return diff_versions(
                app.state.store,
                payload.from_version,
                payload.to_version,
                table=payload.table,
                field_ref=payload.field_ref,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/rollback")
    def rollback(payload: RollbackIn) -> dict:
        try:
            app.state.store.rollback_to(payload.target_version)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {
            "status": "rolled_back",
            "last_version": app.state.store.last_version(),
            "edge_count": len(app.state.store.live_edges()),
        }

    @app.get("/changes")
    def changes(version: int | None = None) -> dict:
        return {"changes": app.state.store.change_log(version)}

    @app.get("/audit")
    def audit(version: int | None = None) -> dict:
        return {"audit_events": app.state.store.audit_events(version)}

    @app.get("/anomalies")
    def anomalies() -> dict:
        scenarios = load_anomalies(app.state.sample_dir / "anomalies.json")
        return {
            "scenarios": [
                {
                    "scenario_id": item.scenario_id,
                    "description": item.description,
                    "version": item.version,
                    "expected_code": item.expected_code,
                }
                for item in scenarios
            ]
        }

    @app.post("/anomalies/inject")
    def inject_anomaly(payload: AnomalyIn) -> dict:
        """Run one anomaly in an isolated scratch DB and report rollback state."""
        scenario = AnomalyScenario(
            scenario_id=payload.scenario_id,
            description=payload.description,
            version=payload.version,
            expected_code=payload.expected_code,
            rules=[item.to_rule() for item in payload.rules],
        )
        workspace = PROJECT_ROOT / "data" / "anomaly_runs"
        return run_scenario(scenario, app.state.sample, workspace)

    @app.post("/anomalies/run-all")
    def run_all_anomalies() -> dict:
        from .injector import run_all_scenarios

        results = run_all_scenarios(
            app.state.sample,
            app.state.sample_dir / "anomalies.json",
            app.state.db_path.parent / "anomaly_runs",
        )
        return {"results": results}

    return app


app = create_app()
