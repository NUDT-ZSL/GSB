"""Application service tying declarations, scheduling and the ledger together."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .declaration import load_declaration as parse_declaration_file
from .errors import StateError
from .ledger import Ledger
from .models import ClusterDeclaration, VersionRegistration
from .scheduler import schedule_minute


class Workbench:
    """High-level API shared by the CLI, demo and Tkinter UI."""

    def __init__(
        self,
        declaration_path: str | Path,
        database_path: str | Path,
        snapshot_dir: str | Path | None = None,
    ) -> None:
        self.declaration_path = Path(declaration_path).resolve()
        self.ledger = Ledger(database_path, snapshot_dir)
        self.declaration: ClusterDeclaration | None = None
        self.registration: VersionRegistration | None = None

    def close(self) -> None:
        self.ledger.close()

    def __enter__(self) -> "Workbench":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def load_declaration(
        self,
        path: str | Path | None = None,
    ) -> VersionRegistration:
        source = Path(path).resolve() if path is not None else self.declaration_path
        declaration = parse_declaration_file(source)
        registration = self.ledger.register_declaration(
            declaration,
            source_path=str(source),
        )
        self.declaration_path = source
        self.declaration = registration.declaration
        self.registration = registration
        return registration

    def load_stored_declaration(self, version: int | None = None) -> ClusterDeclaration:
        if version is None:
            latest = self.ledger.latest_declaration()
            if latest is None:
                raise StateError(
                    "the ledger has no declaration",
                    "$.declarations",
                )
            self.declaration = latest
            return latest
        declaration = self.ledger.load_declaration(version)
        self.declaration = declaration
        return declaration

    def _ensure_declaration(self) -> ClusterDeclaration:
        if self.declaration is None:
            self.load_declaration()
        assert self.declaration is not None
        return self.declaration

    def start_run(self) -> str:
        declaration = self._ensure_declaration()
        return self.ledger.start_run(declaration)

    def get_run(self, run_id: str | None = None) -> dict[str, Any]:
        if run_id is None:
            run = self.ledger.get_current_run()
            if run is None:
                raise StateError("there is no current run", "$.runs")
            return run
        run = self.ledger.get_run(run_id)
        if run is None:
            raise StateError("run does not exist", "$.run_id", {"run_id": run_id})
        return run

    def advance(
        self,
        run_id: str,
        *,
        limit_minutes: int | None = None,
        require_current: bool = True,
    ) -> dict[str, Any]:
        run = self.get_run(run_id)
        if require_current and (run["is_stale"] or not run["is_current"]):
            raise StateError(
                "stale or non-current runs cannot be advanced",
                f"$.runs[{run_id}]",
                {"is_stale": bool(run["is_stale"]), "is_current": bool(run["is_current"])},
            )
        declaration = self.ledger.load_declaration(run["declaration_version"])
        total = int(run["total_minutes"])
        next_minute = int(run["completed_minutes"])
        if limit_minutes is not None:
            if type(limit_minutes) is not int or limit_minutes < 0:
                raise StateError(
                    "limit_minutes must be a non-negative integer",
                    "$.limit_minutes",
                )
            stop = min(total, next_minute + limit_minutes)
        else:
            stop = total

        processed = 0
        while next_minute < stop:
            self.ledger.begin_minute(run_id, next_minute)
            decision = schedule_minute(declaration, next_minute)
            self.ledger.commit_minute(run_id, decision)
            next_minute += 1
            processed += 1
        final_run = self.get_run(run_id)
        return {
            "ok": True,
            "run_id": run_id,
            "declaration_version": final_run["declaration_version"],
            "minutes_processed": processed,
            "completed_minutes": final_run["completed_minutes"],
            "total_minutes": final_run["total_minutes"],
            "status": final_run["status"],
            "in_flight_minute": final_run["in_flight_minute"],
            "is_current": bool(final_run["is_current"]),
            "is_stale": bool(final_run["is_stale"]),
        }

    def run_all(
        self,
        *,
        limit_minutes: int | None = None,
        new_run: bool = True,
    ) -> dict[str, Any]:
        if new_run:
            run_id = self.start_run()
        else:
            run_id = self.get_run()["run_id"]
        result = self.advance(run_id, limit_minutes=limit_minutes)
        result["view"] = self.get_current_view()
        return result

    def recover(self, run_id: str | None = None) -> dict[str, Any]:
        run = self.get_run(run_id)
        return self.ledger.recover_run(run["run_id"])

    def resume(self, run_id: str | None = None) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run["is_stale"]:
            raise StateError(
                "stale runs cannot be resumed",
                f"$.runs[{run['run_id']}]",
            )
        recovery = self.ledger.recover_run(run["run_id"])
        replay = self.ledger.replay_run(run["run_id"])
        if recovery["requires_rerun"]:
            advance = self.advance(
                run["run_id"],
                limit_minutes=None,
                require_current=False,
            )
        else:
            advance = {
                "ok": True,
                "run_id": run["run_id"],
                "minutes_processed": 0,
                "completed_minutes": recovery["recovered_minutes"],
                "total_minutes": recovery["total_minutes"],
                "status": "completed",
            }
        replay_after = self.ledger.replay_run(run["run_id"])
        return {
            "ok": True,
            "code": None,
            "run_id": run["run_id"],
            "recovery": recovery,
            "replay_before_rerun": replay,
            "advance": advance,
            "replay_after_rerun": replay_after,
        }

    def recover_and_resume(self, run_id: str | None = None) -> dict[str, Any]:
        return self.resume(run_id)

    def replay(
        self,
        run_id: str | None = None,
        *,
        supplied_declaration: ClusterDeclaration | None = None,
    ) -> dict[str, Any]:
        run = self.get_run(run_id)
        return self.ledger.replay_run(
            run["run_id"],
            supplied_declaration=supplied_declaration,
        )

    def save_snapshot(self, run_id: str | None = None) -> dict[str, Any]:
        run = self.get_run(run_id)
        return self.ledger.save_snapshot(run["run_id"])

    def restore_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        result = self.ledger.restore_snapshot(snapshot_id)
        self.declaration = self.ledger.load_declaration(result["declaration_version"])
        return result

    def list_snapshots(self) -> list[dict[str, Any]]:
        return self.ledger.list_snapshots()

    def get_current_view(self) -> dict[str, Any]:
        view = self.ledger.get_current_view()
        if self.declaration is not None:
            view["loaded_declaration_version"] = self.declaration.declaration_version
        else:
            view["loaded_declaration_version"] = None
        if self.registration is not None:
            view["registration"] = self.registration.to_dict()
        return view
