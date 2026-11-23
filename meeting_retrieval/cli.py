"""Command line and interactive shell for the local retrieval workspace."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .engine import ForcedTermination, RunOutputs
from .errors import BusinessError
from .service import MeetingRetrievalApp

DEFAULT_WORKSPACE = ".meeting_retrieval_workspace"


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _print_run(output: RunOutputs, as_json: bool = False) -> None:
    payload = output.to_dict()
    if as_json:
        print(_json_text(payload))
        return
    print(f"run_id: {output.run_id}")
    recovery = output.recovery
    print(
        "recovery: last={last} resume_from={resume} complete={complete}".format(
            last=recovery.get("last_committed_stage"),
            resume=recovery.get("resume_from_stage"),
            complete=recovery.get("complete"),
        )
    )
    print("affected coverage:")
    for node in ("decode", "visual", "text", "align", "ranking"):
        values = output.affected.get(node, [])
        print(f"  {node:8s}: {', '.join(values) if values else '-'}")
    print(f"  pairs   : {', '.join(output.affected.get('pair_ids', [])) or '-'}")
    reasons = output.affected.get("reasons", {})
    changed = [key for key, value in reasons.items() if value]
    print(f"  reasons : {', '.join(changed) if changed else 'no input change'}")

    print(f"recomputed nodes: {len(output.recomputed_nodes)}")
    for item in output.recomputed_nodes:
        print(f"  + {item['node']:8s} {item['pair_id']}")
    print(f"reused nodes: {len(output.reused_nodes)}")
    counts: Dict[str, int] = {}
    for item in output.reused_nodes:
        counts[item["node"]] = counts.get(item["node"], 0) + 1
    print("  " + ", ".join(f"{node}={count}" for node, count in sorted(counts.items())))
    print("ranking:")
    print("  rank  pair  score                       block  hit_id")
    for hit in output.ranking["hits"]:
        print(
            f"  {hit['rank']:>4}  {hit['pair_id']:<5} {hit['score']:<26} "
            f"{hit['best_block_index']:>5}  {hit['hit_id']}"
        )
    print(f"metrics: {_compact(output.ranking['metrics'])}")
    print("stages:")
    for stage, view in output.stages.items():
        print(
            f"  {stage:20s} version={view['version']} "
            f"pairs={len(view['completed_pair_ids'])} checksum={view['checksum'][:16]}..."
        )
    print(
        "retrieval snapshot: version={version} checksum={checksum}".format(
            version=output.retrieval_snapshot["version"],
            checksum=output.retrieval_snapshot["checksum"][:16] + "...",
        )
    )
    print(f"cache key preview: {_compact(output.cache_key_preview)}")
    if output.degradation:
        print(f"degradation: {', '.join(output.degradation)}")


def _compact(value: Dict[str, Any]) -> str:
    return ", ".join(f"{key}={value[key]}" for key in sorted(value))


def _print_hits(hits: List[Dict[str, Any]], as_json: bool) -> None:
    if as_json:
        print(_json_text(hits))
        return
    if not hits:
        print("no hits")
        return
    print("rank  pair  score                       block  hit_id")
    for hit in hits:
        print(
            f"{hit['rank']:>4}  {hit['pair_id']:<5} {hit['score']:<26} "
            f"{hit['best_block_index']:>5}  {hit['hit_id']}"
        )


def _print_status(status: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(_json_text(status))
        return
    print(f"workspace: {status['workspace']}")
    print(f"samples: {status['sample_count']} ({', '.join(status['pair_ids'])})")
    snapshots = status["snapshots"]
    last_run = status.get("last_run")
    if last_run:
        print(f"last_run: {last_run['run_id']}")
        print("  invalidation coverage:")
        for node in ("decode", "visual", "text", "align", "ranking"):
            values = last_run["affected"].get(node, [])
            print(f"    {node:8s}: {', '.join(values) if values else '-'}")
        print(
            f"  recomputed={len(last_run['recomputed_nodes'])} "
            f"reused={len(last_run['reused_nodes'])}"
        )
    else:
        print("last_run: -")
    print("stages:")
    for stage in snapshots["schedule"]:
        committed = next(
            (item for item in snapshots["committed_stages"] if item["stage"] == stage), None
        )
        print(
            f"  {stage:20s} "
            + (
                f"version={committed['version']} pairs={len(committed['completed_pair_ids'])} "
                f"checksum={committed['checksum'][:16]}..."
                if committed
                else "uncommitted"
            )
        )
    print(f"resume_from: {snapshots['resume_from_stage']}")
    print("registries:")
    for kind, record in snapshots["registries"].items():
        if record is None:
            print(f"  {kind:18s} -")
        else:
            print(
                f"  {kind:18s} version={record['version']} revision={record['revision']} "
                f"checksum={record['checksum'][:16]}..."
            )
    retrieval = status.get("retrieval")
    if retrieval:
        print("hits:")
        _print_hits(retrieval["hits"], False)
        print(f"metrics: {_compact(retrieval['metrics'])}")
    else:
        print("hits: -")
    error = status.get("last_error")
    print(f"last_error: {error['code'] if error else '-'}")
    if status.get("degradation"):
        print(f"degradation: {', '.join(status['degradation'])}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m meeting_retrieval")
    parser.add_argument(
        "--workspace",
        default=os.environ.get("MEETING_RETRIEVAL_WORKSPACE", DEFAULT_WORKSPACE),
        help="本地工作区目录（默认 .meeting_retrieval_workspace）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="初始化默认样本、词表、配置与快照目录")
    init_parser.add_argument("--json", action="store_true")

    demo_parser = subparsers.add_parser("demo", help="初始化并执行一次完整检索")
    demo_parser.add_argument("--json", action="store_true")

    run_parser = subparsers.add_parser("run", help="执行/恢复一次增量提交")
    run_parser.add_argument("--force-new-run", action="store_true")
    run_parser.add_argument(
        "--crash-after",
        choices=["visual_features", "text_embeddings", "alignment_scores", "retrieval_ranking"],
    )
    run_parser.add_argument("--json", action="store_true")

    status_parser = subparsers.add_parser("status", help="展示计算图、快照、命中与最近错误")
    status_parser.add_argument("--json", action="store_true")

    topology_parser = subparsers.add_parser("topology", help="展示 DAG 拓扑")
    topology_parser.add_argument("--json", action="store_true")

    hits_parser = subparsers.add_parser("hits", help="展示逐命中明细")
    hits_parser.add_argument("--json", action="store_true")

    trace_parser = subparsers.add_parser("trace", help="按 hit_id 反查来源节点路径与版本")
    trace_parser.add_argument("hit_id")
    trace_parser.add_argument("--json", action="store_true")

    replay_parser = subparsers.add_parser("replay", help="按 hit_id 重放分数与排序")
    replay_parser.add_argument("hit_id")
    replay_parser.add_argument("--json", action="store_true")

    cache_parser = subparsers.add_parser("query-cache", help="查询节点与 pair_id 的缓存键组成")
    cache_parser.add_argument("node", choices=["decode", "visual", "text", "align", "ranking"])
    cache_parser.add_argument("pair_id")
    cache_parser.add_argument("--json", action="store_true")

    text_parser = subparsers.add_parser("update-text", help="修正片段文本并增量重算")
    text_parser.add_argument("pair_id")
    text_parser.add_argument("text", nargs="+")
    text_parser.add_argument("--json", action="store_true")

    seed_parser = subparsers.add_parser("update-seed", help="替换画面像素种子并增量重算")
    seed_parser.add_argument("pair_id")
    seed_parser.add_argument("image_seed", type=int)
    seed_parser.add_argument("--json", action="store_true")

    feature_parser = subparsers.add_parser("set-features", help="调整视觉特征参数并增量重算")
    feature_parser.add_argument("--rows", type=int)
    feature_parser.add_argument("--cols", type=int)
    feature_parser.add_argument("--hist-bins", type=int)
    feature_parser.add_argument("--mean-weight", type=float)
    feature_parser.add_argument("--hist-weight", type=float)
    feature_parser.add_argument("--texture-cell", type=int)
    feature_parser.add_argument("--projection-seed")
    feature_parser.add_argument("--json", action="store_true")

    retrieval_parser = subparsers.add_parser("set-retrieval", help="调整相似度门限与排序参数")
    retrieval_parser.add_argument("--min-score", type=float)
    retrieval_parser.add_argument("--top-k", type=int)
    retrieval_parser.add_argument("--metric")
    retrieval_parser.add_argument("--json", action="store_true")

    vocab_parser = subparsers.add_parser("set-vocab", help="整体替换受控词表")
    vocab_parser.add_argument("--terms", required=True, help="逗号分隔词项")
    vocab_parser.add_argument("--json", action="store_true")

    limits_parser = subparsers.add_parser("set-limits", help="设置资源上限")
    limits_parser.add_argument("--max-pixel-bytes", type=int)
    limits_parser.add_argument("--max-cache-entries", type=int)
    limits_parser.add_argument("--max-recompute-pairs", type=int)
    limits_parser.add_argument("--json", action="store_true")

    check_parser = subparsers.add_parser("selfcheck", help="执行跨模块一致性自检")
    check_parser.add_argument("--json", action="store_true")

    recover_parser = subparsers.add_parser("recover", help="展示阶段恢复点")
    recover_parser.add_argument("--json", action="store_true")

    subparsers.add_parser("app", help="进入本地交互 REPL")
    return parser


def _changes(args: argparse.Namespace, fields: Dict[str, str]) -> Dict[str, Any]:
    payload = {}
    for attribute, key in fields.items():
        value = getattr(args, attribute, None)
        if value is not None:
            payload[key] = value
    return payload


def _run_command(args: argparse.Namespace, app: MeetingRetrievalApp) -> int:
    if args.command == "init":
        payload = app.ensure_workspace()
        print(_json_text(payload) if args.json else f"initialized: {payload['workspace']} ({payload['samples']} samples)")
        return 0
    if args.command == "demo":
        app.ensure_workspace()
        output = app.run()
        _print_run(output, as_json=args.json)
        return 0
    if args.command == "run":
        output = app.run(crash_after=args.crash_after, force_new_run=args.force_new_run)
        _print_run(output, as_json=args.json)
        return 0
    if args.command == "status":
        _print_status(app.status(), as_json=args.json)
        return 0
    if args.command == "topology":
        if args.json:
            print(_json_text(app.engine.topology()))
        else:
            print(app.render_topology())
        return 0
    if args.command == "hits":
        _print_hits(app.hits(), as_json=args.json)
        return 0
    if args.command == "trace":
        result = app.trace_hit(args.hit_id)
        print(_json_text(result))
        return 0
    if args.command == "replay":
        result = app.replay_hit(args.hit_id)
        print(_json_text(result))
        return 0
    if args.command == "query-cache":
        result = app.query_cache(args.node, args.pair_id)
        print(_json_text(result))
        return 0
    if args.command == "update-text":
        output = app.update_text(args.pair_id, " ".join(args.text))
        _print_run(output, as_json=args.json)
        return 0
    if args.command == "update-seed":
        output = app.update_seed(args.pair_id, args.image_seed)
        _print_run(output, as_json=args.json)
        return 0
    if args.command == "set-features":
        changes = _changes(
            args,
            {
                "rows": "tile_rows",
                "cols": "tile_cols",
                "hist_bins": "hist_bins",
                "mean_weight": "mean_weight",
                "hist_weight": "hist_weight",
                "texture_cell": "texture_cell",
                "projection_seed": "projection_seed",
            },
        )
        output = app.set_feature_params(**changes)
        _print_run(output, as_json=args.json)
        return 0
    if args.command == "set-retrieval":
        changes = _changes(args, {"metric": "metric", "min_score": "min_score", "top_k": "top_k"})
        output = app.set_retrieval_params(**changes)
        _print_run(output, as_json=args.json)
        return 0
    if args.command == "set-vocab":
        terms = [term.strip() for term in args.terms.split(",") if term.strip()]
        output = app.set_vocabulary(terms)
        _print_run(output, as_json=args.json)
        return 0
    if args.command == "set-limits":
        changes = _changes(
            args,
            {
                "max_pixel_bytes": "max_pixel_bytes_per_image",
                "max_cache_entries": "max_cache_entries",
                "max_recompute_pairs": "max_recompute_pairs",
            },
        )
        print(_json_text(app.set_resource_limits(**changes)))
        return 0
    if args.command == "selfcheck":
        result = app.self_check()
        print(_json_text(result))
        return 0 if result["ok"] else 1
    if args.command == "recover":
        result = app.recover()
        print(_json_text(result))
        return 0
    if args.command == "app":
        return interactive_shell(app)
    return 1


INTERACTIVE_HELP = """commands:
  status | topology | hits | snapshots | error
  run [crash_after]
  update text <pair_id> <text...>
  update seed <pair_id> <seed>
  features rows=3 cols=4 hist_bins=4 mean_weight=1 hist_weight=1
  retrieval min_score=0.0 top_k=5
  vocab <comma-separated terms>
  limits max_recompute_pairs=128 max_cache_entries=5000
  cache <node> <pair_id>
  trace <hit_id> | replay <hit_id>
  selfcheck | recover | help | quit"""


def interactive_shell(app: MeetingRetrievalApp) -> int:
    print("meeting retrieval interactive shell")
    print(INTERACTIVE_HELP)
    while True:
        try:
            line = input("mr> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in {"quit", "exit", "q"}:
            return 0
        if line in {"help", "?"}:
            print(INTERACTIVE_HELP)
            continue
        try:
            tokens = shlex.split(line)
            command = tokens[0]
            if command == "status":
                _print_status(app.status(), False)
            elif command == "topology":
                print(app.render_topology())
            elif command == "hits":
                _print_hits(app.hits(), False)
            elif command == "snapshots":
                print(_json_text(app.engine.snapshots()))
            elif command == "error":
                print(_json_text(app.last_error) if app.last_error else "no recent business error")
            elif command == "run":
                crash_after = tokens[1] if len(tokens) > 1 else None
                _print_run(app.run(crash_after=crash_after), False)
            elif command == "update" and len(tokens) >= 4 and tokens[1] == "text":
                _print_run(app.update_text(tokens[2], " ".join(tokens[3:])), False)
            elif command == "update" and len(tokens) == 4 and tokens[1] == "seed":
                _print_run(app.update_seed(tokens[2], int(tokens[3])), False)
            elif command == "features":
                changes = _parse_assignments(tokens[1:])
                _print_run(app.set_feature_params(**changes), False)
            elif command == "retrieval":
                changes = _parse_assignments(tokens[1:])
                _print_run(app.set_retrieval_params(**changes), False)
            elif command == "vocab" and len(tokens) >= 2:
                terms = [term.strip() for term in " ".join(tokens[1:]).split(",") if term.strip()]
                _print_run(app.set_vocabulary(terms), False)
            elif command == "limits":
                print(_json_text(app.set_resource_limits(**_parse_assignments(tokens[1:]))))
            elif command == "cache" and len(tokens) == 3:
                print(_json_text(app.query_cache(tokens[1], tokens[2])))
            elif command == "trace" and len(tokens) == 2:
                print(_json_text(app.trace_hit(tokens[1])))
            elif command == "replay" and len(tokens) == 2:
                print(_json_text(app.replay_hit(tokens[1])))
            elif command == "selfcheck":
                print(_json_text(app.self_check()))
            elif command == "recover":
                print(_json_text(app.recover()))
            else:
                print("unknown command; type help")
        except BusinessError as exc:
            print(_json_text(exc.to_dict()))
        except ForcedTermination as exc:
            print(
                _json_text(
                    {
                        "ok": False,
                        "error": {
                            "code": "E_FORCED_TERMINATION",
                            "message": f"强制终止于阶段 {exc.stage}",
                            "details": {"recovery": app.recover()},
                        },
                    }
                )
            )
        except (ValueError, TypeError) as exc:
            print(_json_text({"ok": False, "error": {"code": "E_INPUT_INVALID", "message": str(exc)}}))


def _parse_assignments(tokens: List[str]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for token in tokens:
        if "=" not in token:
            raise ValueError(f"expected key=value, got {token}")
        key, raw = token.split("=", 1)
        try:
            payload[key] = int(raw)
            continue
        except ValueError:
            pass
        try:
            payload[key] = float(raw)
            continue
        except ValueError:
            payload[key] = raw
    return payload


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        app = MeetingRetrievalApp(Path(args.workspace))
        return _run_command(args, app)
    except BusinessError as exc:
        print(_json_text(exc.to_dict()), file=sys.stderr)
        return 2
    except ForcedTermination as exc:
        print(
            _json_text(
                {
                    "ok": False,
                    "error": {
                        "code": "E_FORCED_TERMINATION",
                        "message": f"阶段 {exc.stage} 已提交后模拟强制终止",
                    },
                }
            ),
            file=sys.stderr,
        )
        return 75
