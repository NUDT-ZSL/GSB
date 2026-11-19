"""块几何归一化与特征构造。

输入像素级外接矩形，按画布尺寸归一化到 [0, 1]，并派生：

* 中心、宽高；
* 列号（按水平中心做确定性的 1D 聚类，跨栏通栏块列号为 -1）；
* 区域（header / title / body / footer）；
* 是否为跨栏通栏块。

所有浮点结果统一保留 9 位小数，保证同一输入逐字节一致。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict

from .errors import E_DATA_INVALID, LayoutOrderError

ROUND_DIGITS = 9
# 相邻块水平中心距离（归一化）不超过该阈值视为同栏候选。
COLUMN_CENTER_GAP = 0.08
# 宽度占画布比例超过该阈值视为跨栏通栏块。
SPANNING_WIDTH_RATIO = 0.62


def _r(value: float) -> float:
    """统一浮点舍入，避免不同平台/路径产生尾差。"""
    return round(float(value), ROUND_DIGITS)


@dataclass(frozen=True)
class Block:
    block_id: str
    x1: int
    y1: int
    x2: int
    y2: int
    block_type: str
    text_len: int
    reading_index: int

    @classmethod
    def from_dict(cls, data: dict, page_id: str) -> "Block":
        required = (
            "block_id",
            "x1",
            "y1",
            "x2",
            "y2",
            "block_type",
            "text_len",
            "reading_index",
        )
        missing = [name for name in required if name not in data]
        if missing:
            raise LayoutOrderError(
                E_DATA_INVALID,
                f"页 {page_id} 的块缺少字段 {missing[0]}",
                {"page_id": page_id, "field": missing[0]},
            )
        block_id = data["block_id"]
        coords = {}
        for name in ("x1", "y1", "x2", "y2", "text_len", "reading_index"):
            value = data[name]
            if isinstance(value, bool) or not isinstance(value, int):
                raise LayoutOrderError(
                    E_DATA_INVALID,
                    f"块 {block_id} 的字段 {name} 必须是整数",
                    {"page_id": page_id, "block_id": block_id, "field": name},
                )
            coords[name] = value
        if not isinstance(block_id, str) or not block_id:
            raise LayoutOrderError(
                E_DATA_INVALID,
                f"页 {page_id} 存在非法 block_id",
                {"page_id": page_id, "field": "block_id"},
            )
        if not isinstance(data["block_type"], str) or not data["block_type"]:
            raise LayoutOrderError(
                E_DATA_INVALID,
                f"块 {block_id} 的 block_type 非法",
                {"page_id": page_id, "block_id": block_id, "field": "block_type"},
            )
        return cls(
            block_id=block_id,
            x1=coords["x1"],
            y1=coords["y1"],
            x2=coords["x2"],
            y2=coords["y2"],
            block_type=data["block_type"],
            text_len=coords["text_len"],
            reading_index=coords["reading_index"],
        )


@dataclass(frozen=True)
class Page:
    page_id: str
    canvas_height: int
    canvas_width: int
    blocks: tuple[Block, ...]
    layout_family: str
    seed: int

    @property
    def block_ids(self) -> tuple[str, ...]:
        return tuple(b.block_id for b in self.blocks)


@dataclass(frozen=True)
class BlockFeature:
    block_id: str
    block_type: str
    reading_index: int
    nx1: float
    ny1: float
    nx2: float
    ny2: float
    cx: float
    cy: float
    width: float
    height: float
    column: int
    region: str
    is_spanning: bool

    def to_dict(self) -> dict:
        return asdict(self)


def load_jsonl(path: str) -> list[Page]:
    """读取确定性样本 JSONL 并做结构校验。"""
    pages: list[Page] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise LayoutOrderError(
                    E_DATA_INVALID,
                    f"第 {line_no} 行不是合法 JSON",
                    {"line_no": line_no},
                ) from exc
            pages.append(parse_page(data))
    if not pages:
        raise LayoutOrderError(
            E_DATA_INVALID, "样本文件为空", {"path": path}
        )
    return pages


def parse_page(data: dict) -> Page:
    page_id = data.get("page_id")
    if not isinstance(page_id, str) or not page_id:
        raise LayoutOrderError(
            E_DATA_INVALID, "页缺少合法 page_id", {"field": "page_id"}
        )
    canvas_shape = data.get("canvas_shape")
    if (
        not isinstance(canvas_shape, list)
        or len(canvas_shape) != 2
        or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in canvas_shape)
    ):
        raise LayoutOrderError(
            E_DATA_INVALID,
            f"页 {page_id} 的 canvas_shape 必须是 [height, width] 正整数",
            {"page_id": page_id, "field": "canvas_shape"},
        )
    canvas_height, canvas_width = canvas_shape
    raw_blocks = data.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise LayoutOrderError(
            E_DATA_INVALID,
            f"页 {page_id} 的 blocks 必须是非空列表",
            {"page_id": page_id, "field": "blocks"},
        )
    blocks = tuple(Block.from_dict(item, page_id) for item in raw_blocks)
    _validate_blocks(page_id, blocks, canvas_height, canvas_width)
    seed = data.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise LayoutOrderError(
            E_DATA_INVALID,
            f"页 {page_id} 的 seed 必须是整数",
            {"page_id": page_id, "field": "seed"},
        )
    layout_family = data.get("layout_family")
    if not isinstance(layout_family, str) or not layout_family:
        raise LayoutOrderError(
            E_DATA_INVALID,
            f"页 {page_id} 的 layout_family 非法",
            {"page_id": page_id, "field": "layout_family"},
        )
    return Page(
        page_id=page_id,
        canvas_height=canvas_height,
        canvas_width=canvas_width,
        blocks=blocks,
        layout_family=layout_family,
        seed=seed,
    )


def _validate_blocks(
    page_id: str, blocks: tuple[Block, ...], canvas_height: int, canvas_width: int
) -> None:
    seen_ids: set[str] = set()
    seen_indices: set[int] = set()
    for block in blocks:
        if block.block_id in seen_ids:
            raise LayoutOrderError(
                E_DATA_INVALID,
                f"页 {page_id} 的 block_id 重复: {block.block_id}",
                {"page_id": page_id, "block_id": block.block_id},
            )
        seen_ids.add(block.block_id)
        if block.reading_index in seen_indices:
            raise LayoutOrderError(
                E_DATA_INVALID,
                f"页 {page_id} 的 reading_index 重复: {block.reading_index}",
                {"page_id": page_id, "block_id": block.block_id},
            )
        seen_indices.add(block.reading_index)
        if not (0 <= block.x1 < block.x2 <= canvas_width) or not (
            0 <= block.y1 < block.y2 <= canvas_height
        ):
            raise LayoutOrderError(
                E_DATA_INVALID,
                f"页 {page_id} 的块 {block.block_id} 外接矩形越界或退化",
                {"page_id": page_id, "block_id": block.block_id},
            )
        if block.text_len < 0:
            raise LayoutOrderError(
                E_DATA_INVALID,
                f"块 {block.block_id} 的 text_len 不能为负",
                {"page_id": page_id, "block_id": block.block_id},
            )
    if seen_indices != set(range(len(blocks))):
        raise LayoutOrderError(
            E_DATA_INVALID,
            f"页 {page_id} 的 reading_index 必须恰好覆盖 0..n-1",
            {"page_id": page_id},
        )


def normalize_page(page: Page) -> list[BlockFeature]:
    """归一化块矩形并派生列号、区域等特征，输出按 block_id 升序排列。"""
    height = float(page.canvas_height)
    width = float(page.canvas_width)
    raw: list[dict] = []
    for block in page.blocks:
        nx1 = _r(block.x1 / width)
        nx2 = _r(block.x2 / width)
        ny1 = _r(block.y1 / height)
        ny2 = _r(block.y2 / height)
        raw.append(
            {
                "block_id": block.block_id,
                "block_type": block.block_type,
                "reading_index": block.reading_index,
                "nx1": nx1,
                "ny1": ny1,
                "nx2": nx2,
                "ny2": ny2,
                "cx": _r((nx1 + nx2) / 2.0),
                "cy": _r((ny1 + ny2) / 2.0),
                "width": _r(nx2 - nx1),
                "height": _r(ny2 - ny1),
            }
        )

    columns = _assign_columns(raw)
    features: list[BlockFeature] = []
    for item in raw:
        block_type = item["block_type"]
        is_spanning = item["width"] >= SPANNING_WIDTH_RATIO
        if block_type == "header":
            region = "header"
        elif block_type == "footer":
            region = "footer"
        elif block_type == "title":
            region = "title"
        else:
            # 通栏图注仍属于正文流，不能提前到标题区，避免破坏阅读顺序。
            region = "body"
        features.append(
            BlockFeature(
                block_id=item["block_id"],
                block_type=block_type,
                reading_index=item["reading_index"],
                nx1=item["nx1"],
                ny1=item["ny1"],
                nx2=item["nx2"],
                ny2=item["ny2"],
                cx=item["cx"],
                cy=item["cy"],
                width=item["width"],
                height=item["height"],
                column=-1 if is_spanning else columns[item["block_id"]],
                region=region,
                is_spanning=is_spanning,
            )
        )
    features.sort(key=lambda feature: feature.block_id)
    return features


def _assign_columns(raw_items: list[dict]) -> dict[str, int]:
    """对非通栏块按水平中心做确定性 1D 聚类，列号从 0 开始自左向右。"""
    body_items = [
        item
        for item in raw_items
        if item["block_type"] not in ("header", "footer")
        and item["width"] < SPANNING_WIDTH_RATIO
    ]
    # block_id 平局键仅用于消除不确定性，列序最终由水平中心位置决定。
    ordered = sorted(body_items, key=lambda item: (item["cx"], item["block_id"]))
    clusters: list[list[dict]] = []
    for item in ordered:
        if not clusters:
            clusters.append([item])
            continue
        last_mean = sum(member["cx"] for member in clusters[-1]) / len(clusters[-1])
        if abs(item["cx"] - last_mean) <= COLUMN_CENTER_GAP:
            clusters[-1].append(item)
        else:
            clusters.append([item])
    # 列号严格按水平中心自左向右分配，平局再用初始簇序与 block_id 打破，
    # 保证等宽双栏下列 0 永远是物理左栏。
    cluster_means = [
        (
            sum(member["cx"] for member in cluster) / len(cluster),
            idx,
            min(member["block_id"] for member in cluster),
        )
        for idx, cluster in enumerate(clusters)
    ]
    cluster_means.sort(key=lambda triple: (triple[0], triple[1], triple[2]))
    mapping: dict[str, int] = {}
    for column, (_, original_idx, _) in enumerate(cluster_means):
        for member in clusters[original_idx]:
            mapping[member["block_id"]] = column
    return mapping
