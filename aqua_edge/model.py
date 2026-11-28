"""Project loading and validation."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import AquaError


PROJECT_VERSION = 1
PROJECT_FORMAT = "aqua-edge-project"
SENSOR_KINDS = {"sensor", "telemetry"}
CONTROL_KINDS = {"control", "command"}
BULK_KINDS = {"bulk", "history", "bulk_history"}


@dataclass
class Frame:
    frame_id: str
    stream_id: str
    sequence: int
    logical_time_ms: float
    length_bytes: int
    ready: bool = True

    @property
    def ready_time_ms(self) -> float:
        return self.logical_time_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "logical_time_ms": _number(self.logical_time_ms),
            "length_bytes": self.length_bytes,
            "ready": self.ready,
        }


@dataclass
class Stream:
    stream_id: str
    pond_id: str
    kind: str
    priority: int
    window_bytes: int
    sensor_type: str | None = None
    target_device: str | None = None
    deadline_ms: float | None = None
    frames: list[Frame] = field(default_factory=list)

    @property
    def is_control(self) -> bool:
        return self.kind in CONTROL_KINDS

    @property
    def is_sensor(self) -> bool:
        return self.kind in SENSOR_KINDS

    @property
    def is_bulk(self) -> bool:
        return self.kind in BULK_KINDS

    @property
    def label(self) -> str:
        if self.is_control:
            target = self.target_device or "unknown-device"
            return f"{self.pond_id} / 控制 / {target}"
        if self.is_sensor:
            sensor = self.sensor_type or "unknown-sensor"
            return f"{self.pond_id} / 传感器 / {sensor}"
        return f"{self.pond_id} / {self.kind}"

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "stream_id": self.stream_id,
            "pond_id": self.pond_id,
            "kind": self.kind,
            "priority": self.priority,
            "window_bytes": self.window_bytes,
            "frames": [frame.to_dict() for frame in self.frames],
        }
        if self.sensor_type is not None:
            result["sensor_type"] = self.sensor_type
        if self.target_device is not None:
            result["target_device"] = self.target_device
        if self.deadline_ms is not None:
            result["deadline_ms"] = _number(self.deadline_ms)
        return result


@dataclass
class ConsumerEvent:
    time_ms: float
    read_bytes_per_s: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "time_ms": _number(self.time_ms),
            "read_bytes_per_s": _number(self.read_bytes_per_s),
        }


@dataclass
class ConsumerConfig:
    read_bytes_per_s: float
    events: list[ConsumerEvent] = field(default_factory=list)

    @property
    def read_bytes_per_ms(self) -> float:
        return self.read_bytes_per_s / 1000.0

    def rate_at(self, time_ms: float) -> float:
        rate = self.read_bytes_per_s
        for event in self.events:
            if event.time_ms <= time_ms + 1e-9:
                rate = event.read_bytes_per_s
            else:
                break
        return rate

    def rate_changes(self) -> list[ConsumerEvent]:
        return list(self.events)

    def to_dict(self) -> dict[str, Any]:
        return {
            "read_bytes_per_s": _number(self.read_bytes_per_s),
            "events": [event.to_dict() for event in self.events],
        }


@dataclass
class NetworkConfig:
    uplink_bytes_per_s: float
    rtt_ms: float
    max_concurrent_streams: int
    frame_size_limit_bytes: int

    @property
    def uplink_bytes_per_ms(self) -> float:
        return self.uplink_bytes_per_s / 1000.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "uplink_bytes_per_s": _number(self.uplink_bytes_per_s),
            "rtt_ms": _number(self.rtt_ms),
            "max_concurrent_streams": self.max_concurrent_streams,
            "frame_size_limit_bytes": self.frame_size_limit_bytes,
        }


@dataclass
class Outage:
    start_ms: float
    end_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {"start_ms": _number(self.start_ms), "end_ms": _number(self.end_ms)}


@dataclass
class Project:
    name: str
    network: NetworkConfig
    consumer: ConsumerConfig
    outages: list[Outage]
    streams: list[Stream]
    seed: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total_frames(self) -> int:
        return sum(len(stream.frames) for stream in self.streams)

    def stream_map(self) -> dict[str, Stream]:
        return {stream.stream_id: stream for stream in self.streams}

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "format": PROJECT_FORMAT,
            "project_version": PROJECT_VERSION,
            "name": self.name,
            "network": self.network.to_dict(),
            "consumer": self.consumer.to_dict(),
            "outages": [outage.to_dict() for outage in self.outages],
            "streams": [stream.to_dict() for stream in self.streams],
        }
        if self.seed is not None:
            result["seed"] = self.seed
        if self.metadata:
            result["metadata"] = self.metadata
        return result


def load_project(path: str | Path) -> Project:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise AquaError("E_IO", "工程文件不存在", {"path": str(path)}) from exc
    except json.JSONDecodeError as exc:
        raise AquaError(
            "E_JSON",
            "工程文件不是合法 JSON",
            {"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc
    return parse_project(data)


def parse_project(data: Any) -> Project:
    if not isinstance(data, dict):
        raise AquaError("E_PROJECT", "工程根节点必须是 JSON 对象")

    version = data.get("project_version", PROJECT_VERSION)
    if version != PROJECT_VERSION:
        raise AquaError(
            "E_PROJECTVERSION",
            f"不支持的工程版本，期望 {PROJECT_VERSION}",
            {"found": version, "expected": PROJECT_VERSION},
        )
    project_format = data.get("format", PROJECT_FORMAT)
    if project_format != PROJECT_FORMAT:
        raise AquaError(
            "E_PROJECT",
            "工程 format 不匹配",
            {"found": project_format, "expected": PROJECT_FORMAT},
        )

    network = _parse_network(_required_mapping(data, "network"))
    consumer = _parse_consumer(_required_mapping(data, "consumer"))
    outages = _parse_outages(data.get("outages", []))
    raw_streams = data.get("streams")
    if not isinstance(raw_streams, list) or not raw_streams:
        raise AquaError("E_PROJECT", "streams 必须是非空数组")

    flat_frames = data.get("frames")
    if flat_frames is not None and not isinstance(flat_frames, list):
        raise AquaError("E_PROJECT", "frames 必须是数组")

    parsed_streams: list[Stream] = []
    seen: set[str] = set()
    for index, raw_stream in enumerate(raw_streams):
        if not isinstance(raw_stream, dict):
            raise AquaError("E_PROJECT", "流定义必须是 JSON 对象", {"stream_index": index})
        stream_id = _required_text(raw_stream, "stream_id", "流定义")
        if stream_id in seen:
            raise AquaError(
                "E_DUPSTREAM",
                "流标识重复定义",
                {"stream_id": stream_id, "stream_index": index},
            )
        seen.add(stream_id)
        parsed_streams.append(_parse_stream(raw_stream, network, index))

    if flat_frames is not None:
        if any(stream.frames for stream in parsed_streams):
            raise AquaError(
                "E_PROJECT",
                "frames 与流内 frames 不能同时提供",
            )
        _attach_flat_frames(parsed_streams, flat_frames, seen, network)
    elif any(not stream.frames for stream in parsed_streams):
        missing = next(stream.stream_id for stream in parsed_streams if not stream.frames)
        raise AquaError("E_PROJECT", "每条流至少需要一个帧", {"stream_id": missing})

    for stream in parsed_streams:
        _validate_stream_frames(stream, network)

    name = data.get("name", "未命名塘口网络工程")
    if not isinstance(name, str) or not name.strip():
        raise AquaError("E_PROJECT", "name 必须是非空字符串")
    seed = data.get("seed")
    if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool)):
        raise AquaError("E_PROJECT", "seed 必须是整数")
    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict):
        raise AquaError("E_PROJECT", "metadata 必须是 JSON 对象")

    return Project(
        name=name,
        network=network,
        consumer=consumer,
        outages=outages,
        streams=parsed_streams,
        seed=seed,
        metadata=metadata,
    )


def _parse_network(raw: dict[str, Any]) -> NetworkConfig:
    uplink = raw.get("uplink_bytes_per_s")
    if uplink is None and "uplink_bps" in raw:
        uplink = _number_value(raw["uplink_bps"], "network.uplink_bps") / 8.0
    if uplink is None and "uplink_mbps" in raw:
        uplink = _number_value(raw["uplink_mbps"], "network.uplink_mbps") * 125000.0
    if uplink is None and "uplink_bytes_per_ms" in raw:
        uplink = _number_value(raw["uplink_bytes_per_ms"], "network.uplink_bytes_per_ms") * 1000.0
    uplink_value = _positive_number(uplink, "network.uplink_bytes_per_s")
    rtt = _nonnegative_number(raw.get("rtt_ms"), "network.rtt_ms")
    max_streams = _positive_int(raw.get("max_concurrent_streams"), "network.max_concurrent_streams")
    limit = _positive_int(raw.get("frame_size_limit_bytes"), "network.frame_size_limit_bytes")
    return NetworkConfig(
        uplink_bytes_per_s=uplink_value,
        rtt_ms=rtt,
        max_concurrent_streams=max_streams,
        frame_size_limit_bytes=limit,
    )


def _parse_consumer(raw: dict[str, Any]) -> ConsumerConfig:
    rate = raw.get("read_bytes_per_s")
    if rate is None:
        rate = raw.get("read_bps")
    if rate is None and "read_bytes_per_ms" in raw:
        rate = _number_value(raw["read_bytes_per_ms"], "consumer.read_bytes_per_ms") * 1000.0
    base_rate = _positive_number(rate, "consumer.read_bytes_per_s")
    raw_events = raw.get("events", [])
    if not isinstance(raw_events, list):
        raise AquaError("E_PROJECT", "consumer.events 必须是数组")
    events: list[ConsumerEvent] = []
    previous = -1.0
    for index, raw_event in enumerate(raw_events):
        if not isinstance(raw_event, dict):
            raise AquaError(
                "E_PROJECT",
                "consumer.events 元素必须是对象",
                {"event_index": index},
            )
        time_ms = _nonnegative_number(raw_event.get("time_ms"), f"consumer.events[{index}].time_ms")
        event_rate = raw_event.get("read_bytes_per_s", raw_event.get("read_bps"))
        if event_rate is None and "read_bytes_per_ms" in raw_event:
            event_rate = (
                _number_value(
                    raw_event["read_bytes_per_ms"],
                    f"consumer.events[{index}].read_bytes_per_ms",
                )
                * 1000.0
            )
        event_rate = _positive_number(event_rate, f"consumer.events[{index}].read_bytes_per_s")
        if time_ms < previous:
            raise AquaError(
                "E_ORDER",
                "消费者读取速率事件必须按逻辑时刻升序排列",
                {"event_index": index, "time_ms": time_ms, "previous_time_ms": previous},
            )
        previous = time_ms
        events.append(ConsumerEvent(time_ms=time_ms, read_bytes_per_s=event_rate))
    return ConsumerConfig(read_bytes_per_s=base_rate, events=events)


def _parse_outages(raw_outages: Any) -> list[Outage]:
    if not isinstance(raw_outages, list):
        raise AquaError("E_PROJECT", "outages 必须是数组")
    outages: list[Outage] = []
    previous_end = -1.0
    for index, raw in enumerate(raw_outages):
        if not isinstance(raw, dict):
            raise AquaError("E_PROJECT", "outages 元素必须是对象", {"outage_index": index})
        start = _nonnegative_number(raw.get("start_ms"), f"outages[{index}].start_ms")
        end = _nonnegative_number(raw.get("end_ms"), f"outages[{index}].end_ms")
        if end <= start:
            raise AquaError(
                "E_ORDER",
                "瞬断结束时刻必须晚于开始时刻",
                {"outage_index": index, "start_ms": start, "end_ms": end},
            )
        if start < previous_end:
            raise AquaError(
                "E_ORDER",
                "瞬断事件必须按逻辑时刻升序且不得重叠",
                {"outage_index": index, "start_ms": start, "previous_end_ms": previous_end},
            )
        previous_end = end
        outages.append(Outage(start_ms=start, end_ms=end))
    return outages


def _parse_stream(raw: dict[str, Any], network: NetworkConfig, index: int) -> Stream:
    stream_id = _required_text(raw, "stream_id", "流定义")
    pond_id = _required_text(raw, "pond_id", f"流 {stream_id}")
    kind = _required_text(raw, "kind", f"流 {stream_id}").lower()
    priority = _integer(raw.get("priority"), f"{stream_id}.priority")
    window_bytes = _positive_int(raw.get("window_bytes"), f"{stream_id}.window_bytes")

    sensor_type = _optional_text(raw.get("sensor_type"), f"{stream_id}.sensor_type")
    target_device = _optional_text(raw.get("target_device"), f"{stream_id}.target_device")
    deadline = raw.get("deadline_ms", raw.get("delivery_deadline_ms"))
    deadline_ms = None if deadline is None else _positive_number(deadline, f"{stream_id}.deadline_ms")

    if kind in SENSOR_KINDS and sensor_type is None:
        raise AquaError(
            "E_PROJECT",
            "传感器流必须声明 sensor_type",
            {"stream_id": stream_id, "stream_index": index},
        )
    if kind in CONTROL_KINDS and target_device is None:
        raise AquaError(
            "E_PROJECT",
            "控制指令流必须声明 target_device",
            {"stream_id": stream_id, "stream_index": index},
        )
    if kind in CONTROL_KINDS and deadline_ms is None:
        raise AquaError(
            "E_PROJECT",
            "控制指令流必须声明 deadline_ms",
            {"stream_id": stream_id, "stream_index": index},
        )

    raw_frames = raw.get("frames", [])
    if not isinstance(raw_frames, list):
        raise AquaError("E_PROJECT", "流 frames 必须是数组", {"stream_id": stream_id})
    frames: list[Frame] = []
    previous_time = -1.0
    for frame_index, raw_frame in enumerate(raw_frames):
        if not isinstance(raw_frame, dict):
            raise AquaError(
                "E_PROJECT",
                "帧必须是 JSON 对象",
                {"stream_id": stream_id, "frame_index": frame_index},
            )
        logical_time = _frame_time(raw_frame, stream_id, frame_index)
        if logical_time < previous_time:
            raise AquaError(
                "E_ORDER",
                "同一流的逻辑时刻倒序或帧序号与时间顺序不一致",
                {
                    "stream_id": stream_id,
                    "frame_index": frame_index,
                    "logical_time_ms": logical_time,
                    "previous_time_ms": previous_time,
                },
            )
        previous_time = logical_time
        length = _frame_length(raw_frame, stream_id, frame_index)
        ready = raw_frame.get("ready", True)
        if not isinstance(ready, bool):
            raise AquaError(
                "E_PROJECT",
                "帧 ready 标记必须是布尔值",
                {"stream_id": stream_id, "frame_index": frame_index},
            )
        frame_id = _frame_id(raw_frame, stream_id, frame_index)
        frames.append(
            Frame(
                frame_id=frame_id,
                stream_id=stream_id,
                sequence=frame_index,
                logical_time_ms=logical_time,
                length_bytes=length,
                ready=ready,
            )
        )

    return Stream(
        stream_id=stream_id,
        pond_id=pond_id,
        kind=kind,
        priority=priority,
        window_bytes=window_bytes,
        sensor_type=sensor_type,
        target_device=target_device,
        deadline_ms=deadline_ms,
        frames=frames,
    )


def _attach_flat_frames(
    streams: list[Stream],
    raw_frames: list[Any],
    stream_ids: set[str],
    network: NetworkConfig,
) -> None:
    by_id = {stream.stream_id: stream for stream in streams}
    frame_ids: set[str] = set()
    previous_time = -1.0
    for frame_index, raw_frame in enumerate(raw_frames):
        if not isinstance(raw_frame, dict):
            raise AquaError(
                "E_PROJECT",
                "frames 元素必须是 JSON 对象",
                {"frame_index": frame_index},
            )
        stream_id = raw_frame.get("stream_id", raw_frame.get("stream"))
        if not isinstance(stream_id, str) or stream_id not in stream_ids:
            raise AquaError(
                "E_PROJECT",
                "帧引用了未定义的流标识",
                {"frame_index": frame_index, "stream_id": stream_id},
            )
        logical_time = _frame_time(raw_frame, stream_id, frame_index)
        if logical_time < previous_time:
            raise AquaError(
                "E_ORDER",
                "全局帧逻辑时刻倒序",
                {
                    "frame_index": frame_index,
                    "logical_time_ms": logical_time,
                    "previous_time_ms": previous_time,
                },
            )
        previous_time = logical_time
        stream = by_id[stream_id]
        length = _frame_length(raw_frame, stream_id, frame_index)
        ready = raw_frame.get("ready", True)
        if not isinstance(ready, bool):
            raise AquaError(
                "E_PROJECT",
                "帧 ready 标记必须是布尔值",
                {"frame_index": frame_index, "stream_id": stream_id},
            )
        frame_id = _frame_id(raw_frame, stream_id, len(stream.frames))
        if frame_id in frame_ids:
            raise AquaError(
                "E_FRAMEDUP",
                "帧标识重复定义",
                {"frame_id": frame_id, "frame_index": frame_index},
            )
        frame_ids.add(frame_id)
        stream.frames.append(
            Frame(
                frame_id=frame_id,
                stream_id=stream_id,
                sequence=len(stream.frames),
                logical_time_ms=logical_time,
                length_bytes=length,
                ready=ready,
            )
        )


def _validate_stream_frames(stream: Stream, network: NetworkConfig) -> None:
    frame_ids: set[str] = set()
    previous_time = -1.0
    for frame in stream.frames:
        if frame.logical_time_ms < previous_time:
            raise AquaError(
                "E_ORDER",
                "同一流的逻辑时刻倒序",
                {
                    "stream_id": stream.stream_id,
                    "frame_id": frame.frame_id,
                    "logical_time_ms": frame.logical_time_ms,
                    "previous_time_ms": previous_time,
                },
            )
        previous_time = frame.logical_time_ms
        if frame.length_bytes <= 0:
            raise AquaError(
                "E_FRAMELEN",
                "帧长度必须为正整数",
                {"stream_id": stream.stream_id, "frame_id": frame.frame_id, "length_bytes": frame.length_bytes},
            )
        if frame.length_bytes > network.frame_size_limit_bytes:
            raise AquaError(
                "E_FRAMELIMIT",
                "帧长度超过声明的帧大小上限",
                {
                    "stream_id": stream.stream_id,
                    "frame_id": frame.frame_id,
                    "length_bytes": frame.length_bytes,
                    "frame_size_limit_bytes": network.frame_size_limit_bytes,
                },
            )
        if frame.frame_id in frame_ids:
            raise AquaError(
                "E_FRAMEDUP",
                "同一流内帧标识重复定义",
                {"stream_id": stream.stream_id, "frame_id": frame.frame_id},
            )
        frame_ids.add(frame.frame_id)


def _required_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise AquaError("E_PROJECT", f"{key} 必须是 JSON 对象")
    return value


def _required_text(data: dict[str, Any], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AquaError("E_PROJECT", f"{context}.{key} 必须是非空字符串")
    return value.strip()


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AquaError("E_PROJECT", f"{field_name} 必须是非空字符串")
    return value.strip()


def _frame_time(raw: dict[str, Any], stream_id: str, index: int) -> float:
    value = raw.get("logical_time_ms", raw.get("time_ms", raw.get("ready_time_ms")))
    return _nonnegative_number(value, f"{stream_id}.frames[{index}].logical_time_ms")


def _frame_length(raw: dict[str, Any], stream_id: str, index: int) -> int:
    value = raw.get("length_bytes", raw.get("size_bytes", raw.get("frame_length_bytes")))
    if isinstance(value, bool) or not isinstance(value, int):
        raise AquaError(
            "E_FRAMELEN",
            "帧长度必须为正整数",
            {"stream_id": stream_id, "frame_index": index, "length_bytes": value},
        )
    if value <= 0:
        raise AquaError(
            "E_FRAMELEN",
            "帧长度必须为正整数",
            {"stream_id": stream_id, "frame_index": index, "length_bytes": value},
        )
    return value


def _frame_id(raw: dict[str, Any], stream_id: str, index: int) -> str:
    value = raw.get("frame_id")
    if value is None:
        return f"{stream_id}#{index + 1:04d}"
    if not isinstance(value, str) or not value.strip():
        raise AquaError(
            "E_PROJECT",
            "frame_id 必须是非空字符串",
            {"stream_id": stream_id, "frame_index": index},
        )
    return value.strip()


def _number(value: Any) -> int | float:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _number_value(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AquaError("E_PROJECT", f"{field_name} 必须是数字", {"value": value})
    result = float(value)
    if not math.isfinite(result):
        raise AquaError("E_PROJECT", f"{field_name} 必须是有限数字", {"value": value})
    return result


def _positive_number(value: Any, field_name: str) -> float:
    result = _number_value(value, field_name)
    if result <= 0:
        raise AquaError("E_PROJECT", f"{field_name} 必须大于 0", {"value": value})
    return result


def _nonnegative_number(value: Any, field_name: str) -> float:
    result = _number_value(value, field_name)
    if result < 0:
        raise AquaError("E_PROJECT", f"{field_name} 不得小于 0", {"value": value})
    return result


def _integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AquaError("E_PROJECT", f"{field_name} 必须是整数", {"value": value})
    return value


def _positive_int(value: Any, field_name: str) -> int:
    result = _integer(value, field_name)
    if result <= 0:
        raise AquaError("E_PROJECT", f"{field_name} 必须大于 0", {"value": value})
    return result


def _iter_frames(project: Project) -> Iterable[Frame]:
    for stream in project.streams:
        yield from stream.frames
