"""Command-line and interactive diagnostic workbench."""

from __future__ import annotations

import argparse
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from .bridge import affected_partitions, parse_bridge
from .data import Query, read_queries
from .errors import WorkbenchError, format_corrupt
from .expand import ExpansionConfig, expand_query
from .migration import MigrationPolicies, migrate_rules
from .rules import (
    parse_rules,
    ruleset_checksum,
    ruleset_semantic_dict,
    serialize_ruleset,
)
from .samplegen import DEFAULT_SEED, generate_samples
from .scoring import RetrievalConfig
from .selfcheck import run_selfcheck
from .snapshots import SnapshotStore
from .util import atomic_write_bytes, sha256_bytes, stable_json_bytes
from .workbench import (
    BuildConfig,
    build_and_commit,
    query_view,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qxworkbench",
        description="Local deterministic query rewrite and index scheduling workbench",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-samples", help="generate deterministic samples")
    init_parser.add_argument("--samples-dir", default="samples")
    init_parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    init_parser.set_defaults(handler=_cmd_init_samples)

    parse_parser = subparsers.add_parser("parse-rules", help="parse a v1/v2/v3 rule file")
    parse_parser.add_argument("--file", required=True)
    parse_parser.add_argument("--version", choices=["v1", "v2", "v3"])
    parse_parser.add_argument("--output")
    parse_parser.set_defaults(handler=_cmd_parse_rules)

    migrate_parser = subparsers.add_parser("migrate", help="migrate rules between versions")
    migrate_parser.add_argument("--input", required=True)
    migrate_parser.add_argument("--from-version", choices=["v1", "v2", "v3"])
    migrate_parser.add_argument("--to", required=True, choices=["v1", "v2", "v3"])
    migrate_parser.add_argument("--multi-target-policy", choices=["flatten", "drop"], default="flatten")
    migrate_parser.add_argument("--condition-policy", choices=["flatten", "drop"], default="flatten")
    migrate_parser.add_argument("--output", required=True)
    migrate_parser.add_argument("--report-output")
    migrate_parser.set_defaults(handler=_cmd_migrate)

    build_parser_cmd = subparsers.add_parser("build", help="build/commit a coordinated snapshot")
    _add_build_arguments(build_parser_cmd)
    build_parser_cmd.set_defaults(handler=_cmd_build)

    query_parser = subparsers.add_parser("query", help="rewrite and retrieve one query")
    query_parser.add_argument("--store", required=True)
    query_parser.add_argument("--queries", required=True)
    query_parser.add_argument("--query-id", required=True)
    query_parser.add_argument("--top-k", type=int)
    query_parser.add_argument("--merge-policy", choices=["sum", "max"], default="sum")
    query_parser.add_argument("--max-candidates", type=int, default=64)
    query_parser.add_argument("--min-confidence", type=float, default=0.0)
    query_parser.add_argument("--max-postings", type=int, default=1_000_000)
    query_parser.add_argument("--max-resident-bytes", type=int, default=64 * 1024 * 1024)
    query_parser.add_argument("--expected-rule-version", type=int)
    query_parser.add_argument("--expected-bridge-version", type=int)
    query_parser.add_argument("--expected-index-version", type=int)
    query_parser.set_defaults(handler=_cmd_query)

    status_parser = subparsers.add_parser("status", help="show active snapshot status")
    status_parser.add_argument("--store", required=True)
    status_parser.set_defaults(handler=_cmd_status)

    selfcheck_parser = subparsers.add_parser("selfcheck", help="run cross-module checks")
    selfcheck_parser.add_argument("--store", required=True)
    selfcheck_parser.add_argument("--queries")
    selfcheck_parser.add_argument("--max-candidates", type=int, default=256)
    selfcheck_parser.add_argument("--min-confidence", type=float, default=0.0)
    selfcheck_parser.set_defaults(handler=_cmd_selfcheck)

    diagnose_parser = subparsers.add_parser("diagnose", help="interactive diagnostic console")
    diagnose_parser.add_argument("--store", required=True)
    diagnose_parser.add_argument("--queries", required=True)
    diagnose_parser.add_argument("--max-candidates", type=int, default=64)
    diagnose_parser.add_argument("--min-confidence", type=float, default=0.0)
    diagnose_parser.add_argument("--merge-policy", choices=["sum", "max"], default="sum")
    diagnose_parser.set_defaults(handler=_cmd_diagnose)

    demo_parser = subparsers.add_parser("demo", help="generate samples and run the full demo")
    demo_parser.add_argument("--root", default="runtime/demo")
    demo_parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    demo_parser.add_argument("--workers", type=int, default=4)
    demo_parser.set_defaults(handler=_cmd_demo)
    return parser


def _add_build_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", required=True)
    parser.add_argument("--documents", required=True)
    parser.add_argument("--rules", required=True)
    parser.add_argument("--rule-version", type=int, required=True)
    parser.add_argument("--bridge", required=True)
    parser.add_argument("--index-version", type=int, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-postings", type=int, default=1_000_000)
    parser.add_argument("--max-parallel-partitions", type=int, default=8)
    parser.add_argument("--parallel-policy", choices=["degrade", "error"], default="degrade")
    parser.add_argument(
        "--crash-after",
        type=int,
        dest="crash_after_partitions",
        help="deliberately terminate after N newly built partitions",
    )


def _cmd_init_samples(args: argparse.Namespace) -> dict[str, Any]:
    manifest = generate_samples(args.samples_dir, seed=args.seed)
    return {"ok": True, "samples_dir": str(Path(args.samples_dir).resolve()), "manifest": manifest}


def _cmd_parse_rules(args: argparse.Namespace) -> dict[str, Any]:
    ruleset = parse_rules(args.file, expected_version=args.version)
    canonical = serialize_ruleset(ruleset)
    if args.output:
        atomic_write_bytes(Path(args.output), canonical)
    return {
        "ok": True,
        "format_version": ruleset.format_version,
        "checksum": ruleset_checksum(ruleset),
        "rule_count": len(ruleset.rules),
        "canonical_sha256": sha256_bytes(canonical),
        "canonical_bytes": canonical.decode("utf-8"),
        "rules": ruleset_semantic_dict(ruleset)["rules"],
    }


def _cmd_migrate(args: argparse.Namespace) -> dict[str, Any]:
    source = parse_rules(args.input, expected_version=args.from_version)
    result = migrate_rules(
        source,
        args.to,
        policies=MigrationPolicies(
            multi_target=args.multi_target_policy,
            condition=args.condition_policy,
        ),
    )
    output_bytes = result.bytes()
    atomic_write_bytes(Path(args.output), output_bytes)
    reread = result.reread()
    if args.report_output:
        atomic_write_bytes(
            Path(args.report_output),
            stable_json_bytes(result.report),
        )
    return {
        "ok": True,
        "report": result.report,
        "output": str(Path(args.output).resolve()),
        "output_sha256": sha256_bytes(output_bytes),
        "reread_format_version": reread.format_version,
        "reread_rule_count": len(reread.rules),
    }


def _cmd_build(args: argparse.Namespace) -> dict[str, Any]:
    ruleset = parse_rules(args.rules)
    bridge = parse_bridge(args.bridge)
    store = SnapshotStore(args.store)
    result = build_and_commit(
        store,
        documents_path=args.documents,
        ruleset=ruleset,
        rule_version=args.rule_version,
        bridge=bridge,
        index_version=args.index_version,
        config=BuildConfig(
            workers=args.workers,
            max_postings=args.max_postings,
            max_parallel_partitions=args.max_parallel_partitions,
            parallel_policy=args.parallel_policy,
            crash_after_partitions=args.crash_after_partitions,
        ),
    )
    return {
        "ok": True,
        "build_id": result.build_id,
        "resumed": result.resumed,
        "commit": result.commit,
        "index": {
            key: result.index_manifest[key]
            for key in (
                "version",
                "checksum",
                "posting_count",
                "decoded_bytes",
                "degraded",
            )
        },
    }


def _cmd_query(args: argparse.Namespace) -> dict[str, Any]:
    return _run_query(
        store=SnapshotStore(args.store),
        queries_path=args.queries,
        query_id=args.query_id,
        top_k=args.top_k,
        merge_policy=args.merge_policy,
        expansion_config=ExpansionConfig(
            max_candidates=args.max_candidates,
            min_confidence=args.min_confidence,
        ),
        retrieval_config=RetrievalConfig(
            merge_policy=args.merge_policy,
            max_postings=args.max_postings,
            max_resident_bytes=args.max_resident_bytes,
        ),
        expected_rule_version=args.expected_rule_version,
        expected_bridge_version=args.expected_bridge_version,
        expected_index_version=args.expected_index_version,
        include_ok=True,
    )


def _run_query(
    *,
    store: SnapshotStore,
    queries_path: str,
    query_id: str,
    top_k: int | None,
    merge_policy: str,
    expansion_config: ExpansionConfig,
    retrieval_config: RetrievalConfig,
    expected_rule_version: int | None = None,
    expected_bridge_version: int | None = None,
    expected_index_version: int | None = None,
    include_ok: bool = False,
) -> dict[str, Any]:
    current = store.load_current()
    if any(
        value is not None
        for value in (
            expected_rule_version,
            expected_bridge_version,
            expected_index_version,
        )
    ):
        view = store.load_commit(
            current.commit_id,
            expected_rule_version=expected_rule_version,
            expected_bridge_version=expected_bridge_version,
            expected_index_version=expected_index_version,
        )
    else:
        view = current
    query = _find_query(read_queries(queries_path), query_id)
    if top_k is not None:
        query = replace(query, top_k=top_k)
    result = query_view(
        view,
        query,
        expansion_config=expansion_config,
        retrieval_config=retrieval_config,
    )
    result["commit_id"] = view.commit_id
    if include_ok:
        result["ok"] = True
    return result


def _cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    store = SnapshotStore(args.store)
    view = store.load_current()
    return _status_document(view)


def _status_document(view: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "commit": {
            key: view.commit[key]
            for key in (
                "commit_id",
                "rule_version",
                "rule_checksum",
                "bridge_version",
                "bridge_checksum",
                "index_version",
                "index_checksum",
                "affected_partitions",
                "reused_partitions",
                "recomputed_partitions",
                "degraded",
            )
        },
        "rules": {
            "format_version": view.rules.format_version,
            "rule_count": len(view.rules.rules),
        },
        "bridge": {
            "version": view.bridge.version,
            "pair_count": len(view.bridge.language_pairs),
            "entry_count": len(view.bridge.entries),
        },
        "index": {
            "version": view.index_manifest["version"],
            "posting_count": view.index_manifest["posting_count"],
            "decoded_bytes": view.index_manifest["decoded_bytes"],
            "partitions": [
                {
                    "lang": item["lang"],
                    "posting_count": item["posting_count"],
                    "decoded_bytes": item["decoded_bytes"],
                    "checksum": item["checksum"],
                }
                for item in view.index_manifest["partitions"]
            ],
            "degraded": view.index_manifest["degraded"],
        },
    }


def _cmd_selfcheck(args: argparse.Namespace) -> dict[str, Any]:
    report = run_selfcheck(
        SnapshotStore(args.store),
        queries_path=args.queries,
        expansion_config=ExpansionConfig(
            max_candidates=args.max_candidates,
            min_confidence=args.min_confidence,
        ),
    )
    return report


def _cmd_diagnose(args: argparse.Namespace) -> dict[str, Any]:
    store = SnapshotStore(args.store)
    queries = read_queries(args.queries)
    expansion_config = ExpansionConfig(
        max_candidates=args.max_candidates,
        min_confidence=args.min_confidence,
    )
    retrieval_config = RetrievalConfig(merge_policy=args.merge_policy)
    interactive = sys.stdin.isatty()
    if interactive:
        print("qxworkbench diagnostics: help, status, versions, queries, expand ID, search ID, selfcheck, quit")
    while True:
        if interactive:
            print("diag> ", end="", flush=True)
        line = sys.stdin.readline()
        if not line:
            break
        command_line = line.strip()
        if not command_line:
            continue
        try:
            parts = shlex.split(command_line)
        except ValueError as exc:
            print(stable_json_bytes({"ok": False, "error": str(exc)}).decode(), end="")
            continue
        command = parts[0].lower()
        if command in {"quit", "exit"}:
            break
        if command == "help":
            response: Any = {
                "ok": True,
                "commands": [
                    "status",
                    "versions",
                    "queries",
                    "expand <query_id>",
                    "search <query_id>",
                    "selfcheck",
                    "quit",
                ],
            }
        elif command == "status":
            response = _status_document(store.load_current())
        elif command == "versions":
            view = store.load_current()
            response = {
                "ok": True,
                "rule_version": view.commit["rule_version"],
                "bridge_version": view.commit["bridge_version"],
                "index_version": view.commit["index_version"],
            }
        elif command == "queries":
            response = {
                "ok": True,
                "queries": [query.to_dict() for query in queries],
            }
        elif command == "expand" and len(parts) == 2:
            view = store.load_current()
            query = _find_query(queries, parts[1])
            response = {
                "ok": True,
                "expansion": expand_query(
                    query,
                    view.rules,
                    view.bridge,
                    config=expansion_config,
                ),
            }
        elif command == "search" and len(parts) == 2:
            response = _run_query(
                store=store,
                queries_path=args.queries,
                query_id=parts[1],
                top_k=None,
                merge_policy=args.merge_policy,
                expansion_config=expansion_config,
                retrieval_config=retrieval_config,
                include_ok=True,
            )
        elif command == "selfcheck":
            response = run_selfcheck(
                store,
                queries=queries,
                expansion_config=expansion_config,
            )
        else:
            response = {
                "ok": False,
                "error": {
                    "code": "E_DIAGNOSTIC_COMMAND",
                    "message": f"unknown or malformed command: {command_line}",
                },
            }
        print(stable_json_bytes(response).decode(), end="")
    return {"ok": True, "diagnostics_closed": True}


def _cmd_demo(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root)
    samples_dir = root / "samples"
    store = SnapshotStore(root / "state")
    generate_samples(samples_dir, seed=args.seed)
    rules = parse_rules(samples_dir / "rules_v3.json")
    bridge_paths = [
        samples_dir / "bridge_v1.json",
        samples_dir / "bridge_v2.json",
        samples_dir / "bridge_v3.json",
    ]
    builds: list[dict[str, Any]] = []
    previous_bridge = None
    for index, bridge_path in enumerate(bridge_paths, start=1):
        bridge = parse_bridge(bridge_path)
        affected = (
            list(affected_partitions(previous_bridge, bridge))
            if previous_bridge is not None
            else []
        )
        result = build_and_commit(
            store,
            documents_path=samples_dir / "documents.jsonl",
            ruleset=rules,
            rule_version=1,
            bridge=bridge,
            index_version=index,
            config=BuildConfig(workers=args.workers),
        )
        builds.append(
            {
                "index_version": index,
                "bridge_version": bridge.version,
                "predicted_affected": affected,
                "commit": result.commit,
            }
        )
        previous_bridge = bridge

    queries = read_queries(samples_dir / "queries.jsonl")
    view = store.load_current()
    query_results = [
        query_view(view, query)
        for query in queries
    ]
    check = run_selfcheck(store, queries=queries)
    report = {
        "ok": True,
        "root": str(root.resolve()),
        "samples_dir": str(samples_dir.resolve()),
        "store": str((root / "state").resolve()),
        "builds": builds,
        "query_results": query_results,
        "selfcheck": check,
    }
    atomic_write_bytes(
        root / "demo_report.json",
        stable_json_bytes(report),
    )
    return report


def _find_query(queries: Sequence[Query], query_id: str) -> Query:
    for query in queries:
        if query.query_id == query_id:
            return query
    raise format_corrupt("unknown query_id", query_id=query_id)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except WorkbenchError as exc:
        sys.stdout.write(stable_json_bytes(exc.as_dict()).decode("utf-8"))
        return 2
    if result is not None:
        sys.stdout.write(stable_json_bytes(result).decode("utf-8"))
    return 0
