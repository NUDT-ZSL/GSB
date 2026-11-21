"""Interactive local terminal application for activity editing and replay."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_config
from .errors import BusinessError
from .session import AnalysisSession, RunResult


ROOT = Path(__file__).resolve().parents[1]


def _default_config_path() -> str:
    working_tree_config = Path.cwd() / "config" / "app.json"
    if working_tree_config.exists():
        return str(working_tree_config)
    return str(ROOT / "config" / "app.json")


def render_status(session: AnalysisSession) -> str:
    snapshot = session.snapshot_status()
    result = session.last_result
    lines = [
        "Meeting VAD Console",
        f"snapshot_version={snapshot.get('version')} entries={snapshot.get('entry_count')}",
    ]
    if result is None:
        lines.append("nodes: not analyzed")
    else:
        lines.append(
            f"recomputed={','.join(result.recomputed_nodes) or '-'} "
            f"reused={','.join(result.reused_nodes) or '-'}"
        )
        lines.append(
            f"intervals recomputed={result.interval_recompute_count} "
            f"reused={result.interval_reuse_count} "
            f"export_items={result.export_item_count}"
        )
        lines.append(f"export_cache_key={result.export_cache_key}")
        lines.append(
            f"self_check_ok={result.self_check['ok']} "
            f"snapshot_replaced={result.snapshot_replaced}"
        )
        for node in ("envelope", "vad", "refine", "speaker", "export"):
            status = result.node_statuses[node]
            lines.append(
                f"  {node:8s} {status['status']:10s} key={status['cache_key'][:12]} "
                f"version={status['node_version']}"
            )
    if session.last_error:
        lines.append(
            f"last_error={session.last_error['code']}: {session.last_error['message']}"
        )
    else:
        lines.append("last_error=-")
    return "\n".join(lines)


def _print_result(result: RunResult) -> None:
    print(
        json.dumps(
            {
                "ok": True,
                "snapshot_version": result.snapshot_version,
                "snapshot_replaced": result.snapshot_replaced,
                "recomputed_nodes": result.recomputed_nodes,
                "reused_nodes": result.reused_nodes,
                "affected_nodes": result.affected_nodes,
                "affected_ranges": result.affected_ranges,
                "interval_recompute_count": result.interval_recompute_count,
                "interval_reuse_count": result.interval_reuse_count,
                "export_cache_key": result.export_cache_key,
                "export_jsonl_sha256": result.export_jsonl_sha256,
                "export_item_count": result.export_item_count,
                "self_check": result.self_check,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def _print_error(error: BusinessError) -> None:
    print(json.dumps(error.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))


def _print_replay(session: AnalysisSession, cache_key: str | None) -> None:
    replay = session.replay_export(cache_key)
    print(
        json.dumps(
            {
                "ok": True,
                "cache_key": replay.cache_key,
                "snapshot_version": replay.snapshot_version,
                "item_count": len(replay.items),
                "jsonl_sha256": replay.record["jsonl_sha256"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    for item in replay.items:
        print(
            f"{item['clip_id']} {item['segment_id']} "
            f"{item['start_ms']:.3f}-{item['end_ms']:.3f}ms "
            f"{item['speaker_label']} rev={item['label_revision']} "
            f"conf=[{item['confidence_lower']:.6f},{item['confidence_upper']:.6f}]"
        )


def _run_command(session: AnalysisSession, line: str) -> bool:
    parts = line.strip().split()
    if not parts:
        return True
    command = parts[0].lower()
    try:
        if command in {"quit", "exit", "q"}:
            return False
        if command == "help":
            print(
                "commands: status | analyze | edit <segment_id> <speaker_label> [revision] | "
                "quantile <q> | frame <frame_ms> <hop_ms> | replay [cache_key] | "
                "replays | key <node> | snapshot | last | export | quit"
            )
        elif command == "status":
            print(render_status(session))
        elif command == "analyze":
            _print_result(session.analyze())
            print(render_status(session))
        elif command == "edit":
            if len(parts) < 3:
                raise ValueError("usage: edit <segment_id> <speaker_label> [revision]")
            revision = int(parts[3]) if len(parts) > 3 else None
            _print_result(
                session.correct_annotation(
                    parts[1],
                    speaker_label=parts[2],
                    label_revision=revision,
                )
            )
        elif command == "quantile":
            if len(parts) != 2:
                raise ValueError("usage: quantile <q>")
            _print_result(session.set_noise_quantile(float(parts[1])))
        elif command == "frame":
            if len(parts) != 3:
                raise ValueError("usage: frame <frame_ms> <hop_ms>")
            _print_result(session.set_frame(float(parts[1]), float(parts[2])))
        elif command == "replay":
            _print_replay(session, parts[1] if len(parts) > 1 else None)
        elif command == "replays":
            records = session.replay.list_records()
            for record in records:
                print(
                    json.dumps(
                        {
                            "export_cache_key": record["export_cache_key"],
                            "snapshot_version": record["snapshot_version"],
                            "item_count": record["item_count"],
                            "jsonl_sha256": record["jsonl_sha256"],
                        },
                        sort_keys=True,
                    )
                )
            if not records:
                print("no replay records")
        elif command == "key":
            if len(parts) != 2:
                raise ValueError("usage: key <node>")
            print(
                json.dumps(
                    session.key_details(parts[1]),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
        elif command == "snapshot":
            print(
                json.dumps(
                    session.snapshot_status(),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
        elif command == "last":
            if session.last_error:
                print(json.dumps(session.last_error, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print("no business error recorded")
        elif command == "export":
            if session.last_result is None:
                print("run analyze first")
            else:
                print(session.last_result.export_jsonl, end="")
        else:
            print(f"unknown command: {command}; type help")
    except BusinessError as error:
        session.last_error = error.to_dict()
        _print_error(error)
    except (TypeError, ValueError) as error:
        print(f"input error: {error}")
    return True


def interactive(session: AnalysisSession) -> int:
    try:
        import readline  # noqa: F401
    except ImportError:
        pass
    print(render_status(session))
    while True:
        try:
            line = input("vad> ")
        except EOFError:
            print()
            break
        if not _run_command(session, line):
            break
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=_default_config_path())
    parser.add_argument(
        "--check",
        action="store_true",
        help="run one analysis, print JSON, and exit",
    )
    parser.add_argument(
        "--replay",
        metavar="CACHE_KEY",
        help="verify and print one recorded export, then exit",
    )
    parser.add_argument(
        "--use-sample-annotations",
        action="store_true",
        help="ignore runtime/annotations.current.jsonl on startup",
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        session = AnalysisSession(
            config, use_runtime_annotations=not args.use_sample_annotations
        )
        if args.replay:
            _print_replay(session, args.replay)
            return 0
        result = session.analyze()
        if args.check:
            payload = result.to_dict()
            payload["status"] = render_status(session)
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        return interactive(session)
    except BusinessError as error:
        _print_error(error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
