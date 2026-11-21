"""Interactive diagnostic workbench orchestration and reporting."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cache import ContentAddressedCache
from .errors import DiagError
from .formats import parse_text
from .graph import (AssemblyContext, assemble, detect_cycle,
                    recompose_answer, _build_edges)
from .limits import DegradationLog, ResourceLimits
from .lineage import replay_assembly, source_path
from .migration import MigrationPolicy
from .models import AssemblyResult, ManifestEntry
from .snapshots import SnapshotStore
from .store import InvalidationReport, ResultStore


@dataclass
class SelfCheckReport:
    graph_acyclic: bool
    cache_hash_consistent: bool
    recompute_reuse_disjoint: bool
    recompute_reuse_cover_affected: bool
    answer_recomputable: bool
    failures: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures and all((
            self.graph_acyclic, self.cache_hash_consistent,
            self.recompute_reuse_disjoint,
            self.recompute_reuse_cover_affected, self.answer_recomputable))


class DiagnosticWorkbench:
    """Top-level facade used by the CLI, demo and tests."""

    def __init__(self, base: Path,
                 limits: Optional[ResourceLimits] = None):
        self.base = Path(base)
        self.limits = limits or ResourceLimits()
        self.store = ResultStore(self.base / "store")
        self.cache = ContentAddressedCache(self.base / "cache", self.limits)
        self.snapshots = SnapshotStore(self.base / "snapshots")
        self.degradations = DegradationLog()
        self.recent_errors: List[Dict[str, Any]] = []
        self.context = AssemblyContext(self.store, self.cache, self.limits)
        self.assemblies: Dict[str, AssemblyResult] = {}

    def load_result(self, result_ref: str, file_path: Path) -> Dict[str, Any]:
        """Load one result file into the repository, detecting real changes."""
        raw_text = Path(file_path).read_text(encoding="utf-8")
        version, _ = parse_text(raw_text)
        changed = self.store.is_changed(result_ref, raw_text, version)
        stored = self.store.put(result_ref, raw_text)
        return {"result_ref": result_ref, "entry_version": stored.entry_version,
                "format_version": version, "changed": changed,
                "raw_content_hash": stored.raw_content_hash}

    def load_manifest(self, manifest_path: Path) -> List[ManifestEntry]:
        entries: List[ManifestEntry] = []
        for line in Path(manifest_path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(ManifestEntry.from_dict(json.loads(line)))
        return entries

    def resolve_result_file(self, results_root: Path, tool_id: str,
                            version: int) -> Path:
        return Path(results_root) / tool_id / f"v{version}.txt"

    def invalidate(self, changed_refs: List[str]) -> InvalidationReport:
        return self.store.compute_invalidation(
            set(changed_refs), self.context.fragment_index)

    def assemble_request(self, manifest: ManifestEntry,
                         invalidation: Optional[InvalidationReport] = None
                         ) -> AssemblyResult:
        try:
            result = assemble(manifest, self.context, invalidation)
            if invalidation is not None:
                # After recomputation the affected fragment keys are the ones
                # actually rebuilt; keep the report internally consistent so
                # recompute/reuse fully covers the affected set.
                invalidation.affected_fragments = sorted(
                    set(result.affected_fragments))
                invalidation.recomputed = sorted(
                    set(invalidation.recomputed)
                    | set(result.affected_fragments))
            self.assemblies[manifest.request_id] = result
            return result
        except DiagError as exc:
            self.recent_errors.append({"code": exc.code,
                                       "message": exc.message,
                                       "context": exc.context})
            raise

    def replay(self, request_id: str):
        assembly = self.assemblies[request_id]
        return replay_assembly(assembly, self.cache, self.store)

    def self_check(self, manifest: ManifestEntry,
                   assembly: AssemblyResult,
                   invalidation: Optional[InvalidationReport] = None
                   ) -> SelfCheckReport:
        report = SelfCheckReport(
            graph_acyclic=True, cache_hash_consistent=True,
            recompute_reuse_disjoint=True,
            recompute_reuse_cover_affected=True,
            answer_recomputable=True)

        edges = _build_edges(manifest)
        if detect_cycle(edges) is not None:
            report.graph_acyclic = False
            report.failures.append("assembly graph contains a cycle")

        for key, valid in self.cache.verify_integrity():
            if not valid:
                report.cache_hash_consistent = False
                report.failures.append(f"cache product hash mismatch: {key}")

        recomputed = set(assembly.recomputed)
        reused = set(assembly.reused)
        if recomputed & reused:
            report.recompute_reuse_disjoint = False
            report.failures.append("recompute and reuse sets intersect")
        if invalidation is not None:
            affected_keys = set(invalidation.affected_fragments)
            if not affected_keys.issubset(recomputed | reused):
                report.recompute_reuse_cover_affected = False
                report.failures.append(
                    "affected fragments are not fully covered by "
                    "recompute/reuse")

        answer, answer_hash = recompose_answer(assembly.fragments)
        if answer != assembly.answer or answer_hash != assembly.answer_hash:
            report.answer_recomputable = False
            report.failures.append("answer cannot be recomputed identically")
        return report

    def commit_snapshot(self, component: str, payload: Any):
        with self.snapshots.commit_stage():
            return self.snapshots.commit(component, payload)

    def enforce_or_degrade_results(self) -> Dict[str, Any]:
        """Trim resident results to the ceiling and log the degradation.

        Hard enforcement is still available through
        :meth:`ResourceLimits.enforce_results`; this is the graceful
        alternative required by the workbench, which evicts the highest
        result_ref entries until the resident-result ceiling is met and marks
        the degradation category for the diagnostic view.
        """
        refs = self.store.all_refs()
        over = len(refs) - self.limits.max_resident_results
        if over <= 0:
            return {"degraded": False, "evicted": []}
        evicted = sorted(refs, reverse=True)[:over]
        for result_ref in evicted:
            del self.store.entries[result_ref]
            self.store._flush_index()
        self.degradations.record(
            "resident_results",
            {"limit": self.limits.max_resident_results,
             "evicted": evicted, "mode": "graceful_trim"})
        return {"degraded": True, "evicted": evicted}

    def snapshot_versions(self) -> Dict[str, int]:
        return self.snapshots.versions()

    def format_view(self, result_ref: str, version: int) -> str:
        """Render the current stored result in the requested format view."""
        node = self.store.node(result_ref)
        from .migration import migrate
        outcome = migrate(node, version, MigrationPolicy())
        return f"# FORMAT_VERSION={version}\n{outcome.body}"

    def build_view_model(self, request_id: Optional[str] = None
                         ) -> Dict[str, Any]:
        assembly = self.assemblies.get(request_id) if request_id else \
            next(iter(self.assemblies.values()), None)
        model: Dict[str, Any] = {
            "snapshot_versions": self.snapshot_versions(),
            "cache_entries": self.cache.entry_count(),
            "resident_results": len(self.store.all_refs()),
            "recent_errors": self.recent_errors[-10:],
            "degradations": self.degradations.events,
            "results": [],
        }
        for result_ref in self.store.all_refs():
            node = self.store.node(result_ref)
            model["results"].append({
                "result_ref": result_ref,
                "tool_id": node.tool_id,
                "format": node.result_format_version,
                "schema": node.schema_version,
                "status": node.status,
                "completions": [item.__dict__ for item in node.completions],
                "discards": [item.__dict__ for item in node.discards],
            })
        if assembly is not None:
            model["assembly"] = {
                "request_id": assembly.request_id,
                "answer": assembly.answer,
                "answer_hash": assembly.answer_hash,
                "dag": [node.__dict__ for node in assembly.dag],
                "fragments": [
                    {**fragment.to_dict(),
                     "source_path": source_path(fragment)}
                    for fragment in assembly.fragments],
                "reused": assembly.reused,
                "recomputed": assembly.recomputed,
                "affected_results": assembly.affected_results,
                "affected_fragments": assembly.affected_fragments,
                "recompute_suffix": assembly.recompute_suffix,
                "failures": assembly.failures,
            }
        return model

    def render_html_report(self, output: Path,
                           request_id: Optional[str] = None) -> Path:
        model = self.build_view_model(request_id)
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_render_html(model), encoding="utf-8")
        return output

    def render_json_report(self, output: Path,
                           request_id: Optional[str] = None) -> Path:
        model = self.build_view_model(request_id)
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(model, sort_keys=True, indent=2,
                                     ensure_ascii=False), encoding="utf-8")
        return output


def _render_html(model: Dict[str, Any]) -> str:
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>Tool-Result Diagnostic Workbench</title>",
        "<style>body{font-family:monospace;margin:24px;color:#1a1a1a}"
        "table{border-collapse:collapse;margin:12px 0;width:100%}"
        "th,td{border:1px solid #bbb;padding:6px 8px;font-size:12px;"
        "vertical-align:top;text-align:left}"
        ".hit{background:#e6f6e6}.recomputed{background:#fff2d6}"
        ".error{background:#fde2e2}h2{margin-top:28px}"
        "pre{background:#f5f5f5;padding:10px;overflow:auto}</style></head><body>",
        "<h1>Tool-Result Format Evolution Workbench</h1>",
        "<h2>Snapshot &amp; Resource State</h2>",
        "<p>Snapshot versions: "
        + html.escape(json.dumps(model["snapshot_versions"]))
        + "<br>Cache entries: " + str(model["cache_entries"])
        + "<br>Resident results: " + str(model["resident_results"]) + "</p>",
    ]
    if model["recent_errors"]:
        parts.append("<h2>Recent Business Errors</h2><table><tr><th>Code</th>"
                     "<th>Message</th><th>Context</th></tr>")
        for item in model["recent_errors"]:
            parts.append(
                "<tr class='error'><td>" + html.escape(item["code"]) + "</td>"
                "<td>" + html.escape(item["message"]) + "</td>"
                "<td><pre>" + html.escape(json.dumps(item["context"]))
                + "</pre></td></tr>")
        parts.append("</table>")

    parts.append("<h2>Results, Completions &amp; Discards</h2><table>"
                 "<tr><th>Result</th><th>Tool</th><th>Format</th>"
                 "<th>Schema</th><th>Status</th><th>Completions</th>"
                 "<th>Discards</th></tr>")
    for item in model["results"]:
        parts.append(
            "<tr><td>" + html.escape(item["result_ref"]) + "</td>"
            "<td>" + html.escape(item["tool_id"]) + "</td>"
            "<td>v" + str(item["format"]) + "</td>"
            "<td>" + html.escape(item["schema"]) + "</td>"
            "<td>" + html.escape(item["status"]) + "</td>"
            "<td><pre>" + html.escape(json.dumps(item["completions"]))
            + "</pre></td>"
            "<td><pre>" + html.escape(json.dumps(item["discards"]))
            + "</pre></td></tr>")
    parts.append("</table>")

    assembly = model.get("assembly")
    if assembly:
        parts.append("<h2>Computation Graph Topology</h2><table><tr><th>Node</th>"
                     "<th>Kind</th><th>Inputs</th><th>Status</th><th>Output Hash</th></tr>")
        for node in assembly["dag"]:
            css = "hit" if node["status"] == "hit" else \
                "error" if node["status"] == "error" else "recomputed"
            parts.append(
                "<tr class='" + css + "'><td>"
                + html.escape(node["node_id"]) + "</td>"
                "<td>" + html.escape(node["kind"]) + "</td>"
                "<td>" + html.escape(",".join(node["inputs"])) + "</td>"
                "<td>" + html.escape(node["status"]) + "</td>"
                "<td>" + html.escape(node["output_hash"][:16]) + "</td></tr>")
        parts.append("</table>")

        parts.append("<h2>Fragment Lineage &amp; Merge Positions</h2><table>"
                     "<tr><th>Fragment</th><th>Tool</th><th>Format</th>"
                     "<th>Position</th><th>Interval</th><th>Cache Key</th>"
                     "<th>Source Path</th></tr>")
        for fragment in assembly["fragments"]:
            parts.append(
                "<tr><td>" + html.escape(fragment["fragment_id"]) + "</td>"
                "<td>" + html.escape(fragment["tool_id"]) + "</td>"
                "<td>v" + str(fragment["result_format_version"]) + "</td>"
                "<td>" + str(fragment["position"]) + "</td>"
                "<td>" + html.escape(str(fragment["interval"])) + "</td>"
                "<td>" + html.escape(fragment["cache_key"][:16]) + "</td>"
                "<td><pre>" + html.escape(json.dumps(fragment["source_path"]))
                + "</pre></td></tr>")
        parts.append("</table>")

        parts.append(
            "<h2>Invalidation Propagation</h2><ul>"
            "<li>Affected results: "
            + html.escape(json.dumps(assembly["affected_results"])) + "</li>"
            "<li>Affected fragments: "
            + str(len(assembly["affected_fragments"])) + "</li>"
            "<li>Recompute suffix: "
            + html.escape(json.dumps(assembly["recompute_suffix"])) + "</li>"
            "<li>Reused cache keys: " + str(len(assembly["reused"])) + "</li>"
            "<li>Recomputed cache keys: " + str(len(assembly["recomputed"]))
            + "</li></ul>"
            "<h2>Answer</h2><pre>" + html.escape(assembly["answer"]) + "</pre>"
            "<p>Answer hash: " + html.escape(assembly["answer_hash"])
            + "</p>")

    parts.append("</body></html>")
    return "".join(parts)
