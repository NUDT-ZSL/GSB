"""Interactive local diagnostic workbench (stdin-driven, no network)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .errors import DiagError
from .migration import MigrationPolicy, migrate
from .samples import SAMPLE_SEED, generate_workspace
from .workbench import DiagnosticWorkbench

MENU = """
================ Diagnostic Workbench ================
 1) regenerate deterministic sample workspace
 2) load repository results from the generated samples
 3) run assembly for every request
 4) show computation graph + hit/recompute status
 5) show invalidation propagation and self-check
 6) switch a result's format view (v1/v2/v3)
 7) show migration completions and discards
 8) show snapshot versions and recent business errors
 9) write HTML and JSON diagnostic reports
 0) exit
"""


class InteractiveSession:
    def __init__(self, base: Path):
        self.base = Path(base)
        self.workbench = DiagnosticWorkbench(self.base / "runtime")
        self.generated: dict | None = None
        self.manifests = []

    def generate(self) -> None:
        self.generated = generate_workspace(self.base / "data")
        print(f"[ok] seed={SAMPLE_SEED} generated "
              f"{self.generated['logical_result_count']} logical results at "
              f"{self.generated['results']}")

    def load(self) -> None:
        if not self.generated:
            print("[warn] run option 1 first")
            return
        self.manifests = self.workbench.load_manifest(
            self.generated["manifest"])
        loaded = 0
        for entry in self.manifests:
            for call in entry.tool_calls:
                file_path = self.workbench.resolve_result_file(
                    self.generated["results"], call.tool_id,
                    call.result_format_version)
                info = self.workbench.load_result(call.result_ref, file_path)
                loaded += 1
        print(f"[ok] loaded {loaded} calls covering "
              f"{len(self.workbench.store.all_refs())} unique results")

    def assemble_all(self) -> None:
        if not self.manifests:
            print("[warn] run option 2 first")
            return
        for entry in self.manifests:
            assembly = self.workbench.assemble_request(entry)
            print(f"[ok] {assembly.request_id}: "
                  f"{len(assembly.fragments)} fragments, "
                  f"reused={len(assembly.reused)} "
                  f"recomputed={len(assembly.recomputed)}")

    def show_graph(self) -> None:
        model = self.workbench.build_view_model()
        assembly = model.get("assembly")
        if not assembly:
            print("[warn] assemble a request first")
            return
        for node in assembly["dag"]:
            print(f"  {node['status']:<10} {node['kind']:<9} "
                  f"{node['node_id']} -> {node['output_hash'][:12]}")

    def show_invalidation_and_self_check(self) -> None:
        if not self.manifests:
            print("[warn] run option 2 first")
            return
        entry = self.manifests[0]
        assembly = self.workbench.assemblies[entry.request_id]
        report = self.workbench.self_check(entry, assembly)
        print(f"[self-check] ok={report.ok}")
        for failure in report.failures:
            print(f"  - {failure}")
        print(f"  affected_results={assembly.affected_results}")
        print(f"  affected_fragments={len(assembly.affected_fragments)}")
        print(f"  recompute_suffix={assembly.recompute_suffix}")

    def switch_format_view(self) -> None:
        result_ref = input("result_ref: ").strip()
        version = int(input("target version (1/2/3): ").strip())
        try:
            print(self.workbench.format_view(result_ref, version))
        except DiagError as exc:
            print(f"[{exc.code}] {exc.message} {exc.context}")

    def show_migration_detail(self) -> None:
        for result_ref in self.workbench.store.all_refs():
            node = self.workbench.store.node(result_ref)
            print(f"\n== {result_ref} (stored v{node.result_format_version}) ==")
            for item in node.completions:
                print(f"  completion {item.field_name}: {item.action} "
                      f"from {item.source} ({item.reason})")
            outcome = migrate(node, 1, MigrationPolicy())
            for item in outcome.discards:
                print(f"  discard {item.item_type}:{item.item_id} "
                      f"rule={item.rule} ({item.detail})")

    def show_snapshots_and_errors(self) -> None:
        print("snapshot versions:",
              json.dumps(self.workbench.snapshot_versions()))
        if not self.workbench.recent_errors:
            print("no business errors recorded")
        for item in self.workbench.recent_errors:
            print(f"  {item['code']}: {item['message']}")

    def write_reports(self) -> None:
        reports = self.base / "reports"
        reports.mkdir(exist_ok=True)
        html_path = self.workbench.render_html_report(
            reports / "diagnostic_report.html")
        json_path = self.workbench.render_json_report(
            reports / "diagnostic_report.json")
        print(f"[ok] wrote {html_path} and {json_path}")

    def run(self) -> None:
        actions = {
            "1": self.generate,
            "2": self.load,
            "3": self.assemble_all,
            "4": self.show_graph,
            "5": self.show_invalidation_and_self_check,
            "6": self.switch_format_view,
            "7": self.show_migration_detail,
            "8": self.show_snapshots_and_errors,
            "9": self.write_reports,
        }
        while True:
            print(MENU)
            choice = input("choice> ").strip()
            if choice == "0":
                print("bye")
                return
            action = actions.get(choice)
            if action is None:
                print("unknown choice")
                continue
            try:
                action()
            except DiagError as exc:
                self.workbench.recent_errors.append(
                    {"code": exc.code, "message": exc.message,
                     "context": exc.context})
                print(f"[{exc.code}] {exc.message} {exc.context}")


def main() -> None:
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    InteractiveSession(base).run()


if __name__ == "__main__":
    main()
