"""Control plane: patch submission service + FastAPI application.

The ControlPlane class is transport-independent (used by the demo and tests);
create_app() exposes it over HTTP for interactive use.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .clock import LogicalClock
from .dataplane import DataPlane
from .errors import BusinessError
from .invalidation import apply_patch, compute_invalidation
from .models import validate_all
from .store import RuleStore


class ControlPlane:
    def __init__(self, store, clock=None, dataplane=None):
        self.store = store
        self.clock = clock or LogicalClock()
        self.dataplane = dataplane

    def submit_patch(self, patch):
        """Validate, diff and atomically commit a patch.

        Returns {"version", "applied", "invalidation"}. On any validation
        error raises BusinessError and nothing is persisted.
        """
        current = self.store.load_snapshot()
        new_objects = apply_patch(current.objects, patch)
        validate_all(new_objects)
        new_version = current.version + 1
        event = compute_invalidation(current.objects, new_objects,
                                     patch["patch_id"], new_version)
        version, applied = self.store.commit_patch(
            patch_id=patch["patch_id"],
            expected_version=patch.get("expected_version"),
            new_objects=new_objects,
            invalidation_events=[event],
            created_ms=self.clock.now_ms(),
        )
        if not applied:
            stored = self.store.latest_invalidation(version)
            event = stored[-1] if stored else event
        elif self.dataplane is not None:
            self.dataplane.apply_invalidation(event)
        return {"version": version, "applied": applied, "invalidation": event}


def create_app(db_path, clock=None, timeout_ms=500):
    store = RuleStore(db_path)
    clock = clock or LogicalClock()
    dataplane = DataPlane(store, clock, timeout_ms=timeout_ms)
    control = ControlPlane(store, clock, dataplane)

    app = FastAPI(title="graymesh", version="0.1.0")
    app.state.store = store
    app.state.dataplane = dataplane
    app.state.control = control

    @app.exception_handler(BusinessError)
    async def business_error_handler(_request: Request, exc: BusinessError):
        return JSONResponse(status_code=422, content={"error": exc.to_dict()})

    @app.get("/health")
    def health():
        return {"status": "ok", "version": store.current_version()}

    @app.post("/patches")
    def submit_patch(patch: dict):
        return control.submit_patch(patch)

    @app.get("/rules/current")
    def rules_current():
        snap = store.load_snapshot()
        return {"version": snap.version, "digest": snap.digest,
                "objects": [obj for _, obj in sorted(snap.objects.items())]}

    @app.get("/rules/versions/{version}")
    def rules_version(version: int):
        snap = store.load_snapshot(version)
        return {"version": snap.version, "digest": snap.digest,
                "objects": [obj for _, obj in sorted(snap.objects.items())]}

    @app.get("/invalidations")
    def invalidations(version: int = None):
        return {"events": store.latest_invalidation(version)}

    @app.post("/requests")
    def handle_request(request: dict):
        return dataplane.handle(request)

    @app.get("/decisions")
    def decisions():
        return {"decisions": store.list_decisions()}

    @app.get("/mirrors")
    def mirrors():
        dataplane.flush_mirrors()
        return {"mirrors": store.list_mirrors()}

    @app.on_event("shutdown")
    def shutdown():
        dataplane.close()

    return app
