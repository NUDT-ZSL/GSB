"""Bidirectional migrations with explicit fill/drop/loss accounting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import IntentError
from .models import DEFAULT_THRESHOLD, ConfigDocument, IntentSpec

_WEIGHT_EPSILON = 1e-9


@dataclass(frozen=True)
class MigrationResult:
    source_version: str
    target_version: str
    document: ConfigDocument
    actions: tuple[dict[str, Any], ...] = ()
    losses: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def semantic_loss(self) -> bool:
        return bool(self.losses)

    @property
    def affected_intents(self) -> tuple[str, ...]:
        values: list[str] = []
        for intents in self.losses.values():
            for intent in intents:
                if intent not in values:
                    values.append(intent)
        return tuple(values)

    def to_report(self) -> dict[str, Any]:
        return {
            "source_version": self.source_version,
            "target_version": self.target_version,
            "semantic_loss": self.semantic_loss,
            "actions": list(self.actions),
            "losses": {
                key: list(value) for key, value in sorted(self.losses.items())
            },
            "affected_intents": list(self.affected_intents),
            "document": self.document.snapshot_summary(),
        }


def _action(
    action: str,
    field_name: str,
    spec: IntentSpec,
    *,
    step: str,
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "step": step,
        "action": action,
        "field": field_name,
        "intent": spec.intent,
        "line_no": spec.line_no,
        "matching_semantics_unchanged": True,
    }
    row.update(extra)
    return row


def _with_version(document: ConfigDocument, version: str, **changes: Any) -> ConfigDocument:
    return document.with_changes(
        source_version=version,
        checksum=None,
        **changes,
    )


def upgrade_v1_to_v2(document: ConfigDocument) -> MigrationResult:
    if document.source_version != "v1":
        raise IntentError(
            "E_CONFIG_INVALID",
            "upgrade_v1_to_v2 需要 v1 输入",
            stage="migrate",
            field="source_version",
            value=document.source_version,
        )
    actions: list[dict[str, Any]] = []
    intents: list[IntentSpec] = []
    for spec in document.intents:
        actions.append(
            _action(
                "fill",
                "threshold",
                spec,
                step="v1->v2",
                value=DEFAULT_THRESHOLD,
            )
        )
        actions.append(
            _action(
                "fill",
                "fallback_intent",
                spec,
                step="v1->v2",
                value=document.global_fallback,
            )
        )
        if spec.priority is not None:
            actions.append(
                _action(
                    "drop",
                    "priority",
                    spec,
                    step="v1->v2",
                    value=spec.priority,
                    reason="v2 不承载 priority；该字段不参与判定排序",
                )
            )
        intents.append(
            spec.with_changes(
                threshold=DEFAULT_THRESHOLD,
                fallback_intent=document.global_fallback,
                priority=None,
            )
        )
    target = _with_version(document, "v2", intents=tuple(intents))
    return MigrationResult(
        source_version="v1",
        target_version="v2",
        document=target,
        actions=tuple(actions),
        losses={},
    )


def upgrade_v2_to_v3(document: ConfigDocument) -> MigrationResult:
    if document.source_version != "v2":
        raise IntentError(
            "E_CONFIG_INVALID",
            "upgrade_v2_to_v3 需要 v2 输入",
            stage="migrate",
            field="source_version",
            value=document.source_version,
        )
    actions: list[dict[str, Any]] = []
    intents: list[IntentSpec] = []
    for spec in document.intents:
        threshold = (
            spec.threshold if spec.threshold is not None else DEFAULT_THRESHOLD
        )
        fallback = spec.fallback_intent or document.global_fallback
        label_path = (spec.intent,)
        weights = (1.0,) * len(spec.keywords)
        actions.extend(
            (
                _action(
                    "fill",
                    "threshold",
                    spec,
                    step="v2->v3",
                    value=threshold,
                ),
                _action(
                    "fill",
                    "fallback_intent",
                    spec,
                    step="v2->v3",
                    value=fallback,
                ),
                _action(
                    "fill",
                    "label_path",
                    spec,
                    step="v2->v3",
                    value=list(label_path),
                ),
                _action(
                    "fill",
                    "slots",
                    spec,
                    step="v2->v3",
                    value=[],
                ),
                _action(
                    "fill",
                    "weight_vector",
                    spec,
                    step="v2->v3",
                    value=list(weights),
                ),
            )
        )
        intents.append(
            spec.with_changes(
                threshold=threshold,
                fallback_intent=fallback,
                label_path=label_path,
                slots=(),
                weight_vector=weights,
            )
        )
    target = _with_version(document, "v3", intents=tuple(intents))
    return MigrationResult(
        source_version="v2",
        target_version="v3",
        document=target,
        actions=tuple(actions),
        losses={},
    )


def _record_loss(
    losses: dict[str, list[str]],
    field_name: str,
    intent: str,
) -> None:
    losses.setdefault(field_name, [])
    if intent not in losses[field_name]:
        losses[field_name].append(intent)


def downgrade_v3_to_v2(
    document: ConfigDocument,
    *,
    hierarchy_policy: str = "drop",
    loss_policy: str = "allow",
) -> MigrationResult:
    if document.source_version != "v3":
        raise IntentError(
            "E_CONFIG_INVALID",
            "downgrade_v3_to_v2 需要 v3 输入",
            stage="migrate",
            field="source_version",
            value=document.source_version,
        )
    if hierarchy_policy not in {"drop", "flatten", "reject"}:
        raise IntentError(
            "E_CONFIG_INVALID",
            "hierarchy_policy 必须为 drop、flatten 或 reject",
            stage="migrate",
            field="hierarchy_policy",
            value=hierarchy_policy,
        )
    if loss_policy not in {"allow", "reject"}:
        raise IntentError(
            "E_CONFIG_INVALID",
            "loss_policy 必须为 allow 或 reject",
            stage="migrate",
            field="loss_policy",
            value=loss_policy,
        )

    actions: list[dict[str, Any]] = []
    losses: dict[str, list[str]] = {}
    label_mapping: dict[str, str] = {}
    for spec in document.intents:
        path = spec.label_path or (spec.intent,)
        if path != (spec.intent,):
            _record_loss(losses, "label_path", spec.intent)
            if hierarchy_policy == "reject":
                actions.append(
                    _action(
                        "reject",
                        "label_path",
                        spec,
                        step="v3->v2",
                        value=list(path),
                    )
                )
            elif hierarchy_policy == "flatten":
                flattened = "/".join(path)
                label_mapping[spec.intent] = flattened
                actions.append(
                    _action(
                        "flatten",
                        "label_path",
                        spec,
                        step="v3->v2",
                        value=list(path),
                        flattened_intent=flattened,
                        matching_semantics_unchanged=False,
                    )
                )
            else:
                actions.append(
                    _action(
                        "drop",
                        "label_path",
                        spec,
                        step="v3->v2",
                        value=list(path),
                        reason="v2 无法表达标签层级，保留 intent 字段以维持匹配语义",
                    )
                )
        else:
            actions.append(
                _action(
                    "drop",
                    "label_path",
                    spec,
                    step="v3->v2",
                    value=list(path),
                    reason="默认单层路径可由 v2 intent 等价还原",
                )
            )
        if spec.slots:
            _record_loss(losses, "slots", spec.intent)
            if loss_policy == "reject":
                actions.append(
                    _action(
                        "reject",
                        "slots",
                        spec,
                        step="v3->v2",
                        value=[slot.to_dict() for slot in spec.slots],
                    )
                )
            else:
                actions.append(
                    _action(
                        "drop",
                        "slots",
                        spec,
                        step="v3->v2",
                        value=[slot.to_dict() for slot in spec.slots],
                        reason="v2 无法表达槽位占位符",
                    )
                )
        else:
            actions.append(
                _action(
                    "drop",
                    "slots",
                    spec,
                    step="v3->v2",
                    value=[],
                    reason="空槽位集合无信息损失",
                )
            )
        weights = spec.weight_vector or (1.0,) * len(spec.keywords)
        if any(abs(weight - 1.0) > _WEIGHT_EPSILON for weight in weights):
            _record_loss(losses, "weight_vector", spec.intent)
            if loss_policy == "reject":
                actions.append(
                    _action(
                        "reject",
                        "weight_vector",
                        spec,
                        step="v3->v2",
                        value=list(weights),
                    )
                )
            else:
                actions.append(
                    _action(
                        "drop",
                        "weight_vector",
                        spec,
                        step="v3->v2",
                        value=list(weights),
                        reason="v2 无法表达权重向量；回退为等权置信度",
                        matching_semantics_unchanged=False,
                    )
                )
        else:
            actions.append(
                _action(
                    "drop",
                    "weight_vector",
                    spec,
                    step="v3->v2",
                    value=list(weights),
                    reason="全 1 权重可由 v2 等权置信度等价还原",
                )
            )

    if losses and (loss_policy == "reject" or hierarchy_policy == "reject"):
        raise IntentError(
            "E_SEMANTIC_LOSS",
            "v3 降级到 v2 存在不可还原语义",
            stage="migrate",
            version=document.snapshot_version,
            fields=sorted(losses),
            intents=sorted({intent for values in losses.values() for intent in values}),
            unrestorable_fields={
                key: list(values) for key, values in sorted(losses.items())
            },
        )

    intents: list[IntentSpec] = []
    for spec in document.intents:
        mapped_intent = label_mapping.get(spec.intent, spec.intent)
        mapped_fallback = label_mapping.get(
            spec.fallback_intent or document.global_fallback,
            spec.fallback_intent or document.global_fallback,
        )
        intents.append(
            spec.with_changes(
                intent=mapped_intent,
                fallback_intent=mapped_fallback,
                label_path=None,
                slots=None,
                weight_vector=None,
            )
        )
    controlled_labels = frozenset(
        label_mapping.get(label, label) for label in document.controlled_labels
    )
    global_fallback = label_mapping.get(
        document.global_fallback,
        document.global_fallback,
    )
    target = _with_version(
        document,
        "v2",
        intents=tuple(intents),
        controlled_labels=controlled_labels,
        global_fallback=global_fallback,
    )
    return MigrationResult(
        source_version="v3",
        target_version="v2",
        document=target,
        actions=tuple(actions),
        losses={key: tuple(value) for key, value in losses.items()},
    )


def downgrade_v2_to_v1(
    document: ConfigDocument,
    *,
    loss_policy: str = "allow",
) -> MigrationResult:
    if document.source_version != "v2":
        raise IntentError(
            "E_CONFIG_INVALID",
            "downgrade_v2_to_v1 需要 v2 输入",
            stage="migrate",
            field="source_version",
            value=document.source_version,
        )
    if loss_policy not in {"allow", "reject"}:
        raise IntentError(
            "E_CONFIG_INVALID",
            "loss_policy 必须为 allow 或 reject",
            stage="migrate",
            field="loss_policy",
            value=loss_policy,
        )
    actions: list[dict[str, Any]] = []
    losses: dict[str, list[str]] = {}
    for index, spec in enumerate(document.intents):
        threshold = (
            spec.threshold if spec.threshold is not None else DEFAULT_THRESHOLD
        )
        fallback = spec.fallback_intent or document.global_fallback
        if abs(threshold - DEFAULT_THRESHOLD) > _WEIGHT_EPSILON:
            _record_loss(losses, "threshold", spec.intent)
        if fallback != document.global_fallback:
            _record_loss(losses, "fallback_intent", spec.intent)
        actions.append(
            _action(
                "drop",
                "threshold",
                spec,
                step="v2->v1",
                value=threshold,
                reason="v1 不承载置信门限",
                matching_semantics_unchanged=(
                    abs(threshold - DEFAULT_THRESHOLD) <= _WEIGHT_EPSILON
                ),
            )
        )
        actions.append(
            _action(
                "drop",
                "fallback_intent",
                spec,
                step="v2->v1",
                value=fallback,
                reason="v1 仅承载全局 fallback_intent",
                matching_semantics_unchanged=(
                    fallback == document.global_fallback
                ),
            )
        )
        actions.append(
            _action(
                "fill",
                "priority",
                spec,
                step="v2->v1",
                value=1000 - index,
            )
        )
    if losses and loss_policy == "reject":
        raise IntentError(
            "E_SEMANTIC_LOSS",
            "v2 降级到 v1 存在不可还原语义",
            stage="migrate",
            version=document.snapshot_version,
            fields=sorted(losses),
            intents=sorted({intent for values in losses.values() for intent in values}),
            unrestorable_fields={
                key: list(values) for key, values in sorted(losses.items())
            },
        )
    intents = tuple(
        spec.with_changes(
            threshold=None,
            fallback_intent=None,
            label_path=None,
            slots=None,
            weight_vector=None,
            priority=1000 - index,
        )
        for index, spec in enumerate(document.intents)
    )
    target = _with_version(document, "v1", intents=intents)
    return MigrationResult(
        source_version="v2",
        target_version="v1",
        document=target,
        actions=tuple(actions),
        losses={key: tuple(value) for key, value in losses.items()},
    )


def _combine(
    first: MigrationResult,
    second: MigrationResult,
) -> MigrationResult:
    losses: dict[str, list[str]] = {
        key: list(value) for key, value in first.losses.items()
    }
    for key, values in second.losses.items():
        bucket = losses.setdefault(key, [])
        for value in values:
            if value not in bucket:
                bucket.append(value)
    return MigrationResult(
        source_version=first.source_version,
        target_version=second.target_version,
        document=second.document,
        actions=first.actions + second.actions,
        losses={key: tuple(value) for key, value in losses.items()},
    )


def migrate_document(
    document: ConfigDocument,
    target_version: str,
    *,
    hierarchy_policy: str = "drop",
    loss_policy: str = "allow",
) -> MigrationResult:
    if target_version not in {"v1", "v2", "v3"}:
        raise IntentError(
            "E_VERSION_UNSUPPORTED",
            f"不支持的迁移目标版本：{target_version}",
            stage="migrate",
            version=target_version,
        )
    source = document.source_version
    if source == target_version:
        return MigrationResult(
            source_version=source,
            target_version=target_version,
            document=document,
        )

    if source == "v1" and target_version == "v2":
        return upgrade_v1_to_v2(document)
    if source == "v2" and target_version == "v3":
        return upgrade_v2_to_v3(document)
    if source == "v1" and target_version == "v3":
        to_v2 = upgrade_v1_to_v2(document)
        return _combine(to_v2, upgrade_v2_to_v3(to_v2.document))
    if source == "v2" and target_version == "v1":
        return downgrade_v2_to_v1(document, loss_policy=loss_policy)
    if source == "v3" and target_version == "v2":
        return downgrade_v3_to_v2(
            document,
            hierarchy_policy=hierarchy_policy,
            loss_policy=loss_policy,
        )
    if source == "v3" and target_version == "v1":
        to_v2 = downgrade_v3_to_v2(
            document,
            hierarchy_policy=hierarchy_policy,
            loss_policy="allow",
        )
        to_v1 = downgrade_v2_to_v1(to_v2.document, loss_policy="allow")
        result = _combine(to_v2, to_v1)
        if result.semantic_loss and (
            loss_policy == "reject" or hierarchy_policy == "reject"
        ):
            raise IntentError(
                "E_SEMANTIC_LOSS",
                "v3 降级到 v1 存在不可还原语义",
                stage="migrate",
                version=document.snapshot_version,
                fields=sorted(result.losses),
                intents=sorted(
                    {intent for values in result.losses.values() for intent in values}
                ),
                unrestorable_fields={
                    key: list(values)
                    for key, values in sorted(result.losses.items())
                },
            )
        return result
    raise IntentError(
        "E_VERSION_UNSUPPORTED",
        f"不支持迁移路径：{source} -> {target_version}",
        stage="migrate",
        version=source,
        target_version=target_version,
    )


def prepare_for_matching(document: ConfigDocument) -> MigrationResult:
    """Upgrade any source form to the canonical v3 matching document."""

    return migrate_document(
        document,
        "v3",
        hierarchy_policy="drop",
        loss_policy="allow",
    )
