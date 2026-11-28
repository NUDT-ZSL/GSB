"""Command-line entry points for the offline MoE capacity demo."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .generator import generate_project_set, write_project_set
from .normalize import normalize_file
from .verify import verify_project_set


def _fmt_bytes(value: int | float) -> str:
    number = float(value)
    absolute = abs(number)
    if absolute >= 1024 * 1024:
        return f"{number / (1024 * 1024):.2f} MiB"
    if absolute >= 1024:
        return f"{number / 1024:.2f} KiB"
    return f"{int(round(number))} B"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _schema_paths(directory: str | Path) -> dict[str, Path]:
    root = Path(directory)
    paths = {
        version: root / f"project_{version}.json"
        for version in ("v1", "v2", "v3")
    }
    for path in paths.values():
        if not path.exists():
            raise FileNotFoundError(f"missing schema file: {path}")
    return paths


def _prepare_demo(
    seed: int,
    request_count: int,
    out_dir: Path,
    schemas_dir: Path | None,
) -> dict[str, Path]:
    if schemas_dir is None:
        documents = generate_project_set(seed=seed, request_count=request_count)
        return {
            version: Path(path)
            for version, path in write_project_set(
                documents, out_dir / "schemas"
            ).items()
        }
    return _schema_paths(schemas_dir)


def _render_summary(
    verification: dict[str, Any],
    *,
    seed: int | None,
    request_count: int,
) -> str:
    lines: list[str] = [
        "MoE capacity planning demo",
        (
            f"seed={seed if seed is not None else 'n/a'} "
            f"requests={request_count} sample_unit="
            f"{verification['metadata']['sample_equivalent_bytes']}B "
            f"seq_quantum={verification['metadata']['reference_sequence_length']}"
        ),
        "",
        "Normalized model",
    ]
    for version in ("v1", "v2", "v3"):
        fingerprint = verification["cross_version"]["canonical_fingerprints"][version]
        lines.append(
            f"  {version}: project_{version}.json fingerprint={fingerprint[:16]}"
        )
    alignments = len(
        verification["cross_version"]["resolved_normalization_differences"]
    )
    aspects: dict[str, int] = {}
    for item in verification["cross_version"]["resolved_normalization_differences"]:
        aspects[item["aspect"]] = aspects.get(item["aspect"], 0) + 1
    aspect_text = ", ".join(
        f"{key}={value}" for key, value in sorted(aspects.items())
    )
    lines.append(f"  resolved normalization evidence entries: {alignments} ({aspect_text})")

    v1 = verification["versions"]["v1"]
    route = v1["route"]
    traces = {
        trace["request_id"]: trace for trace in route["request_traces"]
    }
    bottleneck_ids = {
        row["replica_id"]
        for row in route["after_rebalance"]["metrics"]["bottleneck_replicas"]
    }
    lines.extend(["", "Per-request routing (v1, final after rebalance)"])
    for assignment in route["assignments"]:
        request_id = assignment["request_id"]
        trace = traces[request_id]
        candidates = ",".join(
            expert["expert_id"] for expert in trace["candidate_experts"]
        )
        marker = "  " if not trace["changed_since_arrival"] else "* "
        lines.append(
            f"{marker}{request_id} t={trace['arrival_time']:<4d} "
            f"{trace['domain']:<7s} s={trace['sample_units']:<3d} "
            f"blk={trace['sequence_blocks']} cand={candidates:<5s} -> "
            f"{assignment['expert_id']}@{assignment['target_replica']} "
            f"(home={assignment['home_replica']}) "
            f"C={_fmt_bytes(assignment['compute_load_bytes'])} "
            f"X={_fmt_bytes(assignment['billed_communication_bytes'])} "
            f"W={_fmt_bytes(assignment['weighted_load_bytes'])}"
        )

    lines.extend(["", "Replica ledger (final)"])
    for replica in route["after_rebalance"]["ledger"]["replicas"]:
        marker = " BOTTLENECK" if replica["replica_id"] in bottleneck_ids else ""
        lines.append(
            f"  {replica['replica_id']} rack={replica['rack']} "
            f"load={_fmt_bytes(replica['weighted_load_bytes'])}/"
            f"{_fmt_bytes(replica['capacity_bytes'])} "
            f"util={replica['utilization'] * 100:.2f}% "
            f"compute={_fmt_bytes(replica['compute_load_bytes'])} "
            f"comm={_fmt_bytes(replica['billed_communication_bytes'])} "
            f"same_rack_avoided="
            f"{_fmt_bytes(replica['same_rack_communication_avoided_bytes'])}"
            f"{marker}"
        )
        for expert in replica["experts"]:
            lines.append(
                f"    {expert['expert_id']}: reqs={','.join(expert['request_ids'])} "
                f"samples={expert['sample_units']} "
                f"compute={_fmt_bytes(expert['compute_load_bytes'])} "
                f"comm={_fmt_bytes(expert['billed_communication_bytes'])} "
                f"total={_fmt_bytes(expert['weighted_load_bytes'])}"
            )

    before = route["before_rebalance"]["metrics"]
    after = route["after_rebalance"]["metrics"]

    def metric_line(label: str, metrics: dict[str, Any]) -> str:
        return (
            f"  {label}: max={_fmt_bytes(metrics['max_replica_load_bytes'])} "
            f"mean={_fmt_bytes(metrics['mean_replica_load_bytes'])} "
            f"max/mean={metrics['imbalance_ratio_max_over_mean']:.4f} "
            f"range={metrics['normalized_range']:.4f} "
            f"cv={metrics['coefficient_of_variation']:.4f} "
            f"peak={metrics['peak_utilization'] * 100:.2f}%"
        )

    lines.extend(
        [
            "",
            "Imbalance metrics",
            metric_line("before", before),
            metric_line("after ", after),
            (
                "  after communication: "
                f"max={_fmt_bytes(after['max_replica_communication_bytes'])} "
                f"mean={_fmt_bytes(after['mean_replica_communication_bytes'])} "
                "max/mean="
                f"{after['communication_imbalance_ratio_max_over_mean']:.4f} "
                f"cv={after['communication_coefficient_of_variation']:.4f}"
            ),
            (
                "  after compute:       "
                f"max={_fmt_bytes(after['max_replica_compute_bytes'])} "
                f"mean={_fmt_bytes(after['mean_replica_compute_bytes'])} "
                f"max/mean={after['compute_imbalance_ratio_max_over_mean']:.4f} "
                f"cv={after['compute_coefficient_of_variation']:.4f}"
            ),
            (
                "  bottleneck replicas: "
                + ",".join(
                    row["replica_id"] for row in after["bottleneck_replicas"]
                )
            ),
            "",
            (
                "Global rebalance adjustments: "
                f"{len(route['rebalance_adjustments'])} "
                f"(order={route['global_rebalance_order']})"
            ),
        ]
    )
    for adjustment in route["rebalance_adjustments"]:
        before_route = adjustment["before"]
        after_route = adjustment["after"]
        lines.append(
            f"  {adjustment['request_id']}: "
            f"{before_route['expert_id']}@{before_route['target_replica']}"
            f"/home={before_route['home_replica']} "
            f"({_fmt_bytes(before_route['weighted_load_bytes'])}) -> "
            f"{after_route['expert_id']}@{after_route['target_replica']}"
            f"/home={after_route['home_replica']} "
            f"({_fmt_bytes(after_route['weighted_load_bytes'])})"
        )

    lines.extend(["", "Event recomputation"])
    for event in route["event_results"]:
        descriptor = (
            f"{event.get('from_replica')}->{event.get('to_replica')} "
            f"expert={event.get('expert_id')}"
            if event["kind"] == "migrate_expert"
            else f"replica={event.get('replica_id')}"
        )
        lines.append(
            f"  {event['event_id']} t={event['at']} {event['kind']} "
            f"{descriptor} affected={','.join(event['affected_request_ids']) or '-'} "
            f"dependent={','.join(event['dependent_expert_ids']) or '-'} "
            f"shortfall={_fmt_bytes(event['shortfall_bytes'])} "
            f"fallback={event['fallback_used']} "
            f"capacity_ok={event['feasible_after_event']}"
        )
        for request_id in sorted(
            set(event["assignments_before"]) | set(event["assignments_after"])
        ):
            old = event["assignments_before"].get(request_id)
            new = event["assignments_after"].get(request_id)
            if old == new:
                continue
            old_text = (
                f"{old['expert_id']}@{old['target_replica']}"
                f"/home={old['home_replica']}"
                if old
                else "unassigned"
            )
            new_text = (
                f"{new['expert_id']}@{new['target_replica']}"
                f"/home={new['home_replica']}"
                if new
                else "unassigned"
            )
            lines.append(f"    {request_id}: {old_text} -> {new_text}")
        if event["infeasible_candidates"]:
            first = event["infeasible_candidates"][0]
            lines.append(
                f"    fallback basis: {first['reason']}; "
                f"first shortfall={_fmt_bytes(first['candidate_options'][0]['shortfall_bytes'])}"
            )

    lines.extend(["", "Independent reference allocator"])
    for version in ("v1", "v2", "v3"):
        check = verification["versions"][version]["reference_check"]
        lines.append(
            f"  {version}: agrees={check['agrees']} "
            f"requests={check['requests_checked']} "
            f"ledger_equal={check['ledger_equal']} "
            f"metrics_equal={check['metrics_equal']} "
            f"first_mismatch={check['first_mismatch_request'] or 'none'}"
        )
    cross = verification["cross_version"]
    lines.extend(
        [
            "",
            "Cross-version consistency",
            (
                f"  canonical_models_equal={cross['canonical_models_equal']} "
                f"routes_equal={cross['per_request_routes_equal']} "
                f"ledgers_equal={cross['ledgers_equal']} "
                f"metrics_equal={cross['imbalance_metrics_equal']} "
                f"events_equal={cross['event_recomputations_equal']} "
                f"consistent={cross['consistent']}"
            ),
            (
                "  observable differences: "
                f"{len(cross['observable_differences'])}"
            ),
            "",
            f"Acceptance: {'PASS' if verification['acceptance']['passed'] else 'FAIL'}",
        ]
    )
    for key, value in verification["acceptance"].items():
        if isinstance(value, bool):
            lines.append(f"  {key}={value}")
    return "\n".join(lines) + "\n"


def _write_demo_outputs(
    verification: dict[str, Any],
    out_dir: Path,
    *,
    seed: int | None,
    request_count: int,
) -> None:
    normalized_dir = out_dir / "normalized"
    routes_dir = out_dir / "routes"
    for version in ("v1", "v2", "v3"):
        version_data = verification["versions"][version]
        _write_json(
            normalized_dir / f"{version}.json",
            version_data["normalized"],
        )
        _write_json(
            routes_dir / f"{version}_route.json",
            version_data["route"],
        )
        _write_json(
            routes_dir / f"{version}_reference.json",
            version_data["reference"],
        )
        _write_json(
            routes_dir / f"{version}_reference_check.json",
            version_data["reference_check"],
        )
    _write_json(out_dir / "verification.json", verification)
    (out_dir / "summary.txt").write_text(
        _render_summary(
            verification,
            seed=seed,
            request_count=request_count,
        ),
        encoding="utf-8",
    )
    _write_json(
        out_dir / "manifest.json",
        {
            "artifact_root": str(out_dir),
            "seed": seed,
            "request_count": request_count,
            "versions": ["v1", "v2", "v3"],
            "acceptance_passed": verification["acceptance"]["passed"],
        },
    )


def _command_demo(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    schemas_dir = Path(args.schemas_dir) if args.schemas_dir else None
    paths = _prepare_demo(args.seed, args.requests, out_dir, schemas_dir)
    verification = verify_project_set(paths)
    request_count = int(verification["metadata"]["request_count"])
    seed = None if schemas_dir is not None else args.seed
    if seed is None:
        seed = verification["metadata"].get("seed")
    _write_demo_outputs(
        verification,
        out_dir,
        seed=seed,
        request_count=request_count,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "acceptance": verification["acceptance"],
                    "out": str(out_dir),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif not args.quiet:
        print(
            _render_summary(
                verification,
                seed=seed,
                request_count=request_count,
            ),
            end="",
        )
    return 0 if verification["acceptance"]["passed"] else 1


def _command_verify(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.schemas_dir:
        paths = _schema_paths(args.schemas_dir)
        seed = None
    else:
        documents = generate_project_set(seed=args.seed, request_count=args.requests)
        paths = {
            version: Path(path)
            for version, path in write_project_set(
                documents, out_dir / "schemas"
            ).items()
        }
        seed = args.seed
    verification = verify_project_set(paths)
    request_count = int(verification["metadata"]["request_count"])
    if seed is None:
        seed = verification["metadata"].get("seed")
    _write_demo_outputs(
        verification,
        out_dir,
        seed=seed,
        request_count=request_count,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "acceptance": verification["acceptance"],
                    "out": str(out_dir),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif not args.quiet:
        print(
            _render_summary(
                verification,
                seed=seed,
                request_count=request_count,
            ),
            end="",
        )
    return 0 if verification["acceptance"]["passed"] else 1


def _command_normalize(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for version, source in zip(("v1", "v2", "v3"), args.inputs):
        project = normalize_file(source)
        _write_json(out_dir / f"{version}.json", project.to_dict())
    return 0


def _command_route(args: argparse.Namespace) -> int:
    from .reference import compare_with_reference, run_reference
    from .router import run_allocation

    project = normalize_file(args.schema)
    route = run_allocation(project)
    reference = run_reference(project)
    check = compare_with_reference(route, reference)
    output = Path(args.out)
    _write_json(output / "normalized.json", project.to_dict())
    _write_json(output / "route.json", route)
    _write_json(output / "reference.json", reference)
    _write_json(output / "reference_check.json", check)
    if not args.quiet:
        print(
            json.dumps(
                {
                    "first_mismatch_request": check["first_mismatch_request"],
                    "reference_agrees": check["agrees"],
                    "route_output": str(output / "route.json"),
                },
                indent=2,
            )
        )
    return 0 if check["agrees"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="moe-capacity",
        description=(
            "Offline MoE expert-routing and capacity-balancing demo "
            "(Python standard library only)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser(
        "demo",
        help="generate schemas, normalize, route, verify and write artifacts",
    )
    demo.add_argument("--seed", type=int, default=42)
    demo.add_argument("--requests", type=int, default=24)
    demo.add_argument("--out", default="artifacts/demo")
    demo.add_argument(
        "--schemas-dir",
        default=None,
        help="use existing project_v1/v2/v3.json instead of generating",
    )
    demo.add_argument("--quiet", action="store_true")
    demo.add_argument("--json", action="store_true")
    demo.set_defaults(handler=_command_demo)

    verify = subparsers.add_parser(
        "verify",
        help="run acceptance against an existing schema directory or new sample",
    )
    verify.add_argument("--schemas-dir", default=None)
    verify.add_argument("--seed", type=int, default=42)
    verify.add_argument("--requests", type=int, default=24)
    verify.add_argument("--out", default="artifacts/verify")
    verify.add_argument("--quiet", action="store_true")
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(handler=_command_verify)

    normalize = subparsers.add_parser(
        "normalize",
        help="normalize three explicit schema files",
    )
    normalize.add_argument("inputs", nargs=3, metavar=("V1", "V2", "V3"))
    normalize.add_argument("--out", default="artifacts/normalized")
    normalize.set_defaults(handler=_command_normalize)

    route = subparsers.add_parser(
        "route",
        help="route one schema and compare with the independent reference",
    )
    route.add_argument("schema")
    route.add_argument("--out", default="artifacts/route")
    route.add_argument("--quiet", action="store_true")
    route.set_defaults(handler=_command_route)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())

