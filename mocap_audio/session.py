"""会话 JSON 数据模型与确定性校验。

校验失败抛出带业务码的异常（E_FORMAT / E_RATE / E_MISSING），
所有异常 refs 中携带设备标识与段标识，便于定位非法条目。
"""

import json
import math
import os

from .errors import FormatError, MissingError, RateError
from .wavio import ALLOWED_BITS, ALLOWED_RATES

ALLOWED_MODEL_ORDERS = (0, 1)


class PulseSpec:
    __slots__ = ("theoretical_ms", "search_ms")

    def __init__(self, theoretical_ms, search_ms):
        self.theoretical_ms = theoretical_ms
        self.search_ms = search_ms

    def as_dict(self):
        return {"theoretical_ms": self.theoretical_ms, "search_ms": self.search_ms}


class SegmentSpec:
    __slots__ = (
        "device_id", "segment_id", "file", "start_ms", "duration_ms",
        "channels", "gain", "pulses",
    )

    def __init__(self, device_id, segment_id, file, start_ms, duration_ms,
                 channels, gain, pulses):
        self.device_id = device_id
        self.segment_id = segment_id
        self.file = file
        self.start_ms = start_ms
        self.duration_ms = duration_ms
        self.channels = list(channels)
        self.gain = list(gain)
        self.pulses = list(pulses)

    @property
    def ref(self):
        return {"device_id": self.device_id, "segment_id": self.segment_id}

    def as_dict(self):
        return {
            "device_id": self.device_id,
            "segment_id": self.segment_id,
            "file": self.file,
            "start_ms": self.start_ms,
            "duration_ms": self.duration_ms,
            "channels": self.channels,
            "gain": self.gain,
            "pulses": [p.as_dict() for p in self.pulses],
        }


class DeviceSpec:
    def __init__(self, device_id, sample_rate, bits, is_reference, segments):
        self.device_id = device_id
        self.sample_rate = sample_rate
        self.bits = bits
        self.is_reference = is_reference
        self.segments = list(segments)

    def as_dict(self):
        return {
            "device_id": self.device_id,
            "sample_rate": self.sample_rate,
            "bits": self.bits,
            "is_reference": self.is_reference,
            "segments": [s.as_dict() for s in self.segments],
        }


class Session:
    def __init__(self, raw, base_dir):
        self.raw = raw
        self.base_dir = base_dir
        self.session_id = raw["session_id"]
        self.snapshot_id = raw["snapshot_id"]
        self.devices = []
        self.reference_device_id = None
        for dev in raw["devices"]:
            segments = []
            for seg in dev["segments"]:
                pulses = [PulseSpec(p["theoretical_ms"], p["search_ms"])
                          for p in seg.get("pulses", [])]
                segments.append(SegmentSpec(
                    dev["device_id"], seg["segment_id"], seg["file"],
                    seg["start_ms"], seg["duration_ms"],
                    seg["channels"], seg["gain"], pulses))
            self.devices.append(DeviceSpec(
                dev["device_id"], dev["sample_rate"], dev["bits"],
                dev.get("is_reference", False), segments))
            if dev.get("is_reference"):
                self.reference_device_id = dev["device_id"]
        self.model_order = raw["model_order"]
        self.stat_window_ms = raw["stat_window_ms"]
        self.absolute_gate_db = float(raw["absolute_gate_db"])
        self.relative_gate_db = float(raw["relative_gate_db"])
        self.dr_lower_db = float(raw["dr_lower_db"])
        self.dr_upper_db = float(raw["dr_upper_db"])
        self.target_sample_rate = raw["target_sample_rate"]
        self.target_bits = raw["target_bits"]
        self.timeslice_ms = raw["timeslice_ms"]
        self.concurrency = raw["concurrency"]
        self.memory_budget_bytes = raw["memory_budget_bytes"]
        self.state_dir = raw["state_dir"]
        self.output_wav = raw["output_wav"]
        self.output_report = raw["output_report"]
        self.tail_rule = raw.get("tail_rule", "include")
        self.gap_fill = raw.get("gap_fill", "zero")
        self.output_channels = list(raw["output_channels"])
        self.peak_threshold = float(raw.get("peak_threshold", 0.5))

    def iter_segments(self):
        """按设备段声明顺序遍历（确定性归并的权威顺序）。"""
        for dev in self.devices:
            for seg in dev.segments:
                yield dev, seg

    def resolve(self, path):
        return path if os.path.isabs(path) else os.path.join(self.base_dir, path)


def _require(cond, message, refs, code=FormatError, detail=None):
    if not cond:
        raise code(message, refs=refs, detail=detail or {})


def _is_nonneg_int_ms(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_pos_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def load_session(session_path):
    if not os.path.isfile(session_path):
        raise MissingError("会话 JSON 不存在", refs=[session_path])
    try:
        with open(session_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise FormatError("会话 JSON 无法解析", refs=[session_path],
                          detail={"reason": str(exc)})
    _validate_schema(raw, os.path.dirname(os.path.abspath(session_path)))
    return Session(raw, os.path.dirname(os.path.abspath(session_path)))


def _validate_schema(raw, base_dir):
    if not isinstance(raw, dict):
        raise FormatError("会话根节点必须是对象")
    for key in ("session_id", "snapshot_id", "devices", "model_order",
                "stat_window_ms", "absolute_gate_db", "relative_gate_db",
                "dr_lower_db", "dr_upper_db", "target_sample_rate",
                "target_bits", "timeslice_ms", "concurrency",
                "memory_budget_bytes", "state_dir", "output_wav",
                "output_report", "output_channels"):
        if key not in raw:
            raise MissingError("会话缺少必需字段", refs=[key])

    _require(_is_pos_int(raw["model_order"]) is False or
             raw["model_order"] in ALLOWED_MODEL_ORDERS,
             "漂移模型阶数仅支持 0（仅偏移）或 1（线性漂移）",
             refs=["model_order"], code=FormatError)
    _require(_is_pos_int(raw["stat_window_ms"]),
             "统计窗口长度必须为正整数毫秒", refs=["stat_window_ms"])
    _require(_is_pos_int(raw["timeslice_ms"]),
             "时间片长度必须为正整数毫秒", refs=["timeslice_ms"])
    _require(_is_pos_int(raw["concurrency"]),
             "并发上限必须为正整数", refs=["concurrency"])
    _require(_is_pos_int(raw["memory_budget_bytes"]),
             "内存预算必须为正整数字节", refs=["memory_budget_bytes"])
    for key in ("absolute_gate_db", "relative_gate_db", "dr_lower_db", "dr_upper_db"):
        _require(isinstance(raw[key], (int, float)) and math.isfinite(raw[key]),
                 f"{key} 必须为有限数", refs=[key])
    _require(raw["dr_lower_db"] <= raw["dr_upper_db"],
             "动态范围下界不得大于上界", refs=["dr_lower_db", "dr_upper_db"])
    if raw["target_sample_rate"] not in ALLOWED_RATES:
        raise RateError("目标采样率不在允许集合 44100/48000/96000",
                        refs=["target_sample_rate"],
                        detail={"value": raw["target_sample_rate"]})
    if raw["target_bits"] not in ALLOWED_BITS:
        raise RateError("目标位深不在允许集合 16/24",
                        refs=["target_bits"], detail={"value": raw["target_bits"]})
    _require(raw.get("tail_rule", "include") in ("include", "drop"),
             "tail_rule 仅支持 include/drop", refs=["tail_rule"])
    _require(raw.get("gap_fill", "zero") in ("zero",),
             "gap_fill 仅支持 zero", refs=["gap_fill"])
    _require(isinstance(raw.get("peak_threshold", 0.5), (int, float)) and
             0.0 < float(raw.get("peak_threshold", 0.5)) <= 1.0,
             "peak_threshold 必须为 (0, 1] 区间内的有限数",
             refs=["peak_threshold"])
    _require(isinstance(raw["output_channels"], list) and raw["output_channels"],
             "output_channels 必须为非空列表", refs=["output_channels"])

    devices = raw["devices"]
    _require(isinstance(devices, list) and devices,
             "devices 必须为非空列表", refs=["devices"])
    ref_count = 0
    declared_channels = set()
    seen_segments = set()
    for dev in devices:
        dev_id = dev.get("device_id", "<unknown-device>")
        refs = [dev_id]
        _require(isinstance(dev, dict) and "device_id" in dev,
                 "设备缺少 device_id", refs=refs)
        if dev["sample_rate"] not in ALLOWED_RATES:
            raise RateError("设备采样率不在允许集合 44100/48000/96000",
                            refs=refs, detail={"value": dev["sample_rate"]})
        if dev["bits"] not in ALLOWED_BITS:
            raise RateError("设备位深不在允许集合 16/24", refs=refs,
                            detail={"value": dev["bits"]})
        if dev.get("is_reference"):
            ref_count += 1
        segments = dev.get("segments")
        _require(isinstance(segments, list) and segments,
                 "设备段列表必须为非空", refs=refs)
        for seg in segments:
            seg_id = seg.get("segment_id", "<unknown-segment>")
            srefs = [dev_id, seg_id]
            key = (dev_id, seg_id)
            _require(key not in seen_segments, "设备段标识重复", refs=srefs)
            seen_segments.add(key)
            for field in ("file", "start_ms", "duration_ms", "channels", "gain"):
                if field not in seg:
                    raise MissingError("段缺少必需字段", refs=srefs, detail={"field": field})
            _require(_is_nonneg_int_ms(seg["start_ms"]),
                     "段起始理论时刻必须为非负整数毫秒", refs=srefs)
            _require(_is_pos_int(seg["duration_ms"]),
                     "段时长必须为正整数毫秒", refs=srefs)
            file_path = seg["file"]
            _require(isinstance(file_path, str) and file_path,
                     "段文件路径必须为非空字符串", refs=srefs)
            if not os.path.isfile(file_path if os.path.isabs(file_path)
                                  else os.path.join(base_dir, file_path)):
                raise MissingError("段 WAV 文件缺失", refs=srefs,
                                  detail={"file": file_path})
            channels = seg["channels"]
            gain = seg["gain"]
            _require(isinstance(channels, list) and channels and
                     all(isinstance(c, str) and c for c in channels),
                     "声道布局必须为非空字符串列表", refs=srefs)
            _require(isinstance(gain, list) and len(gain) == len(channels),
                     "增益数量必须与声道数量一致", refs=srefs)
            for g in gain:
                _require(isinstance(g, (int, float)) and not isinstance(g, bool)
                         and math.isfinite(g),
                         "增益必须为有限数", refs=srefs)
            for c in channels:
                declared_channels.add(c)
            prev = None
            for pulse in seg.get("pulses", []):
                prefs = srefs + [f"pulse@{pulse.get('theoretical_ms')}"]
                t_ms = pulse.get("theoretical_ms")
                w_ms = pulse.get("search_ms")
                _require(_is_nonneg_int_ms(t_ms),
                         "脉冲理论时刻必须为非负整数毫秒", refs=prefs)
                _require(_is_pos_int(w_ms),
                         "脉冲搜索窗必须为正整数毫秒", refs=prefs)
                _require(0 <= t_ms < seg["duration_ms"],
                         "脉冲理论时刻必须落在段内（相对段起点的毫秒数）",
                         refs=prefs)
                if prev is not None:
                    _require(t_ms > prev, "脉冲理论时刻必须严格递增", refs=prefs)
                prev = t_ms
    _require(ref_count == 1, "必须恰好声明一台参考设备",
             refs=["devices"], detail={"reference_count": ref_count})
    _require(set(raw["output_channels"]).issubset(declared_channels),
             "输出声道必须由至少一个设备段声明", refs=["output_channels"])

