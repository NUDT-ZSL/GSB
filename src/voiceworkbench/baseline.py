"""Serial full-run baseline: replay the identical command sequence from blank.

The baseline shares every deterministic building block with the incremental
engine (same pipeline, same clustering/override code, same selectors) but
starts from an empty :class:`StageCache`, so every stage is computed exactly
once and no incremental reuse ledger is produced.
"""
from __future__ import annotations

from .cache import Ledger, StageCache
from .commands import EditCommand, resolve_selector
from .config import PipelineConfig
from .errors import CommandError
from .materialize import apply_cut, apply_replace, load_material
from .pipeline import Pipeline, PipelineResult


def run_serial_baseline(config: PipelineConfig, source_paths: list[str],
                        commands: list[EditCommand]
                        ) -> tuple[PipelineResult, list[dict[str, Any]]]:
    material = load_material(source_paths)
    threshold_override: float | None = None
    overrides: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    cache = StageCache()
    pipeline = Pipeline(config=config, cache=cache)
    result: PipelineResult | None = None

    # Initial blank-state computation, identical to engine.initialize().
    result = pipeline.run(
        material=material,
        current_config=config,
        threshold_override=None,
        cluster_overrides=[],
        ledger=Ledger(),
        invalidate_reason=None,
    )

    for command in commands:
        params = command.params
        if command.command_type == "set_cluster_threshold":
            threshold_override = float(params["cluster_distance_threshold"])
            reason = "threshold_changed"
        elif command.command_type == "cut_interval":
            try:
                apply_cut(material, params)
            except CommandError as exc:
                rejected.append(_rejection(command, exc))
                continue
            reason = "cut_interval"
        elif command.command_type == "replace_interval_audio":
            try:
                apply_replace(material, params, command.seq)
            except CommandError as exc:
                rejected.append(_rejection(command, exc))
                continue
            reason = "replace_interval_audio"
        elif command.command_type == "merge_speakers":
            labels = sorted(cluster.label for cluster in result.clusters)
            label_a = resolve_selector(params["label_a"], labels)
            label_b = resolve_selector(params["label_b"], labels)
            if label_a == label_b:
                rejected.append({
                    "seq": command.seq,
                    "command_type": command.command_type,
                    "code": "E_COMMAND",
                    "message": "merge requires two distinct labels",
                    "context": {"labels": [label_a, label_b]},
                })
                continue
            overrides.append({
                "op": "merge",
                "seq": command.seq,
                "label_a": {"kind": "label", "value": label_a},
                "label_b": {"kind": "label", "value": label_b},
            })
            reason = "cluster_override"
        elif command.command_type == "split_speaker":
            labels = sorted(cluster.label for cluster in result.clusters)
            try:
                label = resolve_selector(params["label"], labels)
            except CommandError as exc:
                rejected.append(_rejection(command, exc))
                continue
            overrides.append({
                "op": "split",
                "seq": command.seq,
                "label": {"kind": "label", "value": label},
            })
            reason = "cluster_override"
        else:
            raise CommandError(f"unsupported command: {command.command_type}")

        result = pipeline.run(
            material=material,
            current_config=config,
            threshold_override=threshold_override,
            cluster_overrides=list(overrides),
            ledger=Ledger(),
            invalidate_reason=reason,
        )

    assert result is not None
    return result, rejected


def _rejection(command: EditCommand, exc: CommandError) -> dict[str, Any]:
    return {
        "seq": command.seq,
        "command_type": command.command_type,
        "code": exc.code,
        "message": exc.message,
        "context": exc.context,
    }
