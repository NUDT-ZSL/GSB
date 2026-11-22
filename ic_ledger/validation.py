"""Strict input parsing and business validation."""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable

from .errors import ErrorCode, business_error
from .models import (
    CoalesceParams,
    EngineConfig,
    FlowRecord,
    QueueSpec,
    SampleDocument,
    canonical_json,
    queue_key,
    sha256_json,
)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise business_error(ErrorCode.SCHEMA, f"{path} must be an object", path=path)
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise business_error(ErrorCode.SCHEMA, f"{path} must be an array", path=path)
    return value


def _required(mapping: dict[str, Any], names: Iterable[str], path: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    raise business_error(
        ErrorCode.SCHEMA,
        f"{path} is missing required field {'/'.join(names)}",
        path=path,
        fields=list(names),
    )


def _positive_int(value: Any, code: str, path: str) -> int:
    if not _is_int(value) or value <= 0:
        raise business_error(code, f"{path} must be a positive integer", path=path, value=value)
    return value


def _nonnegative_int(value: Any, code: str, path: str) -> int:
    if not _is_int(value) or value < 0:
        raise business_error(code, f"{path} must be a non-negative integer", path=path, value=value)
    return value


def _parse_fraction(value: Any, code: str, path: str, *, positive: bool = False) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise business_error(code, f"{path} must be a rational number", path=path, value=value)
    try:
        result = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise business_error(
            code, f"{path} must be a rational number", path=path, value=value
        ) from exc
    if positive and result <= 0:
        raise business_error(code, f"{path} must be positive", path=path, value=value)
    if result < 0:
        raise business_error(code, f"{path} must be non-negative", path=path, value=value)
    return result


def _parse_params(value: Any, path: str) -> CoalesceParams:
    mapping = _require_mapping(value, path)
    return CoalesceParams(
        rx_usecs=_nonnegative_int(
            _required(mapping, ("rx_usecs", "rx-usecs"), f"{path}.rx_usecs"),
            ErrorCode.RX_USECS,
            f"{path}.rx_usecs",
        ),
        rx_frames=_nonnegative_int(
            _required(mapping, ("rx_frames", "rx-frames"), f"{path}.rx_frames"),
            ErrorCode.RX_FRAMES,
            f"{path}.rx_frames",
        ),
        tx_usecs=_nonnegative_int(
            _required(mapping, ("tx_usecs", "tx-usecs"), f"{path}.tx_usecs"),
            ErrorCode.TX_USECS,
            f"{path}.tx_usecs",
        ),
        tx_frames=_nonnegative_int(
            _required(mapping, ("tx_frames", "tx-frames"), f"{path}.tx_frames"),
            ErrorCode.TX_FRAMES,
            f"{path}.tx_frames",
        ),
    )


def _parse_external_params(raw: dict[str, Any]) -> dict[str, CoalesceParams]:
    external: dict[str, CoalesceParams] = {}
    value = raw.get("coalescing", raw.get("params"))
    if value is None:
        return external
    entries: list[tuple[str, Any, str]]
    if isinstance(value, dict):
        entries = []
        for key, item in value.items():
            entries.append((str(key), item, f"coalescing.{key}"))
    else:
        entry_list = _require_list(value, "coalescing")
        entries = []
        for index, item in enumerate(entry_list):
            mapping = _require_mapping(item, f"coalescing[{index}]")
            iface = _required(mapping, ("iface", "interface"), f"coalescing[{index}]")
            queue = _required(mapping, ("queue", "queue_id"), f"coalescing[{index}]")
            if not isinstance(iface, str) or not iface:
                raise business_error(
                    ErrorCode.IFACE_NAME, "coalescing iface must be a non-empty string"
                )
            if not _is_int(queue):
                raise business_error(ErrorCode.QUEUE_INDEX, "coalescing queue must be an integer")
            entries.append((queue_key(iface, queue), mapping, f"coalescing[{index}]"))
    for key, item, path in entries:
        if key in external:
            raise business_error(
                ErrorCode.PARAM_DUPLICATE_CONFLICT,
                f"duplicate coalescing entry for {key}",
                queue=key,
            )
        if ":" not in key:
            raise business_error(
                ErrorCode.PARAM_DUPLICATE_CONFLICT,
                f"coalescing key {key} must use iface:queue",
                queue=key,
            )
        external[key] = _parse_params(item, path)
    return external


def _parse_queues(raw: dict[str, Any]) -> tuple[list[QueueSpec], dict[str, Any]]:
    external = _parse_external_params(raw)
    queues: list[QueueSpec] = []
    seen: set[str] = set()
    interfaces = _require_list(_required(raw, ("interfaces",), "interfaces"), "interfaces")
    interface_names: set[str] = set()
    for iface_index, iface_item in enumerate(interfaces):
        iface_map = _require_mapping(iface_item, f"interfaces[{iface_index}]")
        iface = _required(iface_map, ("name", "iface"), f"interfaces[{iface_index}]")
        if not isinstance(iface, str) or not iface.strip():
            raise business_error(
                ErrorCode.IFACE_NAME,
                f"interfaces[{iface_index}].name must be a non-empty string",
            )
        if iface in interface_names:
            raise business_error(
                ErrorCode.IFACE_DUPLICATE,
                f"duplicate interface {iface}",
                iface=iface,
            )
        interface_names.add(iface)
        queue_items = _require_list(iface_map.get("queues", []), f"interfaces[{iface_index}].queues")
        for queue_index, queue_item in enumerate(queue_items):
            queue_map = _require_mapping(
                queue_item, f"interfaces[{iface_index}].queues[{queue_index}]"
            )
            queue = _required(
                queue_map,
                ("queue", "queue_id"),
                f"interfaces[{iface_index}].queues[{queue_index}]",
            )
            if not _is_int(queue) or queue < 0:
                raise business_error(
                    ErrorCode.QUEUE_INDEX,
                    f"interfaces[{iface_index}].queues[{queue_index}].queue "
                    "must be a non-negative integer",
                    queue=queue,
                )
            key = queue_key(iface, queue)
            if key in seen:
                raise business_error(
                    ErrorCode.QUEUE_DUPLICATE,
                    f"duplicate queue {key}",
                    queue=key,
                )
            seen.add(key)
            weight = _positive_int(
                _required(queue_map, ("weight",), f"queues[{key}].weight"),
                ErrorCode.QUEUE_WEIGHT,
                f"queues[{key}].weight",
            )
            max_irq_rate = _positive_int(
                _required(
                    queue_map,
                    ("max_irq_rate", "max_irq_rate_per_second", "irq_rate_limit"),
                    f"queues[{key}].max_irq_rate",
                ),
                ErrorCode.MAX_IRQ_RATE,
                f"queues[{key}].max_irq_rate",
            )
            nested = queue_map.get("coalesce", queue_map.get("params"))
            if nested is not None:
                params = _parse_params(nested, f"queues[{key}].coalesce")
                if key in external and external[key] != params:
                    raise business_error(
                        ErrorCode.PARAM_DUPLICATE_CONFLICT,
                        f"nested and external coalescing parameters differ for {key}",
                        queue=key,
                    )
                external.pop(key, None)
            elif key in external:
                params = external.pop(key)
            elif all(
                field in queue_map
                for field in ("rx_usecs", "rx_frames", "tx_usecs", "tx_frames")
            ):
                params = _parse_params(queue_map, f"queues[{key}].params")
            else:
                raise business_error(
                    ErrorCode.SCHEMA,
                    f"queue {key} is missing coalescing parameters",
                    queue=key,
                )
            queues.append(
                QueueSpec(
                    iface=iface,
                    queue=queue,
                    weight=weight,
                    max_irq_rate=max_irq_rate,
                    params=params,
                )
            )

    if external:
        unknown = sorted(external)
        raise business_error(
            ErrorCode.QUEUE_NOT_FOUND,
            f"coalescing parameters reference unknown queue {unknown[0]}",
            queue=unknown[0],
        )
    if not queues:
        raise business_error(ErrorCode.SCHEMA, "at least one queue is required")
    return queues, raw


def _parse_config(raw: dict[str, Any], queues: list[QueueSpec]) -> EngineConfig:
    value = _required(raw, ("config", "accounting_config"), "config")
    config_map = _require_mapping(value, "config")
    fixed_scale = _positive_int(
        _required(
            config_map,
            ("fixed_scale", "time_scale", "ticks_per_us"),
            "config.fixed_scale",
        ),
        ErrorCode.CONFIG_FIXED_SCALE,
        "config.fixed_scale",
    )
    avg_interval = _parse_fraction(
        _required(
            config_map,
            ("avg_frame_interval_us", "frame_interval_us"),
            "config.avg_frame_interval_us",
        ),
        ErrorCode.CONFIG_FRAME_INTERVAL,
        "config.avg_frame_interval_us",
        positive=True,
    )
    tolerance = _parse_fraction(
        config_map.get("float_tolerance", "1e-12"),
        ErrorCode.CONFIG_TOLERANCE,
        "config.float_tolerance",
    )
    error_bound = _parse_fraction(
        config_map.get("error_bound_budget_us", config_map.get("error_bound_budget", "0")),
        ErrorCode.CONFIG_ERROR_BOUND,
        "config.error_bound_budget_us",
    )
    clock_step = _positive_int(
        _required(config_map, ("clock_step_us",), "config.clock_step_us"),
        ErrorCode.CONFIG_CLOCK_STEP,
        "config.clock_step_us",
    )
    budget_value = config_map.get(
        "irq_budget_per_second",
        config_map.get("irq_budget", config_map.get("budgets", {})),
    )
    budgets: dict[str, int] = {}
    if budget_value is not None:
        if not isinstance(budget_value, dict):
            raise business_error(
                ErrorCode.CONFIG_IRQ_BUDGET,
                "config.irq_budget_per_second must be an object",
            )
        valid_keys = {item.key for item in queues}
        for key, budget in budget_value.items():
            if not isinstance(key, str) or key not in valid_keys:
                raise business_error(
                    ErrorCode.CONFIG_IRQ_BUDGET,
                    f"irq budget references unknown queue {key}",
                    queue=key,
                )
            budgets[key] = _positive_int(
                budget,
                ErrorCode.CONFIG_IRQ_BUDGET,
                f"config.irq_budget_per_second.{key}",
            )
    return EngineConfig(
        fixed_scale=fixed_scale,
        avg_frame_interval_us=avg_interval,
        float_tolerance=tolerance,
        error_bound_budget_us=error_bound,
        clock_step_us=clock_step,
        irq_budget_per_second=budgets,
    )


def _parse_flows(raw: dict[str, Any], queues: list[QueueSpec]) -> list[FlowRecord]:
    traffic_value = _required(raw, ("traffic", "flows", "records"), "traffic")
    traffic = _require_list(traffic_value, "traffic")
    valid_queues = {item.key for item in queues}
    interface_names = {item.iface for item in queues}
    flows: list[FlowRecord] = []
    previous_seq: int | None = None
    last_clock_by_queue: dict[str, int] = {}
    for index, item in enumerate(traffic):
        mapping = _require_mapping(item, f"traffic[{index}]")
        record_seq = _required(mapping, ("record_seq", "seq", "record_id"), f"traffic[{index}]")
        if not _is_int(record_seq):
            raise business_error(
                ErrorCode.RECORD_SEQ,
                f"traffic[{index}].record_seq must be an integer",
                index=index,
            )
        if previous_seq is not None and record_seq <= previous_seq:
            raise business_error(
                ErrorCode.RECORD_SEQ,
                "record_seq must be strictly increasing",
                previous=previous_seq,
                record_seq=record_seq,
            )
        previous_seq = record_seq
        iface = mapping.get("iface", mapping.get("interface"))
        if iface is None and len(interface_names) == 1:
            iface = next(iter(interface_names))
        if not isinstance(iface, str) or not iface:
            raise business_error(
                ErrorCode.IFACE_NAME,
                f"traffic[{index}].iface must be a non-empty string",
                index=index,
            )
        queue = _required(mapping, ("queue", "queue_id"), f"traffic[{index}].queue")
        if not _is_int(queue):
            raise business_error(
                ErrorCode.QUEUE_INDEX,
                f"traffic[{index}].queue must be an integer",
                index=index,
            )
        key = queue_key(iface, queue)
        if key not in valid_queues:
            raise business_error(
                ErrorCode.RECORD_QUEUE,
                f"traffic[{index}] references unknown queue {key}",
                queue=key,
                record_seq=record_seq,
            )
        clock = _required(
            mapping,
            ("logical_clock_us", "clock_us", "injected_clock_us"),
            f"traffic[{index}].logical_clock_us",
        )
        if not _is_int(clock) or clock < 0:
            raise business_error(
                ErrorCode.RECORD_CLOCK,
                f"traffic[{index}].logical_clock_us must be a non-negative integer",
                record_seq=record_seq,
            )
        last_clock = last_clock_by_queue.get(key)
        if last_clock is not None and clock < last_clock:
            raise business_error(
                ErrorCode.RECORD_CLOCK,
                f"logical clock moved backwards for queue {key}",
                queue=key,
                previous=last_clock,
                current=clock,
                record_seq=record_seq,
            )
        last_clock_by_queue[key] = clock
        frames = _required(
            mapping,
            ("arrival_frames", "frames"),
            f"traffic[{index}].arrival_frames",
        )
        if not _is_int(frames) or frames < 0:
            raise business_error(
                ErrorCode.ARRIVAL_FRAMES,
                f"traffic[{index}].arrival_frames must be a non-negative integer",
                record_seq=record_seq,
            )
        frame_bytes = _required(
            mapping,
            ("frame_bytes", "bytes"),
            f"traffic[{index}].frame_bytes",
        )
        if not _is_int(frame_bytes) or frame_bytes <= 0:
            raise business_error(
                ErrorCode.FRAME_BYTES,
                f"traffic[{index}].frame_bytes must be a positive integer",
                record_seq=record_seq,
            )
        direction = mapping.get("direction", "rx")
        if direction not in ("rx", "tx"):
            raise business_error(
                ErrorCode.RECORD_DIRECTION,
                f"traffic[{index}].direction must be rx or tx",
                record_seq=record_seq,
            )
        flows.append(
            FlowRecord(
                record_seq=record_seq,
                iface=iface,
                queue=queue,
                logical_clock_us=clock,
                arrival_frames=frames,
                frame_bytes=frame_bytes,
                direction=direction,
            )
        )
    return flows


def parse_document(raw: Any) -> SampleDocument:
    if not isinstance(raw, dict):
        raise business_error(ErrorCode.SCHEMA, "top-level JSON must be an object")
    queues, _ = _parse_queues(raw)
    config = _parse_config(raw, queues)
    flows = _parse_flows(raw, queues)
    queues = sorted(queues, key=lambda item: (item.iface, item.queue))
    input_hash = sha256_json(raw)
    return SampleDocument(
        queues=tuple(queues),
        flows=tuple(flows),
        config=config,
        input_hash=input_hash,
        raw=raw,
    )


def load_document(path: str | Path) -> SampleDocument:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise business_error(
            ErrorCode.INPUT_NOT_FOUND,
            f"input file not found: {source}",
            path=str(source),
        ) from exc
    except OSError as exc:
        raise business_error(
            ErrorCode.INPUT_NOT_FOUND,
            f"cannot read input file: {source}",
            path=str(source),
            reason=str(exc),
        ) from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise business_error(
            ErrorCode.INPUT_JSON,
            f"invalid JSON in {source}",
            line=exc.lineno,
            column=exc.colno,
            reason=exc.msg,
        ) from exc
    return parse_document(raw)


def parse_payload(payload: str) -> SampleDocument:
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise business_error(
            ErrorCode.INPUT_JSON,
            "invalid staged JSON payload",
            reason=exc.msg,
        ) from exc
    return parse_document(raw)


def canonical_document_payload(document: SampleDocument) -> str:
    return canonical_json(document.raw)


def document_summary(document: SampleDocument) -> dict[str, Any]:
    return {
        "config": document.config.as_dict(),
        "flow_records": len(document.flows),
        "input_hash": document.input_hash,
        "interfaces": len({item.iface for item in document.queues}),
        "queues": len(document.queues),
    }
