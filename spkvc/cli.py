"""Command-line interface for the reproducible experiment environment."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import PipelineConfig
from .dataset import Dataset, generate_dataset
from .errors import BusinessError
from .pipeline import ConversionEngine


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _parse_assignments(values: list[str], *, numeric: bool) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in values:
        if "=" not in item:
            raise BusinessError(
                "E_CLI_ARGUMENT",
                "参数必须为 key=value 形式",
                field="assignment",
                details={"value": item},
            )
        key, raw = item.split("=", 1)
        if numeric:
            try:
                result[key] = float(raw) if "." in raw else int(raw)
            except ValueError as exc:
                raise BusinessError(
                    "E_CLI_ARGUMENT",
                    "映射参数必须为数字",
                    field=key,
                    details={"value": raw, "reason": str(exc)},
                ) from exc
        else:
            result[key] = raw
    return result


def _engine(args: argparse.Namespace, config: PipelineConfig | None = None) -> ConversionEngine:
    dataset = Dataset(Path(args.data))
    selected = config or dataset.load_config()
    effective_path = Path(args.state_dir) / "run" / "effective_config.json"
    if (
        config is None
        and getattr(args, "command", None)
        in {"run", "selfcheck", "cache", "snapshot", "evaluate"}
        and effective_path.is_file()
    ):
        try:
            effective = json.loads(effective_path.read_text(encoding="utf-8"))
            snapshot_path = Path(args.state_dir) / "run" / "snapshots.json"
            snapshot_signature = None
            if snapshot_path.is_file():
                snapshot_signature = json.loads(
                    snapshot_path.read_text(encoding="utf-8")
                ).get("signature")
            if snapshot_signature == effective.get("signature"):
                selected = PipelineConfig.from_dict(effective["config"])
        except (OSError, KeyError, json.JSONDecodeError, ValueError):
            pass
    return ConversionEngine(
        dataset=dataset,
        config=selected,
        state_dir=args.state_dir,
        cache_dir=args.cache_dir,
        output_dir=getattr(args, "out", None),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spkvc",
        description="Deterministic multi-speaker voice conversion experiment runner",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate deterministic local samples")
    generate.add_argument("--out", default="data", help="dataset directory")
    generate.add_argument("--force", action="store_true")
    generate.add_argument("--speakers", type=int, default=4)
    generate.add_argument("--revisions", default="1,2")

    run = subparsers.add_parser("run", help="run or resume the five-stage pipeline")
    run.add_argument("--data", default="data")
    run.add_argument("--state-dir", default=".spkvc")
    run.add_argument("--cache-dir", default=None)
    run.add_argument("--out", default=None)
    run.add_argument("--crash-after-stage", type=int, default=None)
    run.add_argument("--crash-mid-stage", type=int, default=None)
    run.add_argument("--crash-after-utterances", type=int, default=0)
    run.add_argument("--summary-json", action="store_true")

    incremental = subparsers.add_parser(
        "incremental", help="apply a reference-revision or mapping-only increment"
    )
    incremental.add_argument("--data", default="data")
    incremental.add_argument("--state-dir", default=".spkvc")
    incremental.add_argument("--cache-dir", default=None)
    incremental.add_argument("--out", default=None)
    incremental.add_argument(
        "--ref-revision",
        action="append",
        default=[],
        metavar="REF_ID=REV",
        help="new reference revision, repeated per reference",
    )
    incremental.add_argument("--mapping-version", default=None)
    incremental.add_argument(
        "--mapping-param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a numeric mapping parameter",
    )
    incremental.add_argument("--crash-mid-stage", type=int, default=None)
    incremental.add_argument("--crash-after-utterances", type=int, default=0)
    incremental.add_argument("--summary-json", action="store_true")

    cache = subparsers.add_parser("cache", help="show keys and artifact hashes")
    cache.add_argument("--data", default="data")
    cache.add_argument("--state-dir", default=".spkvc")
    cache.add_argument("--cache-dir", default=None)
    cache.add_argument("--utterance-id", required=True)

    selfcheck = subparsers.add_parser(
        "selfcheck", help="run consistency checks after a complete conversion"
    )
    selfcheck.add_argument("--data", default="data")
    selfcheck.add_argument("--state-dir", default=".spkvc")
    selfcheck.add_argument("--cache-dir", default=None)
    selfcheck.add_argument("--out", default=None)

    snapshot = subparsers.add_parser("snapshot", help="inspect snapshot metadata")
    snapshot.add_argument("--data", default="data")
    snapshot.add_argument("--state-dir", default=".spkvc")
    snapshot.add_argument("--cache-dir", default=None)

    evaluate = subparsers.add_parser(
        "evaluate", help="observe truth-based errors for committed results"
    )
    evaluate.add_argument("--data", default="data")
    evaluate.add_argument("--state-dir", default=".spkvc")
    evaluate.add_argument("--cache-dir", default=None)
    evaluate.add_argument("--utterance-id", action="append", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            revisions = tuple(
                sorted({int(item) for item in args.revisions.split(",") if item})
            )
            dataset = generate_dataset(
                args.out,
                force=args.force,
                speaker_count=args.speakers,
                revisions=revisions,
            )
            _print_json(
                {
                    "ok": True,
                    "dataset_root": str(dataset.root),
                    "manifest": str(dataset.manifest_path),
                    "utterances": len(dataset.load_entries()),
                }
            )
            return 0

        if args.command == "run":
            engine = _engine(args)
            result = engine.run(
                crash_after_stage=args.crash_after_stage,
                crash_mid_stage=args.crash_mid_stage,
                crash_after_utterances=args.crash_after_utterances,
            )
            summary = result.summary()
            summary["output_dir"] = str(engine.output_dir)
            _print_json(summary)
            return 0

        if args.command == "incremental":
            engine = _engine(args)
            revisions: dict[str, int] = {}
            for item in args.ref_revision:
                if "=" not in item:
                    raise BusinessError(
                        "E_CLI_ARGUMENT",
                        "--ref-revision 必须为 REF_ID=REV",
                        field="ref_revision",
                        details={"value": item},
                    )
                ref_id, raw = item.split("=", 1)
                revisions[ref_id] = int(raw)
            mapping_params = _parse_assignments(args.mapping_param, numeric=True)
            if args.mapping_version or mapping_params:
                value = engine.config.to_dict()
                if args.mapping_version:
                    value["mapping_version"] = args.mapping_version
                value["mapping_params"] = {
                    **value["mapping_params"],
                    **mapping_params,
                }
                config = PipelineConfig.from_dict(value)
            else:
                config = None
            result = engine.incremental(
                ref_revisions=revisions,
                new_config=config,
                crash_mid_stage=args.crash_mid_stage,
                crash_after_utterances=args.crash_after_utterances,
            )
            _print_json(result.summary())
            return 0

        if args.command == "cache":
            engine = _engine(args)
            _print_json(engine.cache_report(args.utterance_id))
            return 0

        if args.command == "selfcheck":
            engine = _engine(args)
            _print_json(engine.self_check_snapshots())
            return 0

        if args.command == "snapshot":
            engine = _engine(args)
            state = engine.snapshots.load()
            _print_json(
                {
                    "ok": True,
                    "signature": state.signature,
                    "version_counter": state.version_counter,
                    "entries": [entry.to_dict() for entry in state.entries],
                }
            )
            return 0

        if args.command == "evaluate":
            engine = _engine(args)
            _print_json(engine.evaluate(args.utterance_id))
            return 0

        parser.error(f"unknown command: {args.command}")
        return 2
    except BusinessError as exc:
        _print_json(exc.to_dict())
        return 2
    except ValueError as exc:
        _print_json(
            BusinessError(
                "E_CLI_ARGUMENT",
                str(exc),
                field="arguments",
            ).to_dict()
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())

