"""Dependency-aware computation graph with content-addressed artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from .cache import ArtifactCache, CacheEntry, cache_key
from .errors import ErrorCode, diagnostic, error_codes
from .fusion import make_contribution, make_output, merge_complementary_filter
from .injection import InjectionPlan
from .models import FlightState, SensorDescription
from .preprocess import preprocess_sensor, target_times, time_grid_digest
from .serialization import canonical_bytes, clone_json, content_hash


@dataclass
class NodeResult:
    node_id: str
    node_type: str
    sensor_id: str | None
    key: str
    content_hash: str
    status: str
    depends_on: tuple[str, ...]
    payload: dict[str, Any]
    key_material: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "sensor_id": self.sensor_id,
            "key": self.key,
            "content_hash": self.content_hash,
            "status": self.status,
            "depends_on": list(self.depends_on),
            "key_material": clone_json(self.key_material),
        }


@dataclass
class BuildResult:
    state: FlightState
    nodes: dict[str, NodeResult]
    topology: dict[str, Any]
    diagnostics: list[dict[str, Any]]
    injection_plan: InjectionPlan
    cache_stats: dict[str, int]
    recomputed_nodes: list[str]
    reused_nodes: list[str]
    affected_sensors: list[str]
    output: dict[str, Any]

    @property
    def business_codes(self) -> list[str]:
        return error_codes(self.diagnostics)

    @property
    def last_business_error(self) -> str | None:
        codes = self.business_codes
        return codes[-1] if codes else None

    def to_dict(self, *, include_series: bool = True) -> dict[str, Any]:
        output = clone_json(self.output)
        if not include_series:
            output = {
                key: value
                for key, value in output.items()
                if key
                not in {
                    "times",
                    "quaternions",
                    "euler_deg",
                    "truth_euler_deg",
                    "residual",
                    "accel_residual",
                    "accel_residual_mps2",
                    "mag_residual",
                    "gyro_bias_estimate",
                }
            }
        return {
            "snapshot": {
                "version": self.state.snapshot_version,
                "checksum": self.state.snapshot_checksum,
            },
            "active_formats": dict(sorted(self.state.active_formats.items())),
            "fusion_config": self.state.fusion_config.as_dict(),
            "descriptions": {
                sensor_id: description.as_dict()
                for sensor_id, description in sorted(self.state.descriptions.items())
            },
            "topology": clone_json(self.topology),
            "nodes": [item.public_dict() for item in self.nodes.values()],
            "cache_stats": dict(self.cache_stats),
            "recomputed_nodes": list(self.recomputed_nodes),
            "reused_nodes": list(self.reused_nodes),
            "affected_sensors": list(self.affected_sensors),
            "diagnostics": clone_json(self.diagnostics),
            "business_codes": self.business_codes,
            "last_business_error": self.last_business_error,
            "injection_plan": self.injection_plan.as_dict(),
            "output": output,
        }

    def output_bytes(self) -> bytes:
        return canonical_bytes(self.output)


class FusionGraph:
    def __init__(self, cache_dir: str | Path):
        self.cache = ArtifactCache(cache_dir)

    def _get_or_compute(
        self,
        *,
        node_id: str,
        node_type: str,
        sensor_id: str | None,
        key: str,
        depends_on: tuple[str, ...],
        compute: Callable[[], dict[str, Any]],
        force_recompute: bool,
        key_material: dict[str, Any],
    ) -> NodeResult:
        entry: CacheEntry | None = None
        if not force_recompute:
            entry = self.cache.get(key)
        if entry is not None:
            return NodeResult(
                node_id=node_id,
                node_type=node_type,
                sensor_id=sensor_id,
                key=key,
                content_hash=entry.content_hash,
                status="reused",
                depends_on=depends_on,
                payload=entry.payload,
                key_material=entry.key_material or key_material,
            )
        payload = compute()
        entry = self.cache.put(
            key=key,
            node_id=node_id,
            node_type=node_type,
            payload=payload,
            depends_on=depends_on,
            key_material=key_material,
        )
        return NodeResult(
            node_id=node_id,
            node_type=node_type,
            sensor_id=sensor_id,
            key=key,
            content_hash=entry.content_hash,
            status="recomputed",
            depends_on=depends_on,
            payload=clone_json(payload),
            key_material=clone_json(key_material),
        )

    def build(
        self,
        state: FlightState,
        *,
        injection_plan: InjectionPlan | None = None,
        force_recompute: bool = False,
    ) -> BuildResult:
        plan = injection_plan or InjectionPlan.empty()
        diagnostics: list[dict[str, Any]] = []
        nodes: dict[str, NodeResult] = {}
        effective_descriptions: dict[str, SensorDescription] = {}
        upstream_codes_by_sensor: dict[str, list[str]] = {
            sensor_id: [] for sensor_id in state.descriptions
        }
        root_codes_by_sensor: dict[str, list[str]] = {
            sensor_id: [] for sensor_id in state.descriptions
        }

        for sensor_id in sorted(state.descriptions):
            parse_effect = self._sensor_stage_digest(plan, "parse", sensor_id)
            description = state.descriptions[sensor_id]
            sequence = state.dataset.sequences[sensor_id]
            parse_diags: list[dict[str, Any]] = []

            axis_injection = plan.find("parse", "axis_missing")
            if axis_injection and (
                axis_injection.sensor_id is None or axis_injection.sensor_id == sensor_id
            ):
                bad_order = ["x", "y", "q"]
                description = replace(
                    description,
                    axis_order=("x", "y", "z"),
                    provenance={
                        **description.provenance,
                        "axis_order": "runtime_fallback.identity",
                    },
                )
                parse_diags.append(
                    diagnostic(
                        code=ErrorCode.AXIS_MAPPING_INVALID,
                        stage="parse",
                        sensor_id=sensor_id,
                        field="axis_order",
                        value=bad_order,
                        message="injected axis order references an unknown axis q",
                        degradation="fallback_identity_axis_mapping",
                        details={"injection": "parse.axis_missing"},
                    )
                )
            version_injection = plan.find("parse", "unknown_version")
            if version_injection and (
                version_injection.sensor_id is None
                or version_injection.sensor_id == sensor_id
            ):
                parse_diags.append(
                    diagnostic(
                        code=ErrorCode.VERSION_UNSUPPORTED,
                        stage="parse",
                        sensor_id=sensor_id,
                        field="format_version",
                        value="v99",
                        message="injected unsupported description format version",
                        degradation="retain_last_known_description",
                        details={"injection": "parse.unknown_version"},
                    )
                )
            missing_injection = plan.find("parse", "missing_field")
            if missing_injection and (
                missing_injection.sensor_id is None
                or missing_injection.sensor_id == sensor_id
            ):
                parse_diags.append(
                    diagnostic(
                        code=ErrorCode.FORMAT_CORRUPT,
                        stage="parse",
                        sensor_id=sensor_id,
                        field="sample_rate",
                        line=2,
                        message="injected missing required field sample_rate",
                        degradation="retain_last_known_description",
                        details={"injection": "parse.missing_field"},
                    )
                )

            diagnostics.extend(parse_diags)
            codes = error_codes(parse_diags)
            upstream_codes_by_sensor[sensor_id].extend(codes)
            root_codes_by_sensor[sensor_id].extend(codes)
            effective_descriptions[sensor_id] = description
            raw_digest = sequence.content_hash()
            material = {
                "node_id": f"parse:{sensor_id}",
                "node_type": "parse_sensor",
                "sensor_id": sensor_id,
                "raw_digest": raw_digest,
                "description_version": description.description_version,
                "calibration_version": description.calibration_version,
                "axis_mapping": description.axis_mapping,
                "description_hash": description.content_hash(),
                "stage_effect_digest": parse_effect,
            }
            key = cache_key(material)
            node = self._get_or_compute(
                node_id=f"parse:{sensor_id}",
                node_type="parse_sensor",
                sensor_id=sensor_id,
                key=key,
                depends_on=(),
                compute=lambda description=description, raw_digest=raw_digest: {
                    "sensor": description.as_dict(),
                    "raw_digest": raw_digest,
                    "diagnostic_codes": list(upstream_codes_by_sensor[sensor_id]),
                },
                force_recompute=force_recompute,
                key_material=material,
            )
            nodes[node.node_id] = node

        grid_rate = max(float(state.fusion_config.output_rate_hz), 1e-9)
        target = target_times(state.dataset, grid_rate)
        grid_digest = time_grid_digest(target)
        preprocessed: dict[str, dict[str, Any]] = {}
        effective_rates: dict[str, float] = {}

        for sensor_id in sorted(state.descriptions):
            preprocess_effect = self._sensor_stage_digest(plan, "preprocess", sensor_id)
            description = effective_descriptions[sensor_id]
            sequence = state.dataset.sequences[sensor_id]
            pre_diags: list[dict[str, Any]] = []
            inherited = list(upstream_codes_by_sensor[sensor_id])
            for upstream_code in inherited:
                pre_diags.append(
                    diagnostic(
                        code=ErrorCode.UPSTREAM_DEGRADED,
                        stage="preprocess",
                        sensor_id=sensor_id,
                        field="upstream",
                        upstream_code=upstream_code,
                        message=f"preprocessing continues after {upstream_code}",
                        degradation="continue_with_effective_description",
                    )
                )

            rollback = plan.find("preprocess", "calibration_rollback")
            if rollback and (
                rollback.sensor_id is None or rollback.sensor_id == sensor_id
            ):
                requested = max(1, description.calibration_version - 1)
                pre_diags.append(
                    diagnostic(
                        code=ErrorCode.CALIBRATION_ROLLBACK,
                        stage="preprocess",
                        sensor_id=sensor_id,
                        field="calibration_version",
                        value={
                            "active": description.calibration_version,
                            "requested": requested,
                        },
                        message="injected calibration version rollback",
                        degradation="retain_active_calibration_profile",
                        details={"injection": "preprocess.calibration_rollback"},
                    )
                )
                effective_rates[sensor_id] = description.sample_rate

            rate_injection = plan.find("preprocess", "sample_rate_nonpositive")
            if rate_injection and (
                rate_injection.sensor_id is None
                or rate_injection.sensor_id == sensor_id
            ):
                fallback_rate = float(sequence.sample_rate)
                if fallback_rate <= 0:
                    fallback_rate = 1.0
                effective_rates[sensor_id] = fallback_rate
                pre_diags.append(
                    diagnostic(
                        code=ErrorCode.SAMPLE_RATE_INVALID,
                        stage="preprocess",
                        sensor_id=sensor_id,
                        field="sample_rate",
                        value=0.0,
                        message="injected non-positive sensor sample rate",
                        degradation=f"use_sequence_rate:{fallback_rate:g}",
                        details={"injection": "preprocess.sample_rate_nonpositive"},
                    )
                )
            else:
                effective_rates.setdefault(sensor_id, description.sample_rate)

            diagnostics.extend(pre_diags)
            all_codes = error_codes(pre_diags + [
                {"code": code} for code in inherited
            ])
            upstream_codes_by_sensor[sensor_id] = all_codes
            root_codes_by_sensor[sensor_id].extend(
                code for code in all_codes if code != ErrorCode.UPSTREAM_DEGRADED
            )
            preprocess_material = {
                "node_id": f"preprocess:{sensor_id}",
                "node_type": "preprocess",
                "sensor_id": sensor_id,
                "raw_digest": sequence.content_hash(),
                "description_version": description.description_version,
                "calibration_version": description.calibration_version,
                "axis_mapping": description.axis_mapping,
                "description_hash": description.content_hash(),
                "upstream_hash": nodes[f"parse:{sensor_id}"].content_hash,
                "grid_digest": grid_digest,
                "effective_sample_rate": effective_rates[sensor_id],
                "stage_effect_digest": preprocess_effect,
            }
            preprocess_key = cache_key(preprocess_material)

            def compute_preprocess(
                sensor_id: str = sensor_id,
                description: SensorDescription = description,
                sequence=sequence,
                target=target,
                pre_diags=pre_diags,
                all_codes=all_codes,
            ) -> dict[str, Any]:
                payload = preprocess_sensor(
                    sequence,
                    description,
                    target,
                    effective_sample_rate=effective_rates[sensor_id],
                    degradations=pre_diags,
                )
                payload["upstream_codes"] = all_codes
                return payload

            node = self._get_or_compute(
                node_id=f"preprocess:{sensor_id}",
                node_type="preprocess",
                sensor_id=sensor_id,
                key=preprocess_key,
                depends_on=(f"parse:{sensor_id}",),
                compute=compute_preprocess,
                force_recompute=force_recompute,
                key_material=preprocess_material,
            )
            nodes[node.node_id] = node
            preprocessed[sensor_id] = node.payload

        sensor_versions = {
            sensor_id: {
                "description_version": description.description_version,
                "format_version": description.format_version,
                "calibration_version": description.calibration_version,
                "axis_order": list(description.axis_order),
                "description_hash": description.content_hash(),
            }
            for sensor_id, description in sorted(effective_descriptions.items())
        }
        effective_config = state.fusion_config
        fusion_diags: list[dict[str, Any]] = []
        gain_injection = plan.find("fusion", "gain_out_of_range")
        if gain_injection:
            gains = (1.5, -0.2)
            effective_config = replace(
                effective_config,
                complementary_gain=max(0.0, min(1.0, gains[0])),
            )
            fusion_diags.append(
                diagnostic(
                    code=ErrorCode.GAIN_OUT_OF_RANGE,
                    stage="fusion",
                    sensor_id=None,
                    field="complementary_gain",
                    value=gains[0],
                    message="injected complementary filter gain outside [0, 1]",
                    degradation="clamp_to_valid_range",
                    details={"effective_value": effective_config.complementary_gain},
                )
            )
        weight_injection = plan.find("fusion", "weight_out_of_range")
        if weight_injection:
            field = (
                "accel_weight"
                if weight_injection.sensor_id != "mag"
                else "mag_weight"
            )
            bad_value = 1.75
            effective_config = replace(
                effective_config,
                **{field: max(0.0, min(1.0, bad_value))},
            )
            fusion_diags.append(
                diagnostic(
                    code=ErrorCode.WEIGHT_OUT_OF_RANGE,
                    stage="fusion",
                    sensor_id=weight_injection.sensor_id,
                    field=field,
                    value=bad_value,
                    message="injected fusion weight outside [0, 1]",
                    degradation="clamp_to_valid_range",
                    details={"effective_value": getattr(effective_config, field)},
                )
            )
        diagnostics.extend(fusion_diags)

        contributions: dict[str, dict[str, Any]] = {}
        for sensor_id in sorted(preprocessed):
            description = effective_descriptions[sensor_id]
            config = effective_config
            if description.sensor_type == "gyro":
                relevant_config = {"transform": "deg_s_to_rad_s"}
            elif description.sensor_type == "accel":
                relevant_config = {
                    "weight": config.accel_weight,
                    "gravity_constant": config.gravity_constant,
                }
            else:
                relevant_config = {
                    "weight": config.mag_weight,
                    "mag_reference": list(config.mag_reference),
                }
            contribution_material = {
                "node_id": f"fusion:{sensor_id}",
                "node_type": "fusion_contribution",
                "sensor_id": sensor_id,
                "sensor_type": description.sensor_type,
                "upstream_hash": nodes[f"preprocess:{sensor_id}"].content_hash,
                "description_version": description.description_version,
                "axis_mapping": description.axis_mapping,
                "relevant_config": relevant_config,
            }
            contribution_key = cache_key(contribution_material)
            contribution_diags = list(root_codes_by_sensor[sensor_id])
            for root_code in contribution_diags:
                diagnostics.append(
                    diagnostic(
                        code=ErrorCode.UPSTREAM_DEGRADED,
                        stage="fusion",
                        sensor_id=sensor_id,
                        field="upstream",
                        upstream_code=root_code,
                        message=f"fusion contribution continues after {root_code}",
                        degradation="use_effective_upstream_artifact",
                    )
                )

            def compute_contribution(
                preprocessed_item=preprocessed[sensor_id],
                config=config,
                upstream_hash=nodes[f"preprocess:{sensor_id}"].content_hash,
                contribution_diags=contribution_diags,
            ) -> dict[str, Any]:
                payload = make_contribution(preprocessed_item, config)
                payload["preprocess_content_hash"] = upstream_hash
                payload["upstream_codes"] = contribution_diags
                return payload

            node = self._get_or_compute(
                node_id=f"fusion:{sensor_id}",
                node_type="fusion_contribution",
                sensor_id=sensor_id,
                key=contribution_key,
                depends_on=(f"preprocess:{sensor_id}",),
                compute=compute_contribution,
                force_recompute=force_recompute,
                key_material=contribution_material,
            )
            nodes[node.node_id] = node
            contributions[sensor_id] = node.payload

        all_contribution_hashes = {
            sensor_id: nodes[f"fusion:{sensor_id}"].content_hash
            for sensor_id in sorted(contributions)
        }
        merge_material = {
            "node_id": "fusion:merge",
            "node_type": "fusion",
            "config_version": effective_config.config_version,
            "config_hash": effective_config.content_hash(),
            "contributions": all_contribution_hashes,
            "grid_digest": grid_digest,
            "stage_effect_digest": plan.stage_digest("fusion"),
        }
        merge_key = cache_key(merge_material)
        for root_code in error_codes(diagnostics):
            if root_code == ErrorCode.UPSTREAM_DEGRADED:
                continue
            fusion_diags.append(
                diagnostic(
                    code=ErrorCode.UPSTREAM_DEGRADED,
                    stage="fusion",
                    sensor_id=None,
                    field="upstream",
                    upstream_code=root_code,
                    message=f"fusion merge continues after {root_code}",
                    degradation="continue_with_effective_inputs",
                )
            )
        diagnostics.extend(fusion_diags)

        def compute_merge() -> dict[str, Any]:
            payload = merge_complementary_filter(
                contributions,
                effective_config,
                target_times=target,
                sensor_versions=sensor_versions,
            )
            payload["upstream_codes"] = error_codes(fusion_diags)
            return payload

        merge_node = self._get_or_compute(
            node_id="fusion:merge",
            node_type="fusion",
            sensor_id=None,
            key=merge_key,
            depends_on=tuple(f"fusion:{sensor_id}" for sensor_id in sorted(contributions)),
            compute=compute_merge,
            force_recompute=force_recompute,
            key_material=merge_material,
        )
        nodes[merge_node.node_id] = merge_node

        output_effect = plan.stage_digest("output")
        output_material = {
            "node_id": "output:attitude",
            "node_type": "output",
            "fusion_hash": merge_node.content_hash,
            "truth_digest": state.dataset.truth.content_hash(),
            "config_version": effective_config.config_version,
            "stage_effect_digest": output_effect,
        }
        output_key = cache_key(output_material)
        output_diags: list[dict[str, Any]] = []
        output_injection = plan.find("output", "residual_missing")
        if output_injection:
            output_diags.append(
                diagnostic(
                    code=ErrorCode.OUTPUT_FIELD_MISSING,
                    stage="output",
                    sensor_id=None,
                    field="residual",
                    message="injected missing residual field at output serialization",
                    degradation="restore_residual_from_fusion_artifact",
                    details={"injection": "output.residual_missing"},
                )
            )
        for root_code in error_codes(diagnostics):
            if root_code in {ErrorCode.UPSTREAM_DEGRADED}:
                continue
            if root_code == ErrorCode.OUTPUT_FIELD_MISSING:
                continue
            output_diags.append(
                diagnostic(
                    code=ErrorCode.UPSTREAM_DEGRADED,
                    stage="output",
                    sensor_id=None,
                    field="upstream",
                    upstream_code=root_code,
                    message=f"output continues after {root_code}",
                    degradation="emit_with_provenance",
                )
            )
        diagnostics.extend(output_diags)

        def compute_output() -> dict[str, Any]:
            payload = make_output(
                merge_node.payload,
                state.dataset.truth.times,
                state.dataset.truth.quaternions,
            )
            payload["fusion_content_hash"] = merge_node.content_hash
            payload["upstream_codes"] = error_codes(output_diags)
            if output_injection:
                payload["residual_field_degraded"] = True
            return payload

        output_node = self._get_or_compute(
            node_id="output:attitude",
            node_type="output",
            sensor_id=None,
            key=output_key,
            depends_on=("fusion:merge",),
            compute=compute_output,
            force_recompute=force_recompute,
            key_material=output_material,
        )
        nodes[output_node.node_id] = output_node

        recomputed = [
            node.node_id for node in nodes.values() if node.status == "recomputed"
        ]
        reused = [
            node.node_id for node in nodes.values() if node.status == "reused"
        ]
        affected_sensors = sorted(
            {
                node.sensor_id
                for node in nodes.values()
                if node.status == "recomputed" and node.sensor_id is not None
            }
        )
        topology = self._topology(nodes)
        return BuildResult(
            state=state,
            nodes=nodes,
            topology=topology,
            diagnostics=diagnostics,
            injection_plan=plan,
            cache_stats=self.cache.stats(),
            recomputed_nodes=recomputed,
            reused_nodes=reused,
            affected_sensors=affected_sensors,
            output=output_node.payload,
        )

    @staticmethod
    def _sensor_stage_digest(
        plan: InjectionPlan,
        stage: str,
        sensor_id: str,
    ) -> str:
        relevant = [
            item.as_dict()
            for item in plan.for_stage(stage)
            if item.sensor_id is None or item.sensor_id == sensor_id
        ]
        return content_hash({"injections": relevant})

    def _topology(self, nodes: dict[str, NodeResult]) -> dict[str, Any]:
        node_public = []
        labels = {
            "parse_sensor": "描述解析",
            "preprocess": "逐传感器预处理",
            "fusion_contribution": "融合贡献",
            "fusion": "互补滤波合并",
            "output": "姿态输出与统计",
        }
        for node in nodes.values():
            node_public.append(
                {
                    "id": node.node_id,
                    "type": node.node_type,
                    "sensor_id": node.sensor_id,
                    "label": labels.get(node.node_type, node.node_type),
                    "status": node.status,
                    "key": node.key,
                    "content_hash": node.content_hash,
                }
            )
        edges = []
        for node in nodes.values():
            for dependency in node.depends_on:
                edges.append({"from": dependency, "to": node.node_id})
        return {"nodes": node_public, "edges": edges}

