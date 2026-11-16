"""命令行编排：样本生成、构建、查询、档位切换与一致性验证。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .builder import GracefulStopRequested, IndexBuilder
from .engine import QueryEngine, QueryRequest
from .errors import WorkbenchError, make_failure
from .normalization import parse_stream
from .reporting import compare_canonical_results, write_report
from .runner import compare_query_runs, run_concurrent, run_serial
from .samplegen import write_samples
from .store import (
    IndexConfig,
    IndexLayout,
    dump_json,
    load_json,
    reset_dirs,
)


def _read_config(path: str | Path) -> IndexConfig:
    return IndexConfig.from_dict(load_json(path))


def _read_lines(path: str | Path) -> list[str]:
    with open(path, "r", encoding="utf-8") as handle:
        return [line.rstrip("\n") for line in handle if line.strip()]


def _load_requests(path: str | Path) -> list[QueryRequest]:
    payload = load_json(path)
    requests: list[QueryRequest] = []
    for item in payload["queries"]:
        requests.append(QueryRequest(
            query_id=item["query_id"],
            terms=list(item["terms"]),
            sort_key=int(item["sort_key"]),
            want_positions=bool(item.get("want_positions", True)),
            expected_count=item.get("expected_count"),
        ))
    return requests


def _parse_corpus(corpus_path: str | Path) -> tuple[list, list[dict[str, Any]]]:
    return parse_stream(_read_lines(corpus_path))


def _build(
    config: IndexConfig,
    layout: IndexLayout,
    documents: list | None,
    *,
    total_docs: int | None = None,
) -> dict[str, Any]:
    builder = IndexBuilder(config, layout)
    result = builder.build(documents, total_docs=total_docs)
    builder.restore_signal_handlers()
    return {
        "manifest": result.manifest,
        "reused_blocks": result.reused_blocks,
        "rebuilt_blocks": result.rebuilt_blocks,
        "affected_shards": result.affected_shards,
        "recovered": result.recovered,
    }


def _query_canonical(
    layout: IndexLayout,
    config: IndexConfig,
    requests: list[QueryRequest],
) -> list[dict[str, Any]]:
    engine = QueryEngine(layout, budget_ms=config.latency_budget_ms)
    return [engine.run(request).canonical() for request in requests]


def cmd_generate(args: argparse.Namespace) -> int:
    info = write_samples(
        args.sample_dir,
        seed=args.seed,
        doc_count=args.doc_count,
        vocab_size=args.vocab_size,
    )
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    config = _read_config(args.config)
    layout = IndexLayout(args.output_dir, args.snapshot_dir)
    if args.clean:
        reset_dirs(layout)
    documents, parse_failures = _parse_corpus(args.corpus)
    build_info = _build(config, layout, documents)
    report = {
        "parse_failures": parse_failures,
        "version_fingerprint": build_info["manifest"]["version_fingerprint"],
        "block_reuse": {
            "reused": build_info["reused_blocks"],
            "rebuilt": build_info["rebuilt_blocks"],
            "affected_shards": build_info["affected_shards"],
        },
        "doc_count": build_info["manifest"]["doc_count"],
    }
    write_report(args.report, report)
    print(json.dumps({"status": "ok", "report": args.report},
                     ensure_ascii=False))
    return 0


def _build_incremental(
    config: IndexConfig,
    layout: IndexLayout,
    documents: list,
    batch_size: int,
    parse_failures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """小批增量构建，逐批原子提交，返回每批构建摘要。"""
    builder = IndexBuilder(config, layout)
    summaries: list[dict[str, Any]] = []
    for start in range(0, len(documents), batch_size):
        batch = documents[start : start + batch_size]
        try:
            result = builder.build(batch, total_docs=len(documents))
            summaries.append({
                "batch_start": start,
                "batch_size": len(batch),
                "reused": len(result.reused_blocks),
                "rebuilt": len(result.rebuilt_blocks),
                "affected_shards": result.affected_shards,
                "graceful_stop": result.graceful_stop,
            })
        except GracefulStopRequested:
            builder.restore_signal_handlers()
            raise
    builder.restore_signal_handlers()
    return summaries


def _run_consistency_combo(
    base_config: IndexConfig,
    workspace: Path,
    documents: list,
    requests: list[QueryRequest],
    level: int,
    store_positions: bool,
    batch_size: int,
    parse_failures: list[dict[str, Any]],
) -> dict[str, Any]:
    config = base_config.with_format(level, store_positions)
    inc_layout = IndexLayout(
        workspace / "incremental",
        workspace / "incremental" / "snapshots",
    )
    full_layout = IndexLayout(
        workspace / "full",
        workspace / "full" / "snapshots",
    )
    reset_dirs(inc_layout)
    reset_dirs(full_layout)

    incremental_summaries = _build_incremental(
        config, inc_layout, documents, batch_size, parse_failures,
    )
    incremental_results = _query_canonical(inc_layout, config, requests)

    full_builder = IndexBuilder(config, full_layout)
    full_result = full_builder.build(documents)
    full_builder.restore_signal_handlers()
    full_results = _query_canonical(full_layout, config, requests)

    comparison = compare_canonical_results(incremental_results, full_results)
    return {
        "compression_level": level,
        "store_positions": store_positions,
        "incremental_batches": incremental_summaries,
        "incremental_version": incremental_results and _version_of(inc_layout),
        "full_version": full_result.manifest["version_fingerprint"],
        "query_comparison": comparison,
        "incremental_results": incremental_results,
        "full_results": full_results,
        "full_block_reuse": _load_reuse(full_layout),
    }


def _version_of(layout: IndexLayout) -> str:
    return str(load_json(layout.manifest_path)["version_fingerprint"])


def _load_reuse(layout: IndexLayout) -> dict[str, Any]:
    manifest = load_json(layout.manifest_path)
    return manifest.get("block_reuse", {"reused": [], "rebuilt": [],
                                        "affected_shards": []})


def _switch_format(
    config: IndexConfig,
    layout: IndexLayout,
    new_level: int,
    new_positions: bool,
    documents: list,
) -> dict[str, Any]:
    """在同一会话输出目录切换档位/位置开关，验证版本失效与块级重建。"""
    before = load_json(layout.manifest_path)
    new_config = config.with_format(new_level, new_positions)
    builder = IndexBuilder(new_config, layout)
    mismatch = builder.detect_version_mismatch()
    result = builder.build()
    builder.restore_signal_handlers()
    after = result.manifest
    return {
        "before": {
            "level": before["compression_level"],
            "store_positions": before["store_positions"],
            "version_fingerprint": before["version_fingerprint"],
        },
        "after": {
            "level": after["compression_level"],
            "store_positions": after["store_positions"],
            "version_fingerprint": after["version_fingerprint"],
        },
        "version_mismatch_detected": mismatch,
        "reused_blocks": result.reused_blocks,
        "rebuilt_blocks": result.rebuilt_blocks,
        "affected_shards": result.affected_shards,
        "no_silent_reuse": all(
            entry["level"] == new_level
            and entry["store_positions"] == new_positions
            for entry in after["blocks"]
        ),
    }


def cmd_verify(args: argparse.Namespace) -> int:
    config = _read_config(args.config)
    workspace = Path(args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    documents, parse_failures = _parse_corpus(args.corpus)
    requests = _load_requests(args.queries)

    combos: list[dict[str, Any]] = []
    for level in (1, 2):
        for store_positions in (False, True):
            combo_workspace = (
                workspace / "combos" / f"L{level}-{'pos' if store_positions else 'nopos'}"
            )
            combos.append(_run_consistency_combo(
                config,
                combo_workspace,
                documents,
                requests,
                level,
                store_positions,
                args.batch_size,
                parse_failures,
            ))

    baseline_layout = IndexLayout(
        workspace / "switch", workspace / "switch" / "snapshots"
    )
    reset_dirs(baseline_layout)
    baseline_config = config.with_format(1, True)
    baseline_builder = IndexBuilder(baseline_config, baseline_layout)
    baseline_builder.build(documents)
    baseline_builder.restore_signal_handlers()
    switch_to_level2 = _switch_format(
        baseline_config, baseline_layout, 2, True, documents,
    )
    switch_to_nopos = _switch_format(
        config.with_format(2, True), baseline_layout, 2, False, documents,
    )
    switch_back = _switch_format(
        config.with_format(2, False), baseline_layout, 1, True, documents,
    )

    current_config = config.with_format(1, True)
    engine_factory = lambda: QueryEngine(
        baseline_layout, budget_ms=config.latency_budget_ms
    )
    serial_run = run_serial(requests, engine_factory)
    concurrent_run = run_concurrent(
        requests, engine_factory, config.query_concurrency,
    )
    concurrency_comparison = compare_query_runs(serial_run, concurrent_run)

    report = {
        "config": json.loads(json.dumps(config.__dict__, ensure_ascii=False)),
        "parse_failures": parse_failures,
        "consistency_combos": [
            {
                "compression_level": combo["compression_level"],
                "store_positions": combo["store_positions"],
                "incremental_version": combo["incremental_version"],
                "full_version": combo["full_version"],
                "versions_equal": combo["incremental_version"]
                == combo["full_version"],
                "query_comparison": combo["query_comparison"],
                "incremental_batches": combo["incremental_batches"],
            }
            for combo in combos
        ],
        "format_switch": {
            "L1pos_to_L2pos": switch_to_level2,
            "L2pos_to_L2nopos": switch_to_nopos,
            "L2nopos_to_L1pos": switch_back,
        },
        "concurrency": {
            "max_concurrency": config.query_concurrency,
            "comparison": concurrency_comparison,
            "serial": serial_run.to_dict(),
            "concurrent": concurrent_run.to_dict(),
        },
        "sample_queries_detail": combos[0]["full_results"],
    }
    write_report(args.report, report)
    print(json.dumps({
        "status": "ok",
        "report": args.report,
        "all_consistent": all(
            combo["query_comparison"]["equal"] for combo in combos
        )
        and concurrency_comparison["equal"],
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    config = _read_config(args.config)
    layout = IndexLayout(args.output_dir)
    requests = _load_requests(args.queries)
    result = run_serial(
        requests,
        lambda: QueryEngine(layout, budget_ms=config.latency_budget_ms),
    )
    write_report(args.report, result.to_dict())
    print(json.dumps({"status": "ok", "report": args.report},
                     ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workbench",
        description="本地确定性语料索引构建与倒排压缩工作台",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="生成确定性样本")
    gen.add_argument("--sample-dir", required=True)
    gen.add_argument("--seed", type=int, default=20261117)
    gen.add_argument("--doc-count", type=int, default=120)
    gen.add_argument("--vocab-size", type=int, default=60)
    gen.set_defaults(func=cmd_generate)

    build = sub.add_parser("build", help="构建索引")
    build.add_argument("--config", required=True)
    build.add_argument("--corpus", required=True)
    build.add_argument("--output-dir", required=True)
    build.add_argument("--snapshot-dir")
    build.add_argument("--report", required=True)
    build.add_argument("--clean", action="store_true")
    build.set_defaults(func=cmd_build)

    query = sub.add_parser("query", help="执行查询集")
    query.add_argument("--config", required=True)
    query.add_argument("--queries", required=True)
    query.add_argument("--output-dir", required=True)
    query.add_argument("--report", required=True)
    query.set_defaults(func=cmd_query)

    verify = sub.add_parser("verify", help="执行完整验收流水线")
    verify.add_argument("--config", required=True)
    verify.add_argument("--corpus", required=True)
    verify.add_argument("--queries", required=True)
    verify.add_argument("--workspace", required=True)
    verify.add_argument("--report", required=True)
    verify.add_argument("--batch-size", type=int, default=12)
    verify.set_defaults(func=cmd_verify)

    incr = sub.add_parser("incremental", help="可中断的增量构建")
    incr.add_argument("--config", required=True)
    incr.add_argument("--corpus", required=True)
    incr.add_argument("--output-dir", required=True)
    incr.add_argument("--snapshot-dir")
    incr.add_argument("--report", required=True)
    incr.add_argument("--batch-size", type=int, default=12)
    incr.add_argument("--clean", action="store_true")
    incr.set_defaults(func=cmd_incremental)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except WorkbenchError as exc:
        failure = make_failure(
            exc.identifier or "cli",
            exc.stage,
            exc.code,
            exc,
        ).to_dict()
        print(json.dumps({"status": "failed", "failure": failure},
                         ensure_ascii=False, indent=2),
              file=sys.stderr)
        return 2


def cmd_incremental(args: argparse.Namespace) -> int:
    """逐批增量构建；收到 SIGTERM/SIGINT 时完成当前批次原子提交后退出。"""
    config = _read_config(args.config)
    layout = IndexLayout(args.output_dir, args.snapshot_dir)
    if args.clean:
        reset_dirs(layout)
    documents, parse_failures = _parse_corpus(args.corpus)
    builder = IndexBuilder(config, layout)
    summaries: list[dict[str, Any]] = []
    exit_code = 0
    for start in range(0, len(documents), args.batch_size):
        try:
            result = builder.build(documents[start : start + args.batch_size],
                                   total_docs=len(documents))
            summaries.append({
                "batch_start": start,
                "reused": len(result.reused_blocks),
                "rebuilt": len(result.rebuilt_blocks),
            })
        except GracefulStopRequested:
            exit_code = 0
            break
    builder.restore_signal_handlers()
    write_report(args.report, {
        "exit_code": exit_code,
        "stopped": exit_code == 0 and len(summaries) * args.batch_size < len(documents),
        "batches": summaries,
        "parse_failures": parse_failures,
        "manifest": load_json(layout.manifest_path) if layout.manifest_path.exists() else None,
    })
    print(json.dumps({"status": "stopped" if exit_code == 0 and summaries else "ok",
                      "batches": len(summaries)}, ensure_ascii=False))
    return exit_code
