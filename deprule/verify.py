"""Cross-format equivalence adjudication and trace replay."""

from __future__ import annotations

from pathlib import Path

from .common import canonical_hash
from .errors import Codes, DomainError
from .parser import build_version_chain
from .rules import assert_ir_equivalent, diff_first
from .runner import PipelineRunner


def parse_semantic_view(result: dict) -> dict:
    sentences = {}
    for item in result["results"]:
        sentences[item["sentence_id"]] = {
            "syntax_tree_set": sorted(
                item["syntax_tree_set"],
                key=lambda span: (span["label"], span["start"], span["end"]),
            ),
            "dependency_arcs": sorted(
                (
                    {
                        "arc_id": arc["arc_id"],
                        "head": arc["head"],
                        "dependent": arc["dependent"],
                        "label": arc["label"],
                        "direction": arc["direction"],
                    }
                    for arc in item["dependency_arcs"]
                ),
                key=lambda arc: (arc["dependent"], arc["head"], arc["label"]),
            ),
            "parse_labels": sorted(
                item["parse_labels"],
                key=lambda row: (row["token_index"], row["label"], row["arc_id"]),
            ),
            "roots": sorted(
                ({"index": root["index"], "label": root["label"]} for root in item["roots"]),
                key=lambda root: root["index"],
            ),
            "constraints": item["constraints"],
            "stage_checksums": item["stage_checksums"],
        }
    return sentences


def _category_for_difference(field: str) -> tuple[str, str]:
    lowered = field.lower()
    if "dependency_arcs" in lowered or "arc" in lowered:
        return "arc_set", field
    if "parse_labels" in lowered or "label" in lowered:
        return "label_set", field
    if "roots" in lowered:
        return "root_set", field
    if "syntax_tree" in lowered:
        return "syntax_tree_set", field
    if "constraints" in lowered:
        return "constraint_set", field
    if "stage_checksums" in lowered:
        return "stage_checksum", field
    return "parse_result", field


def adjudicate_equivalence(
    tasks: list[dict],
    snapshots: dict[str, dict],
    state_dir: str | Path,
) -> dict:
    formats = sorted(snapshots)
    baseline_format = formats[0]
    baseline = snapshots[baseline_format]
    ir_reports = {}
    for fmt in formats[1:]:
        assert_ir_equivalent(baseline["ir"], snapshots[fmt]["ir"], f"compare {baseline_format}/{fmt}")
        ir_reports[fmt] = snapshots[fmt]["ir"]["semantic_hash"]

    parse_views = {}
    result_hashes = {}
    for fmt in formats:
        runner = PipelineRunner(
            tasks,
            snapshots[fmt],
            Path(state_dir) / "equivalence",
            fresh=True,
            resume=False,
        )
        result, ledger = runner.run()
        view = parse_semantic_view(result)
        parse_views[fmt] = view
        result_hashes[fmt] = canonical_hash(view)

    for fmt in formats[1:]:
        difference = diff_first(parse_views[baseline_format], parse_views[fmt])
        if difference:
            category, location = _category_for_difference(difference)
            raise DomainError(
                f"cross-format parse semantic drift between {baseline_format} and {fmt}: {difference}",
                code=Codes.E_SEMANTIC_DRIFT,
                baseline_format=baseline_format,
                actual_format=fmt,
                difference_category=category,
                difference_location=location,
            )

    return {
        "business_code": Codes.OK,
        "formats": formats,
        "ir_semantic_hash": baseline["ir"]["semantic_hash"],
        "parse_semantic_hashes": result_hashes,
        "equivalent": True,
        "sentence_count": len(tasks),
    }


def detect_stale(export_doc: dict, tasks: list[dict], snapshot: dict) -> list[dict]:
    task_by_id = {task["sentence_id"]: task for task in tasks}
    stale_chains = []
    for result in export_doc.get("results", []):
        sid = result["sentence_id"]
        task = task_by_id.get(sid)
        if task is None:
            raise DomainError(
                f"export sentence {sid} not present in tasks",
                code=Codes.E_NOT_FOUND,
                sentence_id=sid,
            )
        current_chain = {row["name"]: row["value"] for row in build_version_chain(task, snapshot, snapshot["ir"])}
        recorded_chain = {row["name"]: row["value"] for row in result.get("version_chain", [])}
        changes = []
        for name, current_value in sorted(current_chain.items()):
            recorded_value = recorded_chain.get(name)
            if recorded_value != current_value:
                changes.append({
                    "name": name,
                    "recorded": recorded_value,
                    "current": current_value,
                })
        if changes:
            stale_chains.append({
                "sentence_id": sid,
                "version_chain": result.get("version_chain", []),
                "changed_versions": changes,
            })
    return stale_chains


def replay_export(
    export_doc: dict,
    tasks: list[dict],
    snapshot: dict,
    state_dir: str | Path,
) -> dict:
    stale_chains = detect_stale(export_doc, tasks, snapshot)
    if stale_chains:
        raise DomainError(
            "export is stale because one or more cache-key dependency versions changed",
            code=Codes.E_STALE_RESULT,
            stale_sentence_ids=[item["sentence_id"] for item in stale_chains],
            version_chains=stale_chains,
        )

    runner = PipelineRunner(
        tasks,
        snapshot,
        Path(state_dir) / "replay",
        fresh=True,
        resume=False,
    )
    recomputed, _ = runner.run()
    recomputed_by_id = {item["sentence_id"]: item for item in recomputed["results"]}
    verified_arcs = 0
    for original in export_doc.get("results", []):
        sid = original["sentence_id"]
        fresh = recomputed_by_id[sid]
        difference = diff_first(
            {
                "arcs": sorted(original["dependency_arcs"], key=lambda x: x["arc_id"]),
                "labels": sorted(original["parse_labels"], key=lambda x: x["arc_id"]),
                "roots": original["roots"],
            },
            {
                "arcs": sorted(fresh["dependency_arcs"], key=lambda x: x["arc_id"]),
                "labels": sorted(fresh["parse_labels"], key=lambda x: x["arc_id"]),
                "roots": fresh["roots"],
            },
        )
        if difference:
            category, location = _category_for_difference(difference)
            raise DomainError(
                f"replay mismatch for {sid}: {difference}",
                code=Codes.E_SEMANTIC_DRIFT,
                sentence_id=sid,
                difference_category=category,
                difference_location=location,
            )
        fresh_arcs = {arc["arc_id"]: arc for arc in fresh["dependency_arcs"]}
        for arc in original["dependency_arcs"]:
            candidate = fresh_arcs.get(arc["arc_id"])
            if candidate is None:
                raise DomainError(
                    f"replay arc missing: {arc['arc_id']}",
                    code=Codes.E_SEMANTIC_DRIFT,
                    sentence_id=sid,
                    arc_id=arc["arc_id"],
                )
            for field in ("head", "dependent", "label", "cache_key", "path_node_versions"):
                if arc.get(field) != candidate.get(field):
                    raise DomainError(
                        f"replay trace field mismatch for {arc['arc_id']}: {field}",
                        code=Codes.E_SEMANTIC_DRIFT,
                        sentence_id=sid,
                        arc_id=arc["arc_id"],
                        field=field,
                    )
            verified_arcs += 1

    return {
        "business_code": Codes.OK,
        "replayed_sentence_ids": [item["sentence_id"] for item in export_doc.get("results", [])],
        "verified_arc_count": verified_arcs,
        "same_arcs_labels_roots": True,
    }
