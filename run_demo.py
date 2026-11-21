"""End-to-end deterministic verification driver.

Runs the complete story on local generated data and writes the verification
record to ``reports/verification_results.txt`` plus HTML/JSON diagnostic
reports. No network access is performed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from diagwb.errors import DiagError
from diagwb.equivalence import adjudicate, round_trip
from diagwb.formats import parse_file, parse_text
from diagwb.graph import recompose_answer
from diagwb.lineage import replay_assembly, replay_fragment
from diagwb.migration import MigrationPolicy, migrate, normalize_record
from diagwb.samples import SAMPLE_SEED, generate_workspace
from diagwb.snapshots import ANSWERS, FORMAT_DEFS, REPOSITORY
from diagwb.workbench import DiagnosticWorkbench

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
RUNTIME = BASE / "runtime"
REPORTS = BASE / "reports"


class Recorder:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def log(self, text: str = "") -> None:
        print(text)
        self.lines.append(text)

    def write(self, path: Path) -> None:
        path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def reset() -> None:
    for path in (DATA, RUNTIME, REPORTS):
        if path.exists():
            shutil.rmtree(path)
    REPORTS.mkdir(parents=True, exist_ok=True)


def main() -> int:
    reset()
    record = Recorder()
    record.log("=" * 68)
    record.log("Tool-Result Format Evolution - Local Diagnostic Workbench")
    record.log(f"deterministic seed = {SAMPLE_SEED}")
    record.log("=" * 68)

    # 1) deterministic samples ---------------------------------------------
    info = generate_workspace(DATA)
    record.log(f"[1] generated {info['logical_result_count']} logical results, "
               f"each stored as v1/v2/v3 under {info['results']}")

    wb = DiagnosticWorkbench(RUNTIME)
    manifests = wb.load_manifest(info["manifest"])
    record.log(f"    manifest requests={len(manifests)} "
               f"calls={sum(len(m.tool_calls) for m in manifests)}")

    # 2) load repository ----------------------------------------------------
    loaded = 0
    for entry in manifests:
        for call in entry.tool_calls:
            path = wb.resolve_result_file(info["results"], call.tool_id,
                                          call.result_format_version)
            wb.load_result(call.result_ref, path)
            loaded += 1
    record.log(f"[2] loaded {loaded} calls; unique resident results="
               f"{len(wb.store.all_refs())}")

    # 3) tri-format migration + round trip ---------------------------------
    record.log("[3] cross-format migration and round-trip")
    first_tool = next(info["results"].iterdir())
    nodes = {ver: normalize_record(
        parse_file(first_tool / f"v{ver}.txt")[1][0])
        for ver in (1, 2, 3)}
    verdict = adjudicate(nodes)
    record.log(f"    native v1/v2/v3 equivalent={verdict.equivalent} "
               f"capability_gaps={verdict.irrecoverable}")
    v3 = nodes[3]
    for version in (1, 2, 3):
        rt = round_trip(v3, version)
        record.log(f"    round-trip v{version}: ok={rt.ok} "
                   f"payload={rt.payload_equal} status={rt.status_equal} "
                   f"shards={rt.shards_equal} ci={rt.confidence_equal} "
                   f"lineage={rt.lineage_equal}")
    downgrade = migrate(v3, 1, MigrationPolicy())
    record.log("    v3->v1 discard audit: "
               + ", ".join(f"{d.item_type}:{d.rule}"
                           for d in downgrade.discards))
    try:
        dropped = migrate(v3, 1, MigrationPolicy(
            shards="drop", aggregation="drop", lineage="drop"))
        rebuilt = normalize_record(
            parse_text(f"# FORMAT_VERSION=1\n{dropped.body}\n")[1][0])
        adjudicate({1: rebuilt, 3: v3})
    except DiagError as exc:
        record.log(f"    drop-policy drift -> {exc.code} "
                   f"irrecoverable={exc.context['irrecoverable']}")

    # 4) initial assembly (full recompute) ---------------------------------
    record.log("[4] initial assembly")
    first_manifest = manifests[0]
    first = wb.assemble_request(first_manifest)
    record.log(f"    {first.request_id}: fragments={len(first.fragments)} "
               f"recomputed={len(first.recomputed)} reused={len(first.reused)}")

    # 5) second assembly: full cache reuse ---------------------------------
    second = wb.assemble_request(first_manifest)
    answer, answer_hash = recompose_answer(second.fragments)
    record.log(f"[5] re-assembly: recomputed={len(second.recomputed)} "
               f"reused={len(second.reused)} "
               f"recomputed_answer_matches={answer == second.answer}")

    # 6) incremental invalidation ------------------------------------------
    target = first_manifest.tool_calls[0]
    new_version = 3 if target.result_format_version != 3 else 2
    reload_info = wb.load_result(
        target.result_ref,
        wb.resolve_result_file(info["results"], target.tool_id, new_version))
    invalidation = wb.invalidate([target.result_ref])
    record.log(f"[6] replaced {target.result_ref} -> v{new_version} "
               f"entry_version={reload_info['entry_version']} "
               f"changed={reload_info['changed']}")
    record.log(f"    affected_results={invalidation.affected_results}")
    record.log(f"    unaffected_results={len(invalidation.unaffected_results)}"
               f" affected_fragments={len(invalidation.affected_fragments)} "
               f"reused={len(invalidation.reused)}")
    refreshed = wb.assemble_request(first_manifest, invalidation)
    record.log(f"    incremental assembly recomputed="
               f"{len(refreshed.recomputed)} reused={len(refreshed.reused)} "
               f"suffix={refreshed.recompute_suffix}")

    # 7) stale rejection + lineage replay ----------------------------------
    record.log("[7] staleness and lineage replay")
    stale_fragment = next(item for item in first.fragments
                          if item.result_ref == target.result_ref)
    try:
        replay_fragment(stale_fragment, wb.cache, wb.store,
                        stale_fragment.position)
    except DiagError as exc:
        record.log(f"    stale replay -> {exc.code} ref={exc.context['result_ref']} "
                   f"v{exc.context['source_version']}->"
                   f"v{exc.context['current_version']}")
    replay = replay_assembly(refreshed, wb.cache, wb.store)
    record.log(f"    fresh assembly replay verdicts={len(replay)} "
               f"all_match={all(v.matches for v in replay)}")
    # Reloading identical bytes bumps the entry version but not the content;
    # the change detector must still report changed=False.
    unchanged = wb.load_result(
        target.result_ref,
        wb.resolve_result_file(info["results"], target.tool_id, new_version))
    record.log(f"    unchanged reload changed_flag={unchanged['changed']} "
               "(must be False)")

    # 8) self-check ---------------------------------------------------------
    report = wb.self_check(first_manifest, refreshed, invalidation)
    record.log(f"[8] cross-module self-check ok={report.ok} "
               f"failures={report.failures}")

    # 9) snapshots ----------------------------------------------------------
    record.log("[9] versioned snapshots and concurrency control")
    with wb.snapshots.commit_stage():
        wb.snapshots.commit(REPOSITORY,
                            {"fingerprint": wb.store.content_fingerprint()})
        wb.snapshots.commit(FORMAT_DEFS,
                            json.loads((info["format_definitions"]).read_text()))
        wb.snapshots.commit(ANSWERS,
                            {"request_id": refreshed.request_id,
                             "answer_hash": refreshed.answer_hash})
    record.log(f"    snapshot versions={wb.snapshot_versions()}")
    try:
        wb.snapshots.try_rollback(REPOSITORY, 1)
    except DiagError as exc:
        record.log(f"    rollback attempt -> {exc.code}")

    # 10) reports -----------------------------------------------------------
    html_path = wb.render_html_report(
        REPORTS / "diagnostic_report.html", first_manifest.request_id)
    json_path = wb.render_json_report(
        REPORTS / "diagnostic_report.json", first_manifest.request_id)
    record.log(f"[10] wrote {html_path} and {json_path}")
    record.write(REPORTS / "verification_results.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
