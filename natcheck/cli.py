"""Command-line interface for sample generation, checks and verification."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .diff import (
    compare_normalized_versions,
    compare_result_versions,
    compare_with_reference,
)
from .engine import adjudicate
from .errors import SchemaError
from .generator import generate_samples
from .normalize import load_project
from .reference import reference_adjudicate
from .storage import ArtifactStore, file_sha256


class InputError(Exception):
    pass


def _write_json(value: Any, path: str | None, indent: int = 2) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=True) + "\n"
    if path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    sys.stdout.write(text)


def _load(path: str) -> dict[str, Any]:
    return load_project(path)


def _evaluate(
    model: dict[str, Any],
    source_path: str,
    db_path: str,
) -> dict[str, Any]:
    primary = adjudicate(model)
    reference = reference_adjudicate(model)
    check = compare_with_reference(primary, reference)
    source_sha = file_sha256(source_path)
    run_id = None
    if db_path:
        with ArtifactStore(db_path) as store:
            run_id = store.store_run(
                model,
                primary,
                reference,
                check,
                source_path=source_path,
                source_sha256=source_sha,
            )
    return {
        "model": model,
        "primary": primary,
        "reference": reference,
        "reference_check": check,
        "source_path": source_path,
        "source_sha256": source_sha,
        "run_id": run_id,
    }


def command_gen(args: argparse.Namespace) -> int:
    paths = generate_samples(args.out)
    manifest_path = Path(args.out) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _write_json(
        {
            "ok": True,
            "code": "GEN_OK",
            "out": str(Path(args.out).resolve()),
            "paths": paths,
            "manifest": manifest,
        },
        args.output,
        args.indent,
    )
    return 0


def command_run(args: argparse.Namespace) -> int:
    model = _load(args.input)
    evaluated = _evaluate(model, args.input, args.db)
    payload = {
        "ok": bool(evaluated["reference_check"]["ok"]),
        "code": "RUN_OK" if evaluated["reference_check"]["ok"] else "E_REFERENCE",
        "source": args.input,
        "source_schema": model["source_schema"],
        "run_id": evaluated["run_id"],
        "reference_check": evaluated["reference_check"],
        "result": evaluated["primary"],
        "db": args.db,
    }
    if args.with_reference:
        payload["reference_result"] = evaluated["reference"]
    _write_json(payload, args.output, args.indent)
    return 0 if payload["ok"] else 1


def _expand_inputs(items: list[str]) -> list[str]:
    expanded: list[str] = []
    for item in items:
        path = Path(item)
        if path.is_dir():
            candidates = [path / f"v{version}.json" for version in (1, 2, 3)]
            missing = [str(candidate) for candidate in candidates if not candidate.exists()]
            if missing:
                raise InputError(
                    f"directory {path} does not contain all schema samples: "
                    + ", ".join(missing)
                )
            expanded.extend(str(candidate) for candidate in candidates)
        else:
            expanded.append(str(path))
    if not expanded:
        raise InputError("no input projects supplied")
    return expanded


def command_verify(args: argparse.Namespace) -> int:
    sources = _expand_inputs(args.inputs)
    models: dict[str, dict[str, Any]] = {}
    evaluated_by_version: dict[str, dict[str, Any]] = {}
    run_ids: list[str] = []

    for source in sources:
        model = _load(source)
        version = model["source_schema"]
        if version in models:
            raise InputError(f"duplicate schema version supplied: {version}")
        evaluated = _evaluate(model, source, args.db)
        models[version] = model
        evaluated_by_version[version] = evaluated
        if evaluated["run_id"]:
            run_ids.append(evaluated["run_id"])

    if len(models) != 3 or set(models) != {"v1", "v2", "v3"}:
        raise InputError(
            "verify requires exactly one v1, v2 and v3 project; got "
            + ", ".join(sorted(models))
        )

    normalized = compare_normalized_versions(models)
    primary_results = {
        version: item["primary"] for version, item in evaluated_by_version.items()
    }
    result_comparison = compare_result_versions(primary_results)
    reference_checks = {
        version: item["reference_check"]
        for version, item in evaluated_by_version.items()
    }
    reference_ok = all(check["ok"] for check in reference_checks.values())

    first_inconsistency: dict[str, Any] | None = None
    for version in ("v1", "v2", "v3"):
        check = reference_checks[version]
        if not check["ok"]:
            first_inconsistency = {
                "kind": "reference",
                "schema": version,
                "run_id": evaluated_by_version[version]["run_id"],
                **check["first_mismatch"],
            }
            break
    if first_inconsistency is None and normalized["semantic_differences"]:
        first_inconsistency = {
            "kind": "normalization",
            "schema": None,
            **normalized["semantic_differences"][0],
        }
    if first_inconsistency is None and not normalized["equal"]:
        bad_representation = next(
            (
                item
                for item in normalized["representation_differences"]
                if not item["normalized_equal"]
            ),
            None,
        )
        if bad_representation is not None:
            first_inconsistency = {
                "kind": "normalization",
                "schema": None,
                "path": (
                    f"{bad_representation['entity_type']}."
                    f"{bad_representation['entity_id']}."
                    f"{bad_representation['field']}"
                ),
                **bad_representation,
            }
    if first_inconsistency is None and result_comparison["first_mismatch"]:
        first_inconsistency = {
            "kind": "result",
            **result_comparison["first_mismatch"],
        }

    combined_differences = (
        normalized["differences"] + result_comparison["differences"]
    )
    payload = {
        "ok": bool(
            normalized["equal"]
            and result_comparison["equal"]
            and reference_ok
        ),
        "code": (
            "VERIFY_OK"
            if normalized["equal"]
            and result_comparison["equal"]
            and reference_ok
            else "E_INCONSISTENCY"
        ),
        "inputs": sources,
        "versions": sorted(models),
        "normalized_equal": normalized["equal"],
        "result_equal": result_comparison["equal"],
        "reference_ok": reference_ok,
        "reference_checks": [
            {
                "schema": version,
                "run_id": evaluated_by_version[version]["run_id"],
                **reference_checks[version],
            }
            for version in sorted(reference_checks)
        ],
        "cross_version_equivalence": {
            "equal": normalized["equal"],
            "representation_differences": normalized["representation_differences"],
            "semantic_differences": normalized["semantic_differences"],
        },
        "result_comparison": result_comparison,
        "differences": combined_differences,
        "first_inconsistency": first_inconsistency,
        "summary": {
            "session_count_per_version": {
                version: primary_results[version]["summary"]["session_count"]
                for version in sorted(primary_results)
            },
            "impact_events_per_version": {
                version: primary_results[version]["summary"]["impact_events"]
                for version in sorted(primary_results)
            },
            "reference_trace_entries": sum(
                check["checked_trace_entries"]
                for check in reference_checks.values()
            ),
            "representation_difference_count": len(
                normalized["representation_differences"]
            ),
        },
        "run_ids": sorted(run_ids),
        "db": args.db,
    }

    evaluation_id = None
    if args.db:
        with ArtifactStore(args.db) as store:
            evaluation_id = store.store_evaluation(payload, run_ids)
            payload["evaluation_id"] = evaluation_id
            payload["db_counts"] = store.counts()
    else:
        payload["evaluation_id"] = None
        payload["db_counts"] = None

    _write_json(payload, args.output, args.indent)
    return 0 if payload["ok"] else 1


def command_demo(args: argparse.Namespace) -> int:
    sample_paths = generate_samples(args.samples)
    args.inputs = [str(Path(args.samples) / f"v{version}.json") for version in (1, 2, 3)]
    args.output = args.output
    args.db = args.db
    args.indent = args.indent
    return command_verify(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m natcheck",
        description=(
            "Offline NAT mapping and connection-tracking adjudication kernel."
        ),
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation (default: 2)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    gen = subparsers.add_parser("gen", help="generate deterministic v1/v2/v3 samples")
    gen.add_argument(
        "--out",
        default="samples",
        help="sample output directory (default: samples)",
    )
    gen.add_argument("--output", help="also write JSON command report to this path")
    gen.set_defaults(func=command_gen)

    run = subparsers.add_parser("run", help="load one project and run adjudication")
    run.add_argument("input", help="path to a v1, v2 or v3 JSON project")
    run.add_argument(
        "--db",
        default="artifacts/natcheck.sqlite3",
        help="sqlite3 database path (default: artifacts/natcheck.sqlite3)",
    )
    run.add_argument("--output", help="also write JSON result to this path")
    run.add_argument(
        "--with-reference",
        action="store_true",
        help="include the full independent reference result",
    )
    run.set_defaults(func=command_run)

    verify = subparsers.add_parser(
        "verify",
        help="verify all three normalized schemas and the reference adjudicator",
    )
    verify.add_argument(
        "inputs",
        nargs="+",
        help="three schema projects, or one directory containing v1.json/v2.json/v3.json",
    )
    verify.add_argument(
        "--db",
        default="artifacts/natcheck.sqlite3",
        help="sqlite3 database path (default: artifacts/natcheck.sqlite3)",
    )
    verify.add_argument("--output", help="also write the verification report to this path")
    verify.set_defaults(func=command_verify)

    demo = subparsers.add_parser(
        "demo",
        help="generate samples, verify them and persist all artifacts",
    )
    demo.add_argument(
        "--samples",
        default="artifacts/samples",
        help="sample output directory (default: artifacts/samples)",
    )
    demo.add_argument(
        "--db",
        default="artifacts/natcheck.sqlite3",
        help="sqlite3 database path",
    )
    demo.add_argument("--output", help="write the verification report to this path")
    demo.set_defaults(func=command_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SchemaError as exc:
        _write_json(exc.as_dict(), None, args.indent)
        return 3
    except InputError as exc:
        _write_json(
            {
                "ok": False,
                "code": "E_INPUT",
                "reason": str(exc),
            },
            None,
            args.indent,
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())

