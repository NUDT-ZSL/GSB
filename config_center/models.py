from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import bad_clock, bad_config, digest_mismatch
from .util import decimal_text, sha256_json


NUMERIC_TYPES = {"integer", "decimal"}
SUPPORTED_TYPES = NUMERIC_TYPES | {"string", "boolean"}
KNOWN_ROLES = {
    "timeout_ms",
    "retry_factor",
    "sample_rate",
    "rate_limit_qps",
    "capacity_limit",
    "filter_threshold",
    "weight_alpha",
    "weight_beta",
    "weight_gamma",
    "output_ratio",
}
REQUIRED_ROLES = {
    "timeout_ms",
    "retry_factor",
    "sample_rate",
    "rate_limit_qps",
    "capacity_limit",
    "filter_threshold",
    "weight_alpha",
    "weight_beta",
    "output_ratio",
}

_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class ConfigDecl:
    key: str
    value_type: str
    value_text: str
    value_decimal: Decimal | None
    scale: int
    role: str | None

    @property
    def is_numeric(self) -> bool:
        return self.value_type in NUMERIC_TYPES

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": self.value_type,
            "value": self.value_text,
        }
        if self.role is not None:
            body["role"] = self.role
        if self.is_numeric:
            body["scale"] = self.scale
        return body


@dataclass(frozen=True)
class Snapshot:
    namespace: str
    version: int
    logical_clock_ms: int
    configs: dict[str, ConfigDecl]
    digest: str

    def content(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "version": self.version,
            "logical_clock_ms": self.logical_clock_ms,
            "configs": {key: cfg.to_dict() for key, cfg in self.configs.items()},
        }

    def to_dict(self) -> dict[str, Any]:
        result = self.content()
        result["content_digest"] = self.digest
        return result


@dataclass(frozen=True)
class ValidatedPatch:
    namespace: str
    base_version: int | None
    target_version: int
    logical_clock_ms: int
    mode: str
    operations: tuple[dict[str, Any], ...]
    configs: dict[str, ConfigDecl] | None
    patch_digest: str
    raw_content: dict[str, Any]


def _expect_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise bad_config(f"{field} 必须是 JSON 对象", field=field)
    return value


def _expect_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise bad_config(f"{field} 必须是整数", field=field, value=value)
    return value


def _expect_positive_int(value: Any, field: str) -> int:
    result = _expect_int(value, field)
    if result <= 0:
        raise bad_config(f"{field} 必须大于 0", field=field, value=result)
    return result


def _normalize_namespace(value: Any) -> str:
    if not isinstance(value, str) or not _NAMESPACE_RE.fullmatch(value):
        raise bad_config(
            "namespace 必须匹配 [A-Za-z0-9][A-Za-z0-9_.:-]{0,127}",
            field="namespace",
            value=value,
        )
    return value


def _normalize_key(value: Any) -> str:
    if not isinstance(value, str) or not _KEY_RE.fullmatch(value):
        raise bad_config(
            "config key 必须匹配 [A-Za-z][A-Za-z0-9_.-]{0,127}",
            field="config_key",
            value=value,
        )
    return value


def _decimal_value(value: Any, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise bad_config(f"{field} 必须是有限十进制数", field=field, value=value)
    if isinstance(value, (int, Decimal)):
        dec = Decimal(value)
    elif isinstance(value, str):
        try:
            dec = Decimal(value)
        except InvalidOperation as exc:
            raise bad_config(f"{field} 不是合法十进制数", field=field, value=value) from exc
    else:
        raise bad_config(f"{field} 必须是有限十进制数", field=field, value=value)
    if not dec.is_finite():
        raise bad_config(f"{field} 必须是有限十进制数", field=field, value=value)
    return dec


def parse_config_entry(
    key: str,
    raw: Any,
    *,
    inherited: ConfigDecl | None = None,
) -> ConfigDecl:
    key = _normalize_key(key)
    entry = _expect_object(raw, f"configs.{key}")
    if "value" not in entry:
        raise bad_config(f"配置 {key} 缺少 value", config_key=key)

    value_type = entry.get("type", inherited.value_type if inherited else None)
    if value_type is None:
        raise bad_config(f"新配置 {key} 必须声明 type", config_key=key)
    if value_type not in SUPPORTED_TYPES:
        raise bad_config(
            f"配置 {key} 的 type 不受支持",
            config_key=key,
            value_type=value_type,
            supported=sorted(SUPPORTED_TYPES),
        )

    role = entry.get("role", inherited.role if inherited else None)
    if role is not None and not isinstance(role, str):
        raise bad_config(f"配置 {key} 的 role 必须是字符串", config_key=key)
    if role is not None and role not in KNOWN_ROLES:
        raise bad_config(
            f"配置 {key} 的 role 未知",
            config_key=key,
            role=role,
            supported=sorted(KNOWN_ROLES),
        )

    raw_value = entry["value"]
    value_decimal: Decimal | None = None
    if value_type == "integer":
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            raise bad_config(
                f"配置 {key} 声明为 integer，但值类型不一致",
                config_key=key,
                declared_type=value_type,
                actual_type=type(raw_value).__name__,
            )
        scale_raw = entry.get("scale", inherited.scale if inherited else 1)
        scale = _expect_positive_int(scale_raw, f"configs.{key}.scale")
        if scale != 1:
            raise bad_config(
                f"integer 配置 {key} 的标度必须为 1",
                config_key=key,
                scale=scale,
            )
        value_text = str(raw_value)
        value_decimal = Decimal(raw_value)
    elif value_type == "decimal":
        value_decimal = _decimal_value(raw_value, field=f"configs.{key}.value")
        scale_raw = entry.get("scale", inherited.scale if inherited else None)
        if scale_raw is None:
            raise bad_config(f"decimal 配置 {key} 必须声明 scale", config_key=key)
        scale = _expect_positive_int(scale_raw, f"configs.{key}.scale")
        value_text = decimal_text(value_decimal)
    elif value_type == "string":
        if not isinstance(raw_value, str):
            raise bad_config(
                f"配置 {key} 声明为 string，但值类型不一致",
                config_key=key,
                actual_type=type(raw_value).__name__,
            )
        scale = 1
        value_text = raw_value
    else:
        if not isinstance(raw_value, bool):
            raise bad_config(
                f"配置 {key} 声明为 boolean，但值类型不一致",
                config_key=key,
                actual_type=type(raw_value).__name__,
            )
        scale = 1
        value_text = "true" if raw_value else "false"

    if inherited is not None:
        if value_type != inherited.value_type:
            raise bad_config(
                f"配置 {key} 不得在版本间改变 type",
                config_key=key,
                previous=inherited.value_type,
                requested=value_type,
            )
        if role != inherited.role:
            raise bad_config(
                f"配置 {key} 不得在版本间改变 role",
                config_key=key,
                previous=inherited.role,
                requested=role,
            )
        if value_type in NUMERIC_TYPES and scale != inherited.scale:
            raise bad_config(
                f"配置 {key} 不得在版本间改变 scale",
                config_key=key,
                previous=inherited.scale,
                requested=scale,
            )

    return ConfigDecl(
        key=key,
        value_type=value_type,
        value_text=value_text,
        value_decimal=value_decimal,
        scale=scale,
        role=role,
    )


def normalize_config_map(
    raw_configs: Any,
    *,
    current: dict[str, ConfigDecl] | None = None,
) -> dict[str, ConfigDecl]:
    configs_obj = _expect_object(raw_configs, "configs")
    if not configs_obj:
        raise bad_config("configs 不能为空", field="configs")
    result: dict[str, ConfigDecl] = {}
    roles: dict[str, str] = {}
    for raw_key, entry in configs_obj.items():
        key = _normalize_key(raw_key)
        inherited = current.get(key) if current else None
        decl = parse_config_entry(key, entry, inherited=inherited)
        if decl.role is not None:
            prior = roles.get(decl.role)
            if prior is not None:
                raise bad_config(
                    "role 在命名空间内必须唯一",
                    role=decl.role,
                    first_key=prior,
                    duplicate_key=key,
                )
            roles[decl.role] = key
        result[key] = decl
    return dict(sorted(result.items()))


def snapshot_digest(
    namespace: str,
    version: int,
    logical_clock_ms: int,
    configs: dict[str, ConfigDecl],
) -> str:
    return sha256_json(
        {
            "namespace": namespace,
            "version": version,
            "logical_clock_ms": logical_clock_ms,
            "configs": {key: cfg.to_dict() for key, cfg in configs.items()},
        }
    )


def normalize_snapshot(raw: Any) -> Snapshot:
    doc = _expect_object(raw, "snapshot")
    namespace = _normalize_namespace(doc.get("namespace"))
    version = _expect_positive_int(doc.get("version"), "version")
    if "logical_clock_ms" not in doc:
        raise bad_clock("快照缺少 logical_clock_ms", field="logical_clock_ms")
    clock = _expect_int(doc["logical_clock_ms"], "logical_clock_ms")
    if clock < 0:
        raise bad_clock("logical_clock_ms 不能为负", logical_clock_ms=clock)
    configs = normalize_config_map(doc.get("configs"))
    digest = snapshot_digest(namespace, version, clock, configs)
    declared_digest = doc.get("content_digest")
    if declared_digest is not None:
        if not isinstance(declared_digest, str) or declared_digest != digest:
            raise digest_mismatch(
                "快照 content_digest 与内容不一致",
                declared=declared_digest,
                computed=digest,
            )
    return Snapshot(
        namespace=namespace,
        version=version,
        logical_clock_ms=clock,
        configs=configs,
        digest=digest,
    )


def normalize_patch(raw: Any) -> ValidatedPatch:
    doc = _expect_object(raw, "patch")
    namespace = _normalize_namespace(doc.get("namespace"))
    target_version = _expect_positive_int(doc.get("target_version"), "target_version")
    base_version = doc.get("base_version")
    if base_version is not None:
        base_version = _expect_positive_int(base_version, "base_version")
    if "logical_clock_ms" not in doc:
        raise bad_clock("补丁缺少 logical_clock_ms", field="logical_clock_ms")
    clock = _expect_int(doc["logical_clock_ms"], "logical_clock_ms")
    if clock < 0:
        raise bad_clock("logical_clock_ms 不能为负", logical_clock_ms=clock)

    mode = doc.get("mode", "merge")
    if mode not in {"merge", "replace"}:
        raise bad_config("mode 必须为 merge 或 replace", mode=mode)

    raw_ops = doc.get("operations", [])
    if not isinstance(raw_ops, list):
        raise bad_config("operations 必须是数组", field="operations")
    operations: list[dict[str, Any]] = []
    seen_ops: set[str] = set()
    for index, item in enumerate(raw_ops):
        op = _expect_object(item, f"operations[{index}]")
        op_name = op.get("op")
        if op_name not in {"set", "delete"}:
            raise bad_config(
                "operation 仅支持 set / delete",
                index=index,
                operation=op_name,
            )
        key = _normalize_key(op.get("key"))
        if key in seen_ops:
            raise bad_config(
                "同一补丁不得重复操作同一配置键",
                config_key=key,
                index=index,
            )
        seen_ops.add(key)
        normalized_op = dict(op)
        normalized_op["key"] = key
        if op_name == "set" and "config" in normalized_op:
            nested = _expect_object(normalized_op.pop("config"), f"operations[{index}].config")
            normalized_op.update(nested)
        operations.append(normalized_op)

    configs_raw = doc.get("configs")
    configs = normalize_config_map(configs_raw) if configs_raw is not None else None
    if configs is not None and operations:
        raise bad_config("补丁不能同时声明 configs 与 operations")
    if configs is None and not operations and mode != "replace":
        raise bad_config("merge 补丁必须包含 operations")

    digest_input = dict(doc)
    declared_digest = digest_input.pop("content_digest", None)
    computed_digest = sha256_json(digest_input)
    if declared_digest is None:
        raise digest_mismatch(
            "补丁必须声明 content_digest",
            computed=computed_digest,
        )
    if not isinstance(declared_digest, str) or declared_digest != computed_digest:
        raise digest_mismatch(
            "补丁 content_digest 与内容不一致",
            declared=declared_digest,
            computed=computed_digest,
        )

    return ValidatedPatch(
        namespace=namespace,
        base_version=base_version,
        target_version=target_version,
        logical_clock_ms=clock,
        mode=mode,
        operations=tuple(operations),
        configs=configs,
        patch_digest=computed_digest,
        raw_content=doc,
    )


def build_patch_configs(
    patch: ValidatedPatch,
    current: dict[str, ConfigDecl],
) -> dict[str, ConfigDecl]:
    if patch.configs is not None:
        return dict(sorted(patch.configs.items()))

    working: dict[str, ConfigDecl] = dict(current)
    for index, op in enumerate(patch.operations):
        key = op["key"]
        if op["op"] == "delete":
            if key not in working:
                raise bad_config(
                    f"不能删除不存在的配置键 {key}",
                    config_key=key,
                    operation_index=index,
                )
            del working[key]
            continue
        inherited = working.get(key)
        working[key] = parse_config_entry(key, op, inherited=inherited)

    if not working:
        raise bad_config("补丁应用后 configs 不能为空")
    roles: dict[str, str] = {}
    for key, decl in sorted(working.items()):
        if decl.role is None:
            continue
        prior = roles.get(decl.role)
        if prior is not None:
            raise bad_config(
                "role 在命名空间内必须唯一",
                role=decl.role,
                first_key=prior,
                duplicate_key=key,
            )
        roles[decl.role] = key
    return dict(sorted(working.items()))


def ensure_required_roles(configs: dict[str, ConfigDecl]) -> None:
    roles = {cfg.role for cfg in configs.values() if cfg.role}
    missing = sorted(REQUIRED_ROLES - roles)
    if missing:
        raise bad_config(
            "计算所需的配置 role 不完整",
            missing_roles=missing,
            required_roles=sorted(REQUIRED_ROLES),
        )

