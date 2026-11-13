"""编辑引擎：顺序执行裁剪 / 拼接 / 音量调整指令，维护单条时间线。

设计要点
--------
1. **顺序执行**：指令按 JSON 数组顺序依次应用，后一条的输入是前一条的输出；
   失败指令被跳过，时间线保持失败前的状态不变。
2. **部分成功可用**：每条指令独立 try/except，任何业务错误都结构化为
   :class:`EditError` 记录到结果中，绝不中断整体流程。
3. **断点续跑（重试）**：每条指令处理完立即把时间线与已成功指令索引落盘到
   检查点。重新运行时，已成功指令直接跳过（不会重复应用、不会污染输出），
   仅重新尝试此前失败的指令。可用 ``--fresh`` 忽略检查点全新运行。
4. **时间单位**：指令使用秒（浮点），内部统一换算成整数帧（四舍五入）。
"""

from __future__ import annotations

import array
import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Optional

from .errors import EditError, ErrorCode
from .wavio import SAMPLE_MAX, SAMPLE_MIN, WavData, load_wav, write_wav


@dataclass
class InstructionResult:
    """单条指令的处理结果，同时也是报告中的一条记录。"""

    index: int
    type: str
    status: str                      # "success" | "failed"
    source: Optional[str] = None
    params: dict = field(default_factory=dict)
    applied: bool = False            # 本次/历史运行中是否真正应用到时间线
    resumed: bool = False            # 是否为续跑中跳过的已成功指令
    error: Optional[dict] = None     # 失败时的结构化错误 {code,message,retryable}
    detail: Optional[str] = None     # 成功时的人类可读说明
    output_timeline_duration: float = 0.0  # 该指令处理后的时间线时长（秒）


class EditorEngine:
    """按指令列表编辑音频时间线。

    :param instructions: 原始指令列表（dict 组成的数组）
    :param base_dir: 源文件相对路径的解析基准目录（通常是指令 JSON 所在目录）
    :param work_dir: 检查点存放目录
    :param output_wav: 输出 WAV 路径
    :param fresh: True 时忽略已有检查点，清空时间线全新运行
    """

    def __init__(
        self,
        instructions: list,
        base_dir: str,
        work_dir: str,
        output_wav: str,
        fresh: bool = False,
    ):
        self.instructions = instructions
        self.base_dir = os.path.abspath(base_dir)
        self.work_dir = os.path.abspath(work_dir)
        self.output_wav = os.path.abspath(output_wav)
        self.fresh = fresh

        self.timeline = array.array("h")
        self.sample_rate: Optional[int] = None
        self.results: list[InstructionResult] = []

        # 来源登记：记录每个来源的最终可用性，供报告"不可用来源"清单使用
        self.source_status: dict[str, str] = {}   # source -> "available"/"unavailable"
        self.source_error: dict[str, dict] = {}

        # 本次运行中已加载来源的缓存，避免重复读盘
        self._source_cache: dict[str, WavData] = {}

        os.makedirs(self.work_dir, exist_ok=True)
        self.checkpoint_path = os.path.join(self.work_dir, "checkpoint.json")

        self._instruction_sig = hashlib.sha256(
            json.dumps(instructions, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self._applied_indices: set[int] = set()

    # ------------------------------------------------------------------ #
    # 检查点
    # ------------------------------------------------------------------ #
    def _load_checkpoint(self) -> bool:
        """尝试恢复检查点。返回 True 表示恢复成功。

        检查点与当前指令集签名不一致（指令被修改过）时视为无效，需全新运行。
        """
        if self.fresh or not os.path.exists(self.checkpoint_path):
            return False
        try:
            with open(self.checkpoint_path, "r", encoding="utf-8") as fh:
                ckpt = json.load(fh)
            if ckpt.get("instruction_sig") != self._instruction_sig:
                return False
            self.sample_rate = ckpt["sample_rate"]
            packed = base64.b64decode(ckpt["timeline_b64"])
            self.timeline = array.array("h")
            self.timeline.frombytes(packed)
            self._applied_indices = set(ckpt.get("applied_indices", []))
            return True
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            # 检查点损坏：安全降级为全新运行
            return False

    def _save_checkpoint(self) -> None:
        """把时间线与已成功指令索引原子落盘。"""
        payload = {
            "instruction_sig": self._instruction_sig,
            "sample_rate": self.sample_rate,
            "timeline_b64": base64.b64encode(self.timeline.tobytes()).decode("ascii"),
            "applied_indices": sorted(self._applied_indices),
        }
        tmp = self.checkpoint_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, self.checkpoint_path)

    # ------------------------------------------------------------------ #
    # 来源加载
    # ------------------------------------------------------------------ #
    def _resolve_source(self, source: str) -> str:
        if os.path.isabs(source):
            return os.path.normpath(source)
        return os.path.normpath(os.path.join(self.base_dir, source))

    def _load_source(self, source: str) -> WavData:
        """加载并缓存来源 WAV，同时维护来源可用性登记。"""
        if source in self._source_cache:
            return self._source_cache[source]

        path = self._resolve_source(source)
        data = load_wav(path)  # 失败时抛 EditError

        # 采样率一致性：以时间线首个成功来源的采样率为准
        if self.sample_rate is None:
            self.sample_rate = data.sample_rate
        elif data.sample_rate != self.sample_rate:
            raise EditError(
                ErrorCode.SAMPLE_RATE_MISMATCH,
                f"来源 {source} 采样率 {data.sample_rate}Hz 与时间线 "
                f"{self.sample_rate}Hz 不一致",
                retryable=False,
            )

        self._source_cache[source] = data
        self.source_status[source] = "available"
        self.source_error.pop(source, None)
        return data

    def _record_source_failure(self, source: str, err: EditError) -> None:
        # 已确认可用的来源不被后续瞬时错误覆盖
        if self.source_status.get(source) != "available":
            self.source_status[source] = "unavailable"
            self.source_error[source] = err.to_dict()

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def run(self) -> list[InstructionResult]:
        resumed = self._load_checkpoint()
        self.resumed = resumed

        for index, raw in enumerate(self.instructions):
            result = InstructionResult(
                index=index,
                type=str(raw.get("type", "unknown")) if isinstance(raw, dict) else "unknown",
                status="success",
                source=raw.get("source") if isinstance(raw, dict) else None,
                params=raw.get("params", {}) if isinstance(raw, dict) else {},
            )

            # 续跑：历史已成功的指令原样标记，绝不重复应用
            if resumed and index in self._applied_indices:
                result.applied = True
                result.resumed = True
                result.detail = "续跑跳过：该指令已在之前的运行中成功应用"
                result.output_timeline_duration = self.timeline_duration
                self.results.append(result)
                continue

            try:
                if not isinstance(raw, dict):
                    raise EditError(
                        ErrorCode.INVALID_INSTRUCTION,
                        f"指令必须是 JSON 对象，实际类型: {type(raw).__name__}",
                    )
                instr_type = raw.get("type")
                source = raw.get("source")
                params = raw.get("params", {})
                if not isinstance(params, dict):
                    raise EditError(
                        ErrorCode.INVALID_INSTRUCTION,
                        "指令 params 必须是对象",
                    )

                if instr_type == "crop":
                    self._op_crop(result, source, params)
                elif instr_type == "splice":
                    self._op_splice(result, source, params)
                elif instr_type == "volume":
                    self._op_volume(result, source, params)
                else:
                    raise EditError(
                        ErrorCode.UNKNOWN_INSTRUCTION_TYPE,
                        f"未知指令类型: {instr_type!r}，支持 crop/splice/volume",
                    )
            except EditError as exc:
                self._fail(result, exc)
            except Exception as exc:  # 兜底：任何意外都结构化记录，不中断批处理
                self._fail(
                    result,
                    EditError(
                        ErrorCode.INTERNAL_ERROR,
                        f"内部错误: {type(exc).__name__}: {exc}",
                    ),
                )

            result.output_timeline_duration = self.timeline_duration
            self.results.append(result)

        # 写出 WAV（即使部分失败，已成功部分仍可用）
        if self.sample_rate is not None and len(self.timeline) > 0:
            write_wav(self.output_wav, self.timeline, self.sample_rate)

        return self.results

    def _fail(self, result: InstructionResult, exc: EditError) -> None:
        result.status = "failed"
        result.applied = False
        result.error = exc.to_dict()
        # 只有来源级错误才登记到"不可用来源"清单；
        # 参数/指令类错误（如未知指令类型）不把来源误判为不可用
        source_level_codes = {
            ErrorCode.SOURCE_NOT_FOUND,
            ErrorCode.SOURCE_NOT_READABLE,
            ErrorCode.INVALID_WAV_FORMAT,
            ErrorCode.SAMPLE_RATE_MISMATCH,
        }
        if result.source and exc.code in source_level_codes:
            self._record_source_failure(result.source, exc)

    # ------------------------------------------------------------------ #
    # 指令实现
    # ------------------------------------------------------------------ #
    def _require_source(self, source) -> str:
        if not isinstance(source, str) or not source:
            raise EditError(
                ErrorCode.INVALID_INSTRUCTION,
                "指令缺少有效的 source 字段（非空字符串）",
            )
        return source

    @staticmethod
    def _require_number(params: dict, key: str) -> float:
        if key not in params:
            raise EditError(
                ErrorCode.INVALID_INSTRUCTION,
                f"参数缺少必填字段: {key}",
            )
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EditError(
                ErrorCode.INVALID_INSTRUCTION,
                f"参数 {key} 必须是数字，实际: {value!r}",
            )
        return float(value)

    def _seconds_to_frames(self, seconds: float) -> int:
        if self.sample_rate is None:
            raise EditError(
                ErrorCode.INVALID_INSTRUCTION,
                "时间线尚未建立（尚无成功加载的来源），无法进行帧换算",
            )
        return int(round(seconds * self.sample_rate))

    def _op_crop(self, result: InstructionResult, source, params: dict) -> None:
        """裁剪：用来源文件的 [start, end) 片段 *替换* 当前时间线。"""
        source = self._require_source(source)
        start = self._require_number(params, "start")
        end = self._require_number(params, "end")
        if start < 0 or end < 0:
            raise EditError(
                ErrorCode.TIME_OUT_OF_RANGE,
                f"裁剪时间不能为负: start={start}, end={end}",
            )
        if start >= end:
            raise EditError(
                ErrorCode.TIME_OUT_OF_RANGE,
                f"裁剪起始时间必须小于结束时间: start={start}, end={end}",
            )

        data = self._load_source(source)
        duration = data.duration_seconds
        # 越界判定基于秒，与报告口径一致；允许浮点误差 1 个采样间隔
        tol = 1.0 / data.sample_rate
        if end > duration + tol:
            raise EditError(
                ErrorCode.TIME_OUT_OF_RANGE,
                f"裁剪结束时间 {end:.3f}s 超出来源 {source} 时长 {duration:.3f}s",
            )

        start_frame = self._seconds_to_frames(start)
        end_frame = self._seconds_to_frames(end)
        if end_frame > data.frame_count:
            end_frame = data.frame_count
        if start_frame >= end_frame:
            raise EditError(
                ErrorCode.TIME_OUT_OF_RANGE,
                f"裁剪区间帧数为空: start_frame={start_frame}, end_frame={end_frame}",
            )

        self.timeline = data.samples[start_frame:end_frame]
        self._applied_indices.add(result.index)
        result.applied = True
        result.detail = (
            f"裁剪 {source} [{start:.3f}s, {end:.3f}s)，"
            f"得到 {end_frame - start_frame} 帧"
        )
        self._save_checkpoint()

    def _op_splice(self, result: InstructionResult, source, params: dict) -> None:
        """拼接：把来源片段插入时间线的 at 位置，后续样本自动后移。"""
        source = self._require_source(source)
        at = self._require_number(params, "at")
        if at < 0:
            raise EditError(
                ErrorCode.INSERT_OUT_OF_RANGE,
                f"插入时间点不能为负: at={at}",
            )

        data = self._load_source(source)

        # 片段范围：缺省取整个来源；给定时做与裁剪相同的越界校验
        if "start" in params or "end" in params:
            start = self._require_number(params, "start")
            end = self._require_number(params, "end")
            if start < 0 or end < 0 or start >= end:
                raise EditError(
                    ErrorCode.TIME_OUT_OF_RANGE,
                    f"片段区间非法: start={start}, end={end}",
                )
            tol = 1.0 / data.sample_rate
            if end > data.duration_seconds + tol:
                raise EditError(
                    ErrorCode.TIME_OUT_OF_RANGE,
                    f"片段结束时间 {end:.3f}s 超出来源 {source} 时长 "
                    f"{data.duration_seconds:.3f}s",
                )
            sf = self._seconds_to_frames(start)
            ef = min(self._seconds_to_frames(end), data.frame_count)
            if sf >= ef:
                raise EditError(
                    ErrorCode.TIME_OUT_OF_RANGE,
                    "拼接片段区间帧数为空",
                )
            segment = data.samples[sf:ef]
            segment_desc = f"片段[{start:.3f}s,{end:.3f}s)"
        else:
            segment = data.samples[:]
            segment_desc = "完整来源"

        # 插入点必须在时间线有效范围 [0, 当前时长] 内（等于时长即尾部追加）
        insert_frame = self._seconds_to_frames(at)
        if insert_frame < 0 or insert_frame > len(self.timeline):
            raise EditError(
                ErrorCode.INSERT_OUT_OF_RANGE,
                f"插入时间点 {at:.3f}s 超出时间线有效范围 "
                f"[0, {self.timeline_duration:.3f}s]",
            )

        self.timeline = self.timeline[:insert_frame] + segment + self.timeline[insert_frame:]
        self._applied_indices.add(result.index)
        result.applied = True
        result.detail = (
            f"在 {at:.3f}s 处插入 {source} {segment_desc}，"
            f"插入 {len(segment)} 帧，后续内容后移"
        )
        self._save_checkpoint()

    def _op_volume(self, result: InstructionResult, source, params: dict) -> None:
        """音量调整：对时间线（或指定区间）应用分贝增益，缩放后钳位防削波。"""
        gain_db = self._require_number(params, "gain_db")
        if gain_db < -60.0 or gain_db > 60.0:
            raise EditError(
                ErrorCode.GAIN_OUT_OF_RANGE,
                f"分贝增益 {gain_db} 超出合法范围 [-60, +60]",
            )

        # 区间：缺省作用于整条时间线
        if "start" in params or "end" in params:
            start = self._require_number(params, "start")
            end = self._require_number(params, "end")
            if start < 0 or end < 0 or start >= end:
                raise EditError(
                    ErrorCode.TIME_OUT_OF_RANGE,
                    f"音量调整区间非法: start={start}, end={end}",
                )
            sf = self._seconds_to_frames(start)
            ef = self._seconds_to_frames(end)
            if ef > len(self.timeline) or sf >= len(self.timeline):
                raise EditError(
                    ErrorCode.TIME_OUT_OF_RANGE,
                    f"音量调整区间 [{start:.3f}s, {end:.3f}s) 超出时间线时长 "
                    f"{self.timeline_duration:.3f}s",
                )
            ef = min(ef, len(self.timeline))
            scope_desc = f"区间[{start:.3f}s,{end:.3f}s)"
        else:
            sf, ef = 0, len(self.timeline)
            scope_desc = "整条时间线"

        if len(self.timeline) == 0:
            raise EditError(
                ErrorCode.INVALID_INSTRUCTION,
                "时间线为空，没有可调整音量的样本（请先用 crop/splice 建立时间线）",
            )

        factor = 10.0 ** (gain_db / 20.0)
        clipped = 0
        # 原地修改区间内的样本；钳位到 16-bit 范围，避免削波溢出
        for i in range(sf, ef):
            scaled = int(round(self.timeline[i] * factor))
            if scaled > SAMPLE_MAX:
                scaled = SAMPLE_MAX
                clipped += 1
            elif scaled < SAMPLE_MIN:
                scaled = SAMPLE_MIN
                clipped += 1
            self.timeline[i] = scaled

        self._applied_indices.add(result.index)
        result.applied = True
        result.detail = (
            f"对{scope_desc}应用 {gain_db:+.1f}dB 增益（系数 {factor:.4f}），"
            f"钳位样本 {clipped} 个"
        )
        self._save_checkpoint()

    # ------------------------------------------------------------------ #
    # 状态导出
    # ------------------------------------------------------------------ #
    @property
    def timeline_duration(self) -> float:
        if self.sample_rate is None:
            return 0.0
        return len(self.timeline) / self.sample_rate

    @property
    def frame_count(self) -> int:
        return len(self.timeline)

    def unavailable_sources(self) -> list[dict]:
        """报告用：始终不可用的来源清单。"""
        return [
            {
                "source": source,
                "error": self.source_error.get(source, {}),
            }
            for source, status in sorted(self.source_status.items())
            if status == "unavailable"
        ]
