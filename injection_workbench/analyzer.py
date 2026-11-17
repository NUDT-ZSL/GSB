"""路径级缺陷判定、汇点聚合与规模模式切换。

缺陷优先级（每条路径至多一报，互不叠报）：
1. E_ORDER    所需编码族发生“先编码后解码”的顺序颠倒（抵消记录可定位）；
2. E_MISMATCH 所需编码族都有编码动作，但规范链与汇点要求不一致（族/顺序不匹配）；
3. E_MISSING_ENC 缺少汇点所需编码族。

两种规模模式共用同一份判定函数，因此缺陷集合与汇点判定天然一致；
批处理模式仅额外按“规范链签名 + 所需族”做等价类记忆化剪枝。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .normalizer import normalize, NormalizedChain
from .parser import ParsedManifest, Sink, PathRecord
from .resources import Deadline, DeadlineExceeded

E_MISSING_ENC = "E_MISSING_ENC"
E_ORDER = "E_ORDER"
E_MISMATCH = "E_MISMATCH"

MODE_EXACT = "exact_per_path"
MODE_BATCH = "batch_grouped"


@dataclass
class Defect:
    path_id: str
    error_code: str
    defect_index: int | None
    required_families: list[str]
    family: str | None
    message: str

    def to_dict(self) -> dict:
        return {
            "path_id": self.path_id,
            "error_code": self.error_code,
            "defect_transform_index": self.defect_index,
            "required_families": self.required_families,
            "family": self.family,
            "message": self.message,
        }


@dataclass
class PathVerdict:
    path_id: str
    sink_id: str
    source_id: str
    safe: bool
    chain_signature: str
    chain: list[dict]
    cancels: list[dict]
    defect: Defect | None
    duplicate: bool = False

    def to_dict(self) -> dict:
        data = {
            "path_id": self.path_id,
            "sink_id": self.sink_id,
            "source_id": self.source_id,
            "safe": self.safe,
            "duplicate": self.duplicate,
            "chain_signature": self.chain_signature,
            "canonical_chain": self.chain,
            "cancels": self.cancels,
        }
        data["defect"] = self.defect.to_dict() if self.defect else None
        return data


@dataclass
class SinkVerdict:
    sink: Sink
    verdict: str  # safe | injectable | no_path
    path_ids: list[str] = field(default_factory=list)
    defect_path_ids: list[str] = field(default_factory=list)
    unique_defect_count: int = 0
    duplicate_count: int = 0

    def to_dict(self) -> dict:
        return {
            "sink_id": self.sink.sink_id,
            "line": self.sink.line,
            "context_type": self.sink.context_type,
            "required_families": list(self.sink.required_families),
            "verdict": self.verdict,
            "path_ids": self.path_ids,
            "defect_path_ids": self.defect_path_ids,
            "unique_defect_count": self.unique_defect_count,
            "duplicate_count": self.duplicate_count,
        }


def classify_path(path: PathRecord, sink: Sink) -> tuple[NormalizedChain, Defect | None]:
    """对单条路径做规范化与缺陷分类，返回规范链与缺陷（安全时为 None）。"""
    chain = normalize(path.transforms)
    required = list(sink.required_families)

    # 1) 顺序颠倒：所需族出现先编码后解码的抵消
    for cancel in chain.cancels:
        if cancel.family in required and cancel.is_encode_then_decode():
            defect = Defect(
                path_id=path.path_id,
                error_code=E_ORDER,
                defect_index=cancel.revert_index(),
                required_families=required,
                family=cancel.family,
                message=(
                    f"编码族 {cancel.family} 先编码(序号 {cancel.encode_indices})"
                    f"后解码(序号 {cancel.decode_indices})，防护被还原"
                ),
            )
            return chain, defect

    surviving_encodes = chain.encode_families

    # 2) 缺少所需编码族：规范链中不存在任何存活编码步骤
    if not surviving_encodes:
        defect_index = chain.steps[-1].indices[-1] if chain.steps else None
        defect = Defect(
            path_id=path.path_id,
            error_code=E_MISSING_ENC,
            defect_index=defect_index,
            required_families=required,
            family=required[0],
            message=f"缺少汇点所需编码族: {required}",
        )
        return chain, defect

    missing = [family for family in required if family not in surviving_encodes]
    extra = [family for family in surviving_encodes if family not in required]

    # 3a) 施加了汇点上下文不需要的编码族 -> 族不匹配
    if extra:
        mismatch_family = extra[0]
        mismatch_step = next(
            step for step in chain.steps
            if step.direction == "encode" and step.family == mismatch_family
        )
        defect = Defect(
            path_id=path.path_id,
            error_code=E_MISMATCH,
            defect_index=mismatch_step.indices[0],
            required_families=required,
            family=mismatch_family,
            message=f"编码族 {mismatch_family} 与汇点上下文要求 {required} 不匹配",
        )
        return chain, defect

    # 3b) 所需族部分缺失（无多余族）-> 遗漏编码
    if missing:
        last_encode = next(
            step for step in reversed(chain.steps) if step.direction == "encode"
        )
        defect = Defect(
            path_id=path.path_id,
            error_code=E_MISSING_ENC,
            defect_index=last_encode.indices[-1],
            required_families=required,
            family=missing[0],
            message=f"缺少汇点所需编码族: {missing}",
        )
        return chain, defect

    # 3c) 编码族集合一致但顺序与要求不符 -> 族不匹配
    if surviving_encodes != required:
        mismatch_step = next(
            step for position, step in enumerate(chain.steps)
            if step.direction == "encode"
            and step.family != required[position]
        )
        defect = Defect(
            path_id=path.path_id,
            error_code=E_MISMATCH,
            defect_index=mismatch_step.indices[0],
            required_families=required,
            family=mismatch_step.family,
            message=f"编码族链 {surviving_encodes} 顺序与汇点要求 {required} 不匹配",
        )
        return chain, defect

    return chain, None


def _build_verdict(path: PathRecord, chain: NormalizedChain, defect: Defect | None,
                   duplicate: bool) -> PathVerdict:
    return PathVerdict(
        path_id=path.path_id,
        sink_id=path.sink_id,
        source_id=path.source_id,
        safe=defect is None,
        chain_signature=chain.signature,
        chain=chain.describe(),
        cancels=[
            {
                "family": cancel.family,
                "encode_indices": cancel.encode_indices,
                "decode_indices": cancel.decode_indices,
            }
            for cancel in chain.cancels
        ],
        defect=defect,
        duplicate=duplicate,
    )


def _rebase_defect(template: Defect, path_id: str) -> Defect:
    """等价类命中时，把模板缺陷的路径标识替换为当前路径。"""
    return Defect(
        path_id=path_id,
        error_code=template.error_code,
        defect_index=template.defect_index,
        required_families=list(template.required_families),
        family=template.family,
        message=template.message,
    )


def choose_mode(path_count: int, sink_count: int, config: Config) -> str:
    if path_count > config.thresholds.path_count or sink_count > config.thresholds.sink_count:
        return MODE_BATCH
    return MODE_EXACT


def analyze_manifest(
    parsed: ParsedManifest,
    config: Config,
    force_mode: str | None = None,
    deadline: Deadline | None = None,
) -> dict:
    """分析一份已解析清单。force_mode 仅用于测试/演示，生产由规模观测自动切换。"""
    sinks_by_id = {sink.sink_id: sink for sink in parsed.sinks}
    observed_paths = len(parsed.paths)
    observed_sinks = len(parsed.sinks)
    mode = force_mode or choose_mode(observed_paths, observed_sinks, config)

    # 按路径标识去重：首次出现为真值
    unique_paths: dict[str, PathRecord] = {}
    duplicate_count = 0
    for path in parsed.paths:
        if path.path_id in unique_paths:
            duplicate_count += 1
        else:
            unique_paths[path.path_id] = path

    # 批处理模式的编码族等价类记忆化。
    # 键必须包含抵消记录：仅用抵消后签名会让“空链(被还原)”与“空链(本就无变换)”碰撞，
    # 从而把 E_ORDER 错误复用到 E_MISSING_ENC 路径。
    memo: dict[tuple[str, tuple[str, ...]], Defect | None] = {}
    memo_hits = 0
    path_verdicts: list[PathVerdict] = []

    seen_path_ids: set[str] = set()
    for path in parsed.paths:
        if deadline is not None:
            deadline.check()
        is_duplicate = path.path_id in seen_path_ids
        seen_path_ids.add(path.path_id)
        if is_duplicate:
            chain = normalize(path.transforms)
            # 重复路径仍输出明细，但判定以首次真值为准（聚合阶段只计首次）
            first = unique_paths[path.path_id]
            _, template = classify_path(first, sinks_by_id[first.sink_id])
            defect = _rebase_defect(template, path.path_id) if template else None
            path_verdicts.append(_build_verdict(path, chain, defect, duplicate=True))
            continue

        sink = sinks_by_id[path.sink_id]
        chain = normalize(path.transforms)
        if mode == MODE_BATCH:
            cancel_key = tuple(
                (
                    cancel.family,
                    tuple(cancel.encode_indices),
                    tuple(cancel.decode_indices),
                )
                for cancel in chain.cancels
            )
            key = (
                chain.signature,
                cancel_key,
                sink.required_families,
            )
            if key in memo:
                memo_hits += 1
                template = memo[key]
                defect = _rebase_defect(template, path.path_id) if template else None
            else:
                _, defect = classify_path(path, sink)
                memo[key] = defect
        else:
            _, defect = classify_path(path, sink)

        path_verdicts.append(_build_verdict(path, chain, defect, duplicate=False))

    # 汇点聚合：只统计去重后的唯一路径
    sink_verdicts: list[SinkVerdict] = []
    grouped: dict[str, list[PathVerdict]] = {sink_id: [] for sink_id in sinks_by_id}
    for verdict in path_verdicts:
        if verdict.duplicate:
            continue
        grouped[verdict.sink_id].append(verdict)

    for sink in parsed.sinks:
        verdicts = grouped.get(sink.sink_id, [])
        defect_ids = [v.path_id for v in verdicts if v.defect is not None]
        if not verdicts:
            status = "no_path"
        elif defect_ids:
            status = "injectable"
        else:
            status = "safe"
        sink_verdicts.append(
            SinkVerdict(
                sink=sink,
                verdict=status,
                path_ids=[v.path_id for v in verdicts],
                defect_path_ids=defect_ids,
                unique_defect_count=len(defect_ids),
                duplicate_count=sum(
                    1 for v in path_verdicts if v.duplicate and v.sink_id == sink.sink_id
                ),
            )
        )

    unique_lookups = len(unique_paths)
    prune_ratio = round(memo_hits / unique_lookups, 6) if mode == MODE_BATCH and unique_lookups else 0.0

    return {
        "file_id": parsed.file_id,
        "dialect": parsed.dialect,
        "mode": mode,
        "scale_observations": {
            "path_records": observed_paths,
            "sink_records": observed_sinks,
            "unique_paths": unique_lookups,
            "duplicate_paths": duplicate_count,
        },
        "equivalence_pruning": {
            "enabled": mode == MODE_BATCH,
            "memo_hits": memo_hits,
            "prune_ratio": prune_ratio,
        },
        "rejections": [rejection.to_dict() for rejection in parsed.rejections],
        "sink_verdicts": [verdict.to_dict() for verdict in sink_verdicts],
        "path_verdicts": [verdict.to_dict() for verdict in path_verdicts],
    }
