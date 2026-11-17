"""Deterministic local sample generator."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
from pathlib import Path
from typing import Any

from .io_utils import atomic_write_json
from .model import STATUS_RANK, compute_sync_marker
from .paths import canonical_path


def _record(
    path: str,
    sources: list[str],
    status: str,
    *,
    version: str = "v2",
    transitive: bool = True,
    policy_id: str = "policy.default",
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": path,
        "sources": sources,
        "masking": status,
        "policy_id": policy_id,
        "version": version,
    }
    if version == "v2":
        record["transitive"] = transitive
    return record


def _dataset(
    dataset_id: str,
    records: list[dict[str, Any]],
    injection: dict[str, Any],
    *,
    seed: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "seed": seed,
        "sync_marker": compute_sync_marker(records, injection),
        "injection": injection,
        "records": records,
    }


def _empty_injection(**extra: Any) -> dict[str, Any]:
    injection: dict[str, Any] = {
        "force_cycle_length": 0,
        "simulate_delay_ms": 0,
        "expect_no_cycle": True,
    }
    injection.update(extra)
    return injection


def build_samples(seed: int = 20261118) -> list[dict[str, Any]]:
    """Return the complete deterministic sample suite."""

    clean_records = [
        _record("account[0].id", [], "plaintext"),
        _record("account[0].id_copy", ["account[0].id"], "plaintext"),
        _record("account[0].masked_root", [], "masked"),
        _record(
            "account[0].masked_copy",
            ["account[0].masked_root"],
            "masked",
        ),
    ]
    clean = _dataset(
        "clean",
        clean_records,
        _empty_injection(expect_clean=True),
        seed=seed,
    )

    order_records = [
        _record("order[0].leaf", ["order[0].masked", "order[0].hidden"], "plaintext"),
        _record("order[0].hidden", ["order[0].seed"], "invisible"),
        _record(
            "order[0].masked",
            ["order[0].seed", "order[0].seed"],
            "masked",
        ),
        _record("order[0].seed", [], "plaintext"),
    ]
    order_independence = _dataset(
        "order_independence",
        order_records,
        _empty_injection(
            force_downgrade_at="order[0].masked",
            force_leak_from="order[0].hidden",
        ),
        seed=seed,
    )

    cycle_length = 4
    statuses = ["plaintext", "masked", "invisible", "masked"]
    cycle_records = [
        _record(
            f"cycle[{index}].value",
            [f"cycle[{(index + 1) % cycle_length}].value"],
            statuses[index],
        )
        for index in range(cycle_length)
    ]
    cycle = _dataset(
        "cycle_injection",
        cycle_records,
        {
            "force_cycle_length": cycle_length,
            "simulate_delay_ms": 0,
        },
        seed=seed,
    )

    version_records = [
        _record(
            "mix[0].legacy",
            [],
            "plaintext",
            version="v1",
        ),
        _record(
            "mix[0].modern",
            [],
            "masked",
            version="v2",
            transitive=True,
        ),
        _record(
            "mix[0].out",
            ["mix[0].legacy", "mix[0].modern"],
            "masked",
        ),
    ]
    version_mix = _dataset(
        "version_mix",
        version_records,
        _empty_injection(
            forge_version_mix_at="mix[0].out",
            force_downgrade_at="mix[0].out",
        ),
        seed=seed,
    )

    downgrade_records = [
        _record("policy[0].secret", [], "plaintext"),
        _record("policy[0].view", ["policy[0].secret"], "masked"),
        _record(
            "policy[0].closed",
            [],
            "invisible",
            transitive=False,
        ),
        _record("policy[0].copy", ["policy[0].closed"], "plaintext"),
    ]
    downgrade_leak = _dataset(
        "downgrade_leak",
        downgrade_records,
        _empty_injection(
            force_downgrade_at="policy[0].view",
            force_leak_from="policy[0].closed",
        ),
        seed=seed,
    )

    depth = 16
    deep_records = [
        _record(
            f"deep[{index}].value",
            [] if index == 0 else [f"deep[{index - 1}].value"],
            "plaintext" if index == 0 else "masked",
        )
        for index in range(depth)
    ]
    deep_paths = [f"deep[{index}].value" for index in range(depth)]
    deep_chain = _dataset(
        "deep_chain",
        deep_records,
        _empty_injection(
            deep_chain={
                "paths": deep_paths,
                "expect_effective": "plaintext",
            },
            force_downgrade_at=deep_paths[-1],
        ),
        seed=seed,
    )

    timeout_records = [
        _record("timeout[0].root", [], "masked"),
        _record("timeout[0].copy", ["timeout[0].root"], "masked"),
    ]
    timeout_dataset = _dataset(
        "timeout_injection",
        timeout_records,
        _empty_injection(simulate_delay_ms=250),
        seed=seed,
    )

    padding_fields = 40
    fields_records = [_record("large[0].root", [], "masked", transitive=False)]
    fields_records.extend(
        _record(
            f"large[0].padding[{index}]",
            [],
            "masked",
            transitive=False,
        )
        for index in range(padding_fields)
    )
    fields_limit = _dataset(
        "fields_limit",
        fields_records,
        _empty_injection(padding_fields=padding_fields),
        seed=seed,
    )

    self_cycle = _dataset(
        "self_cycle",
        [_record("self.cycle", ["self.cycle"], "masked")],
        {
            "force_cycle_length": 1,
            "simulate_delay_ms": 0,
        },
        seed=seed,
    )

    return [
        clean,
        order_independence,
        cycle,
        version_mix,
        downgrade_leak,
        deep_chain,
        timeout_dataset,
        fields_limit,
        self_cycle,
    ]


def build_injected_dataset(
    injection: dict[str, Any],
    *,
    dataset_id: str = "injected",
    seed: int = 20261118,
) -> dict[str, Any]:
    """Build a description set from explicit injection controls.

    Supported controls:
    - force_cycle_length: positive ring length (1 creates a self-cycle)
    - force_downgrade_at: target path for a plaintext -> masked derivation
    - forge_version_mix_at: target path with one v1 and one v2 direct source
    - deep_chain: {prefix, depth} or {paths, expect_effective}
    """

    if not isinstance(injection, dict):
        raise ValueError("injection must be a JSON object")
    injection_out = copy.deepcopy(injection)
    records: list[dict[str, Any]] = []
    used: set[str] = set()

    def add(
        path: str,
        sources: list[str],
        status: str,
        *,
        version: str = "v2",
        transitive: bool = True,
    ) -> None:
        canonical = canonical_path(path)
        if canonical in used:
            raise ValueError(f"injection generated duplicate path: {canonical}")
        used.add(canonical)
        records.append(
            _record(
                canonical,
                [canonical_path(source) for source in sources],
                status,
                version=version,
                transitive=transitive,
            )
        )

    cycle_length = injection.get("force_cycle_length", 0)
    if cycle_length is not None and (
        not isinstance(cycle_length, int) or isinstance(cycle_length, bool)
    ):
        raise ValueError("force_cycle_length must be an integer")
    if isinstance(cycle_length, int):
        if cycle_length < 0:
            raise ValueError("force_cycle_length must be non-negative")
        if cycle_length:
            prefix = str(injection.get("cycle_prefix", "injected.cycle"))
            cycle_paths = [
                canonical_path(f"{prefix}[{index}].value")
                for index in range(cycle_length)
            ]
            for index, path in enumerate(cycle_paths):
                add(
                    path,
                    [cycle_paths[(index + 1) % cycle_length]],
                    "masked",
                )

    downgrade_target = injection.get("force_downgrade_at")
    if downgrade_target is not None and not isinstance(downgrade_target, str):
        raise ValueError("force_downgrade_at must be a path string")
    if isinstance(downgrade_target, str):
        target = canonical_path(downgrade_target)
        source = canonical_path(
            injection.get("force_downgrade_from", f"{target}.source")
        )
        add(source, [], "plaintext")
        add(target, [source], "masked")

    version_target = injection.get("forge_version_mix_at")
    if version_target is not None and not isinstance(version_target, str):
        raise ValueError("forge_version_mix_at must be a path string")
    if isinstance(version_target, str):
        target = canonical_path(version_target)
        legacy = canonical_path(f"{target}.legacy")
        modern = canonical_path(f"{target}.modern")
        add(legacy, [], "masked", version="v1")
        add(modern, [], "masked")
        add(target, [legacy, modern], "masked")

    deep = injection.get("deep_chain")
    if deep is not None and not isinstance(deep, dict):
        raise ValueError("deep_chain must be an object")
    if isinstance(deep, dict):
        supplied_paths = deep.get("paths")
        if isinstance(supplied_paths, list) and supplied_paths:
            deep_paths = [
                canonical_path(path) for path in supplied_paths if isinstance(path, str)
            ]
            if len(deep_paths) != len(supplied_paths):
                raise ValueError("deep_chain.paths must contain strings")
        else:
            prefix = str(deep.get("prefix", "injected.deep"))
            depth = deep.get("depth", 6)
            if not isinstance(depth, int) or isinstance(depth, bool) or depth <= 0:
                raise ValueError("deep_chain.depth must be a positive integer")
            deep_paths = [
                canonical_path(f"{prefix}[{index}].value")
                for index in range(depth)
            ]
        initial_status = str(deep.get("initial_status", "plaintext"))
        rest_status = str(deep.get("rest_status", "masked"))
        if initial_status not in STATUS_RANK or rest_status not in STATUS_RANK:
            raise ValueError("deep_chain statuses must be plaintext/masked/invisible")
        for index, path in enumerate(deep_paths):
            add(
                path,
                [] if index == 0 else [deep_paths[index - 1]],
                initial_status if index == 0 else rest_status,
            )
        expected_status = max(
            [initial_status, *([rest_status] if len(deep_paths) > 1 else [])],
            key=lambda status: STATUS_RANK[status],
        )
        injection_out["deep_chain"] = {
            "paths": deep_paths,
            "expect_effective": expected_status,
        }

    delay_ms = injection.get("simulate_delay_ms", 0)
    if (
        not isinstance(delay_ms, int)
        or isinstance(delay_ms, bool)
        or delay_ms < 0
    ):
        raise ValueError("simulate_delay_ms must be a non-negative integer")

    if not records:
        add("injected.root", [], "masked", transitive=False)

    return _dataset(dataset_id, records, injection_out, seed=seed)


def _prepare_output(destination: Path, clean: bool) -> None:
    if destination == destination.parent:
        raise ValueError("refusing to generate into a filesystem root")
    if clean and destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)


def _safe_filename(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in value
    )
    return cleaned.strip("._") or "injected"


def generate(
    out_dir: str | Path,
    *,
    seed: int = 20261118,
    clean: bool = False,
) -> dict[str, Any]:
    destination = Path(out_dir).resolve()
    _prepare_output(destination, clean)
    dataset_dir = destination / "datasets"
    dataset_dir.mkdir(parents=True, exist_ok=True)

    relative_paths: list[str] = []
    for dataset in build_samples(seed):
        relative = f"datasets/{dataset['dataset_id']}.json"
        atomic_write_json(destination / relative, dataset)
        relative_paths.append(relative)

    manifest = {
        "schema_version": 1,
        "generator": "sensitive_guard.generator",
        "seed": seed,
        "datasets": relative_paths,
    }
    atomic_write_json(destination / "batch.json", manifest)
    return manifest


def generate_injected(
    out_dir: str | Path,
    injection: dict[str, Any],
    *,
    dataset_id: str = "injected",
    seed: int = 20261118,
    clean: bool = False,
) -> dict[str, Any]:
    destination = Path(out_dir).resolve()
    _prepare_output(destination, clean)
    dataset_dir = destination / "datasets"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    dataset = build_injected_dataset(
        injection,
        dataset_id=dataset_id,
        seed=seed,
    )
    relative = f"datasets/{_safe_filename(dataset_id)}.json"
    atomic_write_json(destination / relative, dataset)
    manifest = {
        "schema_version": 1,
        "generator": "sensitive_guard.generator",
        "seed": seed,
        "injection_source": "injection-file",
        "datasets": [relative],
    }
    atomic_write_json(destination / "batch.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--seed", type=int, default=20261118)
    parser.add_argument(
        "--clean",
        action="store_true",
        help="remove the output directory before generating",
    )
    parser.add_argument(
        "--injection-file",
        help="JSON object with explicit injection controls",
    )
    parser.add_argument("--dataset-id", default="injected")
    args = parser.parse_args(argv)
    try:
        if args.injection_file:
            with open(args.injection_file, "r", encoding="utf-8") as handle:
                document = json.load(handle)
            if not isinstance(document, dict):
                raise ValueError("injection file must contain a JSON object")
            injection = document.get("injection", document)
            dataset_id = str(document.get("dataset_id", args.dataset_id))
            manifest = generate_injected(
                args.out_dir,
                injection,
                dataset_id=dataset_id,
                seed=args.seed,
                clean=args.clean,
            )
        else:
            manifest = generate(args.out_dir, seed=args.seed, clean=args.clean)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "out_dir": str(Path(args.out_dir).resolve()),
                "dataset_count": len(manifest["datasets"]),
                "batch": str((Path(args.out_dir).resolve() / "batch.json")),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
