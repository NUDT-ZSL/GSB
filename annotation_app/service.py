"""Application service shared by the CLI and local web UI."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .config import ConfigStore
from .demo import run_demo
from .editing import Editor
from .errors import AppError
from .lineformat import export_line
from .migration import downgrade_v2_to_v1
from .model import V1Sample, V2Sample, graph_snapshot
from .ontology import OntologySnapshot, OntologyStore
from .reader import read_file
from .samplegen import write_assets
from .storage import read_json


class WorkbenchService:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or Path.cwd()).resolve()
        if not (self.root / "samples" / "sample-v2.json").exists():
            write_assets(self.root)
        self.config_store = ConfigStore(self.root / "cache" / "ui-config.json")
        self.config = self.config_store.load(self.root / "snapshots" / "config-v1.json")
        self.ontology_store = OntologyStore(
            active_path=self.root / "cache" / "ui-ontology.json",
            contract_mode=self.config.ontology_contract_mode,
        )
        self.ontology = self.ontology_store.load(
            self.root / "snapshots" / "ontology-v1.json"
        ).snapshot
        self._load_base()
        self.last_demo: dict[str, Any] | None = None
        report_path = self.root / "cache" / "demo-report.json"
        if report_path.exists():
            try:
                self.last_demo = read_json(report_path)
            except (OSError, ValueError):
                self.last_demo = None

    def _load_base(self) -> None:
        loaded = read_file(
            self.root / "samples" / "sample-v2.json",
            ontology=self.ontology,
            max_import_bytes=self.config.limits.max_import_bytes,
        )
        if not isinstance(loaded, V2Sample):
            raise AppError("E_INCONSISTENT", "UI base sample must be v2")
        self.base_sample = loaded
        self.editor = Editor(
            self.base_sample,
            self.ontology,
            limits=self.config.limits,
        )

    def reset(self) -> None:
        self._load_base()

    def apply_edit(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            self.editor.edit(operation, payload)
        except AppError as exc:
            return {
                "status": "rejected",
                "error": exc.to_dict(),
                "state": self.state(),
            }
        return {
            "status": "applied",
            "audit_entry": self.editor.ledger.entries[-1],
            "state": self.state(),
        }

    def trigger_demo(self) -> dict[str, Any]:
        self.last_demo = run_demo(self.root)
        return {
            "status": self.last_demo.get("status"),
            "demo": self.last_demo,
            "state": self.state(),
        }

    def view_content(self, view: str) -> tuple[Any, str | None]:
        if view == "v2":
            return self.editor.sample.to_dict(), None
        if view == "v1":
            result = downgrade_v2_to_v1(self.editor.sample, self.ontology)
            assert isinstance(result.sample, V1Sample)
            return result.sample.to_dict(), None
        if view == "line":
            return export_line(self.editor.sample, ontology=self.ontology), None
        raise AppError(
            "E_FORMAT_INVALID",
            f"unsupported view {view!r}",
            path="$.view",
            details={"allowed": ["v1", "v2", "line"]},
        )

    def state(self, view: str = "v2") -> dict[str, Any]:
        content, warning = self.view_content(view)
        sample = self.editor.sample
        rejected = [
            entry["error"]
            for entry in self.editor.ledger.entries
            if entry["status"] == "rejected"
        ]
        demo_errors = (
            copy.deepcopy(self.last_demo.get("latest_errors", []))
            if self.last_demo
            else []
        )
        combined_errors = (rejected + demo_errors)[-5:]
        migration = None
        if self.last_demo:
            migration = {
                "status": self.last_demo.get("status"),
                "report_checksum": self.last_demo.get("report_checksum"),
                "v1_path": self.last_demo.get("migration_roundtrip", {}).get(
                    "v1_path", {}
                ).get("status"),
                "v2_path": self.last_demo.get("migration_roundtrip", {}).get(
                    "v2_path", {}
                ).get("status"),
                "v2_differences": self.last_demo.get("migration_roundtrip", {}).get(
                    "v2_path", {}
                ).get("difference_count"),
                "v2_dropped": self.last_demo.get("migration_roundtrip", {}).get(
                    "v2_path", {}
                ).get("dropped_count"),
                "v2_conflicts": self.last_demo.get("migration_roundtrip", {}).get(
                    "v2_path", {}
                ).get("conflict_count"),
                "ontology_contraction": {
                    "strict_blocked": self.last_demo.get(
                        "ontology_contraction", {}
                    ).get("strict", {}).get("blocked"),
                    "strict_code": self.last_demo.get(
                        "ontology_contraction", {}
                    ).get("strict", {}).get("error", {}).get("code"),
                    "lenient_affected": self.last_demo.get(
                        "ontology_contraction", {}
                    ).get("lenient", {}).get("affected", []),
                },
            }
        return {
            "application": "EventGraph Annotation Studio",
            "sample_id": sample.sample_id,
            "current_format": "v2",
            "active_view": view,
            "view_content": content,
            "view_warning": warning,
            "ontology": {
                "version": self.ontology.version,
                "checksum": self.ontology.checksum_value,
                "components": {
                    name: {
                        "version": component.version,
                        "checksum": component.checksum_value,
                    }
                    for name, component in self.ontology.components.items()
                },
                "entity_types": sorted(self.ontology.entity_types),
                "argument_roles": sorted(self.ontology.argument_roles),
                "relation_types": sorted(self.ontology.relation_types),
            },
            "config": {
                "version": self.config.version,
                "checksum": self.config.checksum_value,
                "limits": self.config.limits.to_dict(),
                "ontology_contract_mode": self.config.ontology_contract_mode,
            },
            "event_graph": graph_snapshot(sample),
            "graph_view": {
                "nodes": [
                    {
                        **node.to_dict(),
                        "doc_id": document.doc_id,
                    }
                    for document in sample.documents
                    for node in document.nodes
                ],
                "edges": [
                    {
                        **edge.to_dict(),
                        "doc_id": document.doc_id,
                    }
                    for document in sample.documents
                    for edge in document.edges
                ],
            },
            "graph_stats": {
                "nodes": sum(len(document.nodes) for document in sample.documents),
                "edges": sum(len(document.edges) for document in sample.documents),
                "events": len(
                    {
                        node.event_id
                        for _, node in sample.iter_nodes()
                        if node.kind == "trigger"
                    }
                ),
            },
            "audit": self.editor.ledger.entries,
            "recent_errors": combined_errors,
            "migration_report": migration,
            "recent_business_error_codes": [
                row.get("code") for row in combined_errors
            ],
            "views": {
                "v1": "flat JSON",
                "v2": "unified event graph",
                "line": "line-oriented exchange",
            },
        }

    def state_json(self, view: str = "v2") -> str:
        return json.dumps(
            self.state(view),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

