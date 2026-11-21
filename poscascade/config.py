from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .canonical import object_checksum, read_json
from .errors import BusinessError, ErrorCode


@dataclass(frozen=True)
class PipelineConfig:
    algorithm_version: str = "1.0.0"
    max_codepoints_per_line: int = 240
    max_candidate_edges_per_stage: int = 20_000
    candidate_limit_policy: str = "degrade"
    degrade_max_candidates_per_start: int = 3
    max_tag_resident_bytes: int = 8_000_000
    tag_memory_policy: str = "degrade"
    fallback_pos: str = "x"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def checksum(self) -> str:
        return object_checksum(self.to_dict())


def load_config(path: str | Path | None = None) -> PipelineConfig:
    values: dict[str, Any] = {}
    if path is not None:
        try:
            raw = read_json(path)
        except (OSError, ValueError) as exc:
            raise BusinessError(
                ErrorCode.E_INPUT_FORMAT,
                f"配置读取失败: {exc}",
                details={"path": str(path)},
            ) from exc
        if not isinstance(raw, dict):
            raise BusinessError(ErrorCode.E_INPUT_FORMAT, "配置文件必须是 JSON 对象")
        values = raw

    known = set(PipelineConfig.__dataclass_fields__)
    unknown = sorted(set(values) - known)
    if unknown:
        raise BusinessError(
            ErrorCode.E_INPUT_FORMAT,
            "配置包含未知字段",
            details={"unknown_fields": unknown},
        )

    merged = PipelineConfig(**values)
    _validate_config(merged)
    return merged


def _validate_config(config: PipelineConfig) -> None:
    if not isinstance(config.algorithm_version, str) or not config.algorithm_version:
        raise BusinessError(ErrorCode.E_INPUT_FORMAT, "algorithm_version 必须是非空字符串")
    integer_fields = (
        "max_codepoints_per_line",
        "max_candidate_edges_per_stage",
        "degrade_max_candidates_per_start",
        "max_tag_resident_bytes",
    )
    for field_name in integer_fields:
        value = getattr(config, field_name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise BusinessError(
                ErrorCode.E_INPUT_FORMAT,
                f"{field_name} 必须是正整数",
                details={"field": field_name, "value": value},
            )
    if config.candidate_limit_policy not in {"degrade", "error"}:
        raise BusinessError(
            ErrorCode.E_INPUT_FORMAT,
            "candidate_limit_policy 只能是 degrade 或 error",
        )
    if config.tag_memory_policy not in {"degrade", "error"}:
        raise BusinessError(
            ErrorCode.E_INPUT_FORMAT,
            "tag_memory_policy 只能是 degrade 或 error",
        )
    if not isinstance(config.fallback_pos, str) or not config.fallback_pos:
        raise BusinessError(ErrorCode.E_INPUT_FORMAT, "fallback_pos 必须是非空字符串")
