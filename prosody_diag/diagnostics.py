from __future__ import annotations

from pathlib import Path
from typing import Any

from .audio import extract_acoustic_record
from .canonical import canonical_hash, read_jsonl, write_json_atomic
from .config import Config
from .errors import E_SEMANTIC_LOSS, DiagnosticError
from .precision import PrecisionContext
from .trees import (
    canonical_tree_view,
    infer_candidate,
    load_decision_tree,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
    parse_v1_tree,
    write_migrated_v2,
)
from .validation import load_tasks


def rule_set_fingerprint(tree: dict[str, Any]) -> str:
    view = canonical_tree_view(tree)
    view.pop("tree_id", None)
    return canonical_hash(view)


def _all_boundary_signatures(
    sample_dir: str | Path, cfg: Config, trees: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    tasks = load_tasks(sample_dir)
    truth = read_jsonl(Path(sample_dir) / "frame_truth.jsonl")
    truth_by_utterance: dict[str, list[dict[str, Any]]] = {}
    for row in truth:
        truth_by_utterance.setdefault(row["utterance_id"], []).append(row)
    result: dict[str, list[dict[str, Any]]] = {version: [] for version in trees}
    context = PrecisionContext("fp64")
    for task in tasks:
        record = extract_acoustic_record(
            task,
            Path(sample_dir) / "audio" / f"{task['speaker_id']}.pcm",
            truth_by_utterance[task["utterance_id"]],
            cfg,
        )
        for candidate in record["candidates"]:
            for version, tree in trees.items():
                inference = infer_candidate(tree, candidate, context, cfg)
                result[version].append(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "label": inference["label"],
                        "rule_id": inference["selected_rule_id"],
                        "path_hash": canonical_hash(
                            [
                                [(c["feature"], c["result"], c["margin"]) for c in trace["conditions"]]
                                for trace in inference["decision_path"]
                            ]
                        ),
                    }
                )
    return {version: canonical_hash(rows) for version, rows in result.items()}


def verify_deterministic_reruns(sample_dir: str | Path, cfg: Config) -> dict[str, Any]:
    trees = {
        "v1": load_decision_tree(Path(sample_dir) / "trees" / "v1" / "prosody_tree_v1.json"),
        "v2": load_decision_tree(Path(sample_dir) / "trees" / "v2"),
    }
    first = _all_boundary_signatures(sample_dir, cfg, trees)
    second = _all_boundary_signatures(sample_dir, cfg, trees)
    return {
        "first_run_hashes": first,
        "second_run_hashes": second,
        "byte_identical": first == second,
    }


def verify_migrations(
    sample_dir: str | Path, output_dir: str | Path
) -> dict[str, Any]:
    sample_root = Path(sample_dir)
    migration_dir = Path(output_dir) / "migration"
    migration_dir.mkdir(parents=True, exist_ok=True)
    v1 = load_decision_tree(sample_root / "trees" / "v1" / "prosody_tree_v1.json")
    v2 = load_decision_tree(sample_root / "trees" / "v2")

    package, upgrade_report = migrate_v1_to_v2(v1)
    v1_to_v2_dir = migration_dir / "v1_to_v2"
    write_migrated_v2(v1_to_v2_dir, package)
    migrated_v2 = load_decision_tree(v1_to_v2_dir)
    v1_fingerprint = rule_set_fingerprint(v1)
    upgraded_fingerprint = rule_set_fingerprint(migrated_v2)

    downgraded, downgrade_report = migrate_v2_to_v1(migrated_v2, strict_equivalence=True)
    write_json_atomic(migration_dir / "v2_to_v1.json", downgraded)
    reparsed_v1 = parse_v1_tree(downgraded, migration_dir / "v2_to_v1.json")
    downgraded_fingerprint = rule_set_fingerprint(reparsed_v1)

    sequence_original = _all_boundary_signatures(
        sample_root, Config(), {"v1": v1}
    )["v1"]
    sequence_upgraded = _all_boundary_signatures(
        sample_root, Config(), {"v2_migrated": migrated_v2}
    )["v2_migrated"]

    strict_native_code = None
    try:
        migrate_v2_to_v1(v2, strict_equivalence=True)
    except DiagnosticError as exc:
        strict_native_code = exc.code
    native_downgrade, native_report = migrate_v2_to_v1(
        v2, tombstone_policy="discard", strict_equivalence=False
    )
    write_json_atomic(migration_dir / "native_v2_downgraded_discard.json", native_downgrade)
    write_json_atomic(migration_dir / "migration_sources.json", upgrade_report)
    write_json_atomic(migration_dir / "downgrade_report.json", downgrade_report)
    write_json_atomic(migration_dir / "native_downgrade_report.json", native_report)

    return {
        "v1_rule_set_hash": v1_fingerprint,
        "v1_to_v2_rule_set_hash": upgraded_fingerprint,
        "v2_to_v1_rule_set_hash": downgraded_fingerprint,
        "v1_to_v2_internal_rules_identical": v1_fingerprint == upgraded_fingerprint,
        "v2_to_v1_internal_rules_identical": upgraded_fingerprint == downgraded_fingerprint,
        "boundary_sequence_hash_v1": sequence_original,
        "boundary_sequence_hash_migrated_v2": sequence_upgraded,
        "boundary_sequences_identical": sequence_original == sequence_upgraded,
        "completion_source_count": len(upgrade_report["completion_sources"]),
        "strict_native_v2_downgrade_code": strict_native_code or E_SEMANTIC_LOSS,
        "native_v2_discarded_tombstones": native_report["discarded_tombstone_ids"],
        "native_v2_flattened_rules": native_report["flattened_rule_ids"],
    }
