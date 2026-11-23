"""Application service API for local workspaces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .canonical import atomic_write_bytes, atomic_write_json, digest, read_json
from .default_data import DEFAULT_VOCABULARY, default_samples
from .engine import ComputationEngine, ForcedTermination, RunOutputs
from .errors import (
    BusinessError,
    E_CONFIG_INVALID,
    E_DUPLICATE_PAIR_ID,
    E_IMAGE_SEED_INVALID,
    E_INPUT_INVALID,
    E_TEXT_EMPTY,
    E_VOCAB_MISSING,
)
from .models import (
    FeatureConfig,
    RetrievalConfig,
    SampleRecord,
    Vocabulary,
    WorkspaceConfig,
    natural_key,
)
from .text import validate_text_coverage


class MeetingRetrievalApp:
    def __init__(self, workspace: Path | str, initialize: bool = True):
        self.workspace = Path(workspace).resolve()
        self.config_path = self.workspace / "config.json"
        self.vocab_path = self.workspace / "vocab.json"
        self.samples_path = self.workspace / "samples.jsonl"
        self.state_dir = self.workspace / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.last_error_path = self.state_dir / "last_error.json"
        self.engine = ComputationEngine(self.workspace)
        try:
            raw_last_error = read_json(
                self.last_error_path,
                default=None,
                error_code=E_CONFIG_INVALID,
                field="last_error",
            )
        except BusinessError:
            raw_last_error = None
        self._last_error = raw_last_error.get("error") if isinstance(raw_last_error, dict) else None
        if initialize:
            self.ensure_workspace()

    # ------------------------------------------------------------------
    # Workspace lifecycle and loading
    # ------------------------------------------------------------------
    def ensure_workspace(self) -> Dict[str, Any]:
        self.workspace.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            config = WorkspaceConfig()
            config.validate()
            atomic_write_json(self.config_path, config.to_dict())
        config = self.load_config()
        if not self.vocab_path.exists():
            atomic_write_json(self.vocab_path, DEFAULT_VOCABULARY)
        if not self.samples_path.exists():
            self.write_samples(
                [SampleRecord.from_dict(item) for item in default_samples(config.features.canvas_shape)]
            )
        samples = self.load_samples(config.features)
        vocabulary = self.load_vocabulary()
        config.validate()
        vocabulary.validate()
        self._sync_snapshots(config, vocabulary)
        return {
            "workspace": str(self.workspace),
            "samples": len(samples),
            "config": config.to_dict(),
            "vocab_terms": len(vocabulary.terms),
            "snapshots": self.engine.snapshots(),
        }

    def load_config(self) -> WorkspaceConfig:
        raw = read_json(self.config_path, error_code=E_CONFIG_INVALID, field="config")
        if raw is None:
            config = WorkspaceConfig()
        elif not isinstance(raw, dict):
            raise BusinessError(E_CONFIG_INVALID, "config.json 必须为对象", field="config")
        else:
            config = WorkspaceConfig.from_dict(raw)
        config.validate()
        return config

    def load_vocabulary(self) -> Vocabulary:
        raw = read_json(self.vocab_path, error_code=E_CONFIG_INVALID, field="vocab")
        if raw is None:
            raw = DEFAULT_VOCABULARY
        if not isinstance(raw, dict):
            raise BusinessError(E_CONFIG_INVALID, "vocab.json 必须为对象", field="vocab")
        vocabulary = Vocabulary.from_dict(raw)
        vocabulary.validate()
        return vocabulary

    def load_samples(self, feature_config: FeatureConfig | None = None) -> List[SampleRecord]:
        config = self.load_config() if feature_config is None else None
        features = config.features if config is not None else feature_config
        assert features is not None
        records: List[SampleRecord] = []
        seen: Dict[str, int] = {}
        with self.samples_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise BusinessError(
                        E_CONFIG_INVALID,
                        "JSONL 行解析失败",
                        field="samples.jsonl",
                        details={"line": line_number, "column": exc.colno},
                    ) from exc
                if not isinstance(raw, dict):
                    raise BusinessError(
                        E_CONFIG_INVALID,
                        "JSONL 每行必须为对象",
                        field="samples.jsonl",
                        details={"line": line_number},
                    )
                record = SampleRecord.from_dict(raw, line_number=line_number)
                if record.pair_id in seen:
                    raise BusinessError(
                        E_DUPLICATE_PAIR_ID,
                        "pair_id 重复",
                        pair_id=record.pair_id,
                        field="pair_id",
                        details={"first_line": seen[record.pair_id], "duplicate_line": line_number},
                    )
                seen[record.pair_id] = line_number
                record.validate(features)
                records.append(record)
        if not records:
            raise BusinessError(E_INPUT_INVALID, "样本清单为空", field="samples.jsonl")
        return sorted(records, key=lambda item: natural_key(item.pair_id))

    def validate_sample_payloads(self, payloads: Sequence[Dict[str, Any]]) -> List[SampleRecord]:
        config = self.load_config()
        records: List[SampleRecord] = []
        seen: Dict[str, int] = {}
        for index, raw in enumerate(payloads, start=1):
            record = SampleRecord.from_dict(raw, line_number=index)
            if record.pair_id in seen:
                raise BusinessError(
                    E_DUPLICATE_PAIR_ID,
                    "pair_id 重复",
                    pair_id=record.pair_id,
                    field="pair_id",
                    details={"first_line": seen[record.pair_id], "duplicate_line": index},
                )
            seen[record.pair_id] = index
            record.validate(config.features)
            records.append(record)
        if not records:
            raise BusinessError(E_INPUT_INVALID, "样本清单为空", field="samples")
        return records

    def write_samples(self, records: Sequence[SampleRecord]) -> None:
        lines = []
        for record in sorted(records, key=lambda item: natural_key(item.pair_id)):
            lines.append(
                json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
        data = ("\n".join(lines) + "\n").encode("utf-8")
        atomic_write_bytes(self.samples_path, data)

    def _sync_snapshots(self, config: WorkspaceConfig, vocabulary: Vocabulary) -> None:
        self.engine.registry.put("vocab", vocabulary.to_dict())
        self.engine.registry.put("features", config.features.to_dict())

    # ------------------------------------------------------------------
    # Error capture
    # ------------------------------------------------------------------
    def record_error(self, error: BusinessError) -> None:
        self._last_error = error.to_dict()["error"]
        atomic_write_json(
            self.last_error_path,
            {"recorded_ns": __import__("time").time_ns(), "error": self._last_error},
        )

    @property
    def last_error(self) -> Optional[Dict[str, Any]]:
        return self._last_error

    def _guarded(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except BusinessError as exc:
            self.record_error(exc)
            raise

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def run(
        self,
        crash_after: Optional[str] = None,
        force_new_run: bool = False,
    ) -> RunOutputs:
        def operation() -> RunOutputs:
            config = self.load_config()
            vocabulary = self.load_vocabulary()
            samples = self.load_samples(config.features)
            self._sync_snapshots(config, vocabulary)
            return self.engine.run(
                samples=samples,
                vocabulary=vocabulary,
                feature_config=config.features,
                retrieval_config=config.retrieval,
                max_pixel_bytes=config.limits.max_pixel_bytes_per_image,
                max_cache_entries=config.limits.max_cache_entries,
                max_recompute_pairs=config.limits.max_recompute_pairs,
                crash_after=crash_after,
                force_new_run=force_new_run,
            )

        return self._guarded(operation)

    def recover(self) -> Dict[str, Any]:
        return self._guarded(self.engine.stages.recover)

    def status(self) -> Dict[str, Any]:
        def operation() -> Dict[str, Any]:
            config = self.load_config()
            samples = self.load_samples(config.features)
            manifest = self.engine.manifest()
            retrieval = self.engine.latest_retrieval()
            return {
                "workspace": str(self.workspace),
                "sample_count": len(samples),
                "pair_ids": [sample.pair_id for sample in samples],
                "topology": self.engine.topology(),
                "snapshots": self.engine.snapshots(),
                "last_run": None
                if manifest is None
                else {
                    "run_id": manifest["run_id"],
                    "affected": manifest["last_result"]["affected"],
                    "recomputed_nodes": manifest["last_result"]["recomputed_nodes"],
                    "reused_nodes": manifest["last_result"]["reused_nodes"],
                },
                "retrieval": None
                if retrieval is None
                else {
                    "cache_key": retrieval["cache_key"],
                    "artifact_hash": retrieval["artifact_hash"],
                    "metrics": retrieval["ranking"]["metrics"],
                    "hits": retrieval["ranking"]["hits"],
                },
                "last_error": self.last_error,
                "degradation": [],
            }

        return self._guarded(operation)

    def hits(self) -> List[Dict[str, Any]]:
        def operation() -> List[Dict[str, Any]]:
            retrieval = self.engine.latest_retrieval()
            if retrieval is None:
                return []
            return retrieval["ranking"]["hits"]

        return self._guarded(operation)

    def trace_hit(self, hit_id: str) -> Dict[str, Any]:
        return self._guarded(self.engine.trace_hit, hit_id)

    def replay_hit(self, hit_id: str) -> Dict[str, Any]:
        return self._guarded(self.engine.replay_hit, hit_id)

    def query_cache(self, node: str, pair_id: str) -> Dict[str, Any]:
        return self._guarded(self.engine.query_cache, node, pair_id)

    def self_check(self) -> Dict[str, Any]:
        return self._guarded(self.engine.self_check)

    # ------------------------------------------------------------------
    # Incremental mutations
    # ------------------------------------------------------------------
    def update_text(self, pair_id: str, text: str) -> RunOutputs:
        def operation() -> RunOutputs:
            if not isinstance(text, str) or not text.strip():
                raise BusinessError(E_TEXT_EMPTY, "转写文本不得为空", pair_id=pair_id, field="text")
            config = self.load_config()
            samples = self.load_samples(config.features)
            matched = False
            for sample in samples:
                if sample.pair_id == pair_id:
                    sample.text = text
                    matched = True
                    break
            if not matched:
                raise BusinessError(E_INPUT_INVALID, "pair_id 不存在", pair_id=pair_id, field="pair_id")
            for sample in samples:
                sample.validate(config.features)
            validate_text_coverage(samples, self.load_vocabulary())
            self.engine.prepare_change("text_changed")
            self.write_samples(samples)
            return self._run_loaded(config, samples)

        return self._guarded(operation)

    def update_seed(self, pair_id: str, image_seed: int) -> RunOutputs:
        def operation() -> RunOutputs:
            if isinstance(image_seed, bool) or not isinstance(image_seed, int):
                raise BusinessError(
                    E_IMAGE_SEED_INVALID,
                    "image_seed 必须为整数",
                    pair_id=pair_id,
                    field="image_seed",
                )
            if image_seed < 0 or image_seed >= 2**64:
                raise BusinessError(
                    E_IMAGE_SEED_INVALID,
                    "image_seed 必须位于 [0, 2^64)",
                    pair_id=pair_id,
                    field="image_seed",
                )
            config = self.load_config()
            samples = self.load_samples(config.features)
            matched = False
            for sample in samples:
                if sample.pair_id == pair_id:
                    sample.image_seed = int(image_seed)
                    matched = True
                    break
            if not matched:
                raise BusinessError(E_INPUT_INVALID, "pair_id 不存在", pair_id=pair_id, field="pair_id")
            for sample in samples:
                sample.validate(config.features)
            self.engine.prepare_change("image_seed_changed")
            self.write_samples(samples)
            return self._run_loaded(config, samples)

        return self._guarded(operation)

    def set_feature_params(self, **changes: Any) -> RunOutputs:
        def operation() -> RunOutputs:
            config = self.load_config()
            vocabulary = self.load_vocabulary()
            samples = self.load_samples(config.features)
            payload = config.features.to_dict()
            payload.update(changes)
            features = FeatureConfig.from_dict(payload)
            features.validate()
            new_config = WorkspaceConfig(features=features, retrieval=config.retrieval, limits=config.limits)
            for sample in samples:
                sample.validate(features)
            self.engine.registry.put("features", features.to_dict())
            self.engine.prepare_change("feature_params_changed")
            atomic_write_json(self.config_path, new_config.to_dict())
            return self._run_loaded(new_config, samples)

        return self._guarded(operation)

    def set_retrieval_params(self, **changes: Any) -> RunOutputs:
        def operation() -> RunOutputs:
            config = self.load_config()
            samples = self.load_samples(config.features)
            payload = config.retrieval.to_dict()
            payload.update(changes)
            retrieval = RetrievalConfig.from_dict(payload)
            retrieval.validate()
            new_config = WorkspaceConfig(features=config.features, retrieval=retrieval, limits=config.limits)
            self.engine.prepare_change("retrieval_params_changed")
            atomic_write_json(self.config_path, new_config.to_dict())
            return self._run_loaded(new_config, samples)

        return self._guarded(operation)

    def set_resource_limits(self, **changes: Any) -> Dict[str, Any]:
        def operation() -> Dict[str, Any]:
            from .models import ResourceLimits

            config = self.load_config()
            payload = config.limits.to_dict()
            payload.update(changes)
            limits = ResourceLimits.from_dict(payload)
            limits.validate()
            new_config = WorkspaceConfig(
                features=config.features,
                retrieval=config.retrieval,
                limits=limits,
            )
            atomic_write_json(self.config_path, new_config.to_dict())
            return {"ok": True, "limits": limits.to_dict()}

        return self._guarded(operation)

    def set_vocabulary(self, terms: Sequence[str]) -> RunOutputs:
        def operation() -> RunOutputs:
            config = self.load_config()
            samples = self.load_samples(config.features)
            vocabulary = Vocabulary(terms=[str(term) for term in terms])
            vocabulary.validate()
            validate_text_coverage(samples, vocabulary)
            self.engine.registry.put("vocab", vocabulary.to_dict())
            atomic_write_json(self.vocab_path, vocabulary.to_dict())
            self.engine.prepare_change("vocabulary_changed")
            return self._run_loaded(config, samples, vocabulary)

        return self._guarded(operation)

    def _run_loaded(
        self,
        config: WorkspaceConfig,
        samples: Sequence[SampleRecord],
        vocabulary: Optional[Vocabulary] = None,
    ) -> RunOutputs:
        selected_vocab = vocabulary or self.load_vocabulary()
        self._sync_snapshots(config, selected_vocab)
        return self.engine.run(
            samples=samples,
            vocabulary=selected_vocab,
            feature_config=config.features,
            retrieval_config=config.retrieval,
            max_pixel_bytes=config.limits.max_pixel_bytes_per_image,
            max_cache_entries=config.limits.max_cache_entries,
            max_recompute_pairs=config.limits.max_recompute_pairs,
        )

    # ------------------------------------------------------------------
    # Presentation helpers
    # ------------------------------------------------------------------
    def render_topology(self) -> str:
        topology = self.engine.topology()
        lines = ["计算图拓扑（DAG）", "=" * 72]
        for node in topology["nodes"]:
            dependencies = ", ".join(node["depends_on"]) or "(source)"
            lines.append(
                f"{node['id']:8s} <- {dependencies:20s} stage={node['stage']} scope={node['scope']}"
            )
            lines.append(f"         {node['label']}")
        lines.append("-" * 72)
        lines.append(f"同分规则: {topology['tie_break']}")
        return "\n".join(lines)
