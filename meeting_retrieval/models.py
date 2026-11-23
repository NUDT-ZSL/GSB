"""Configuration and sample models with validation."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import (
    BusinessError,
    E_CONFIG_INVALID,
    E_GRID_INVALID,
    E_IMAGE_SEED_INVALID,
    E_IMAGE_SHAPE_MISMATCH,
    E_LOCALE_INVALID,
    E_RECT_INVALID,
    E_TEXT_EMPTY,
    E_VOCAB_OUT_OF_RANGE,
)

SCHEMA_VERSION = 1
DECODER_VERSION = "meeting-decoder-v1"
VISUAL_VERSION = "tile-visual-v1"
TEXT_VERSION = "weighted-token-v1"
ALIGN_VERSION = "cosine-max-v1"
RANK_VERSION = "rank-v1"
TEXT_EMBEDDING_DIM = 16


@dataclass(frozen=True)
class FeatureConfig:
    tile_rows: int = 3
    tile_cols: int = 4
    hist_bins: int = 4
    mean_weight: float = 1.0
    hist_weight: float = 1.0
    projection_seed: str = "meeting-visual-projection-v1"
    canvas_shape: Tuple[int, int, int] = (120, 192, 3)
    texture_cell: int = 8

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "FeatureConfig":
        shape = value.get("canvas_shape", [120, 192, 3])
        try:
            shape_tuple = (int(shape[0]), int(shape[1]), int(shape[2]))
        except (TypeError, ValueError, IndexError) as exc:
            raise BusinessError(
                E_CONFIG_INVALID,
                "canvas_shape 必须为 [height, width, 3]",
                field="canvas_shape",
            ) from exc
        return cls(
            tile_rows=int(value.get("tile_rows", 3)),
            tile_cols=int(value.get("tile_cols", 4)),
            hist_bins=int(value.get("hist_bins", 4)),
            mean_weight=float(value.get("mean_weight", 1.0)),
            hist_weight=float(value.get("hist_weight", 1.0)),
            projection_seed=str(value.get("projection_seed", "meeting-visual-projection-v1")),
            canvas_shape=shape_tuple,
            texture_cell=int(value.get("texture_cell", 8)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tile_rows": self.tile_rows,
            "tile_cols": self.tile_cols,
            "hist_bins": self.hist_bins,
            "mean_weight": self.mean_weight,
            "hist_weight": self.hist_weight,
            "projection_seed": self.projection_seed,
            "canvas_shape": list(self.canvas_shape),
            "texture_cell": self.texture_cell,
        }

    def validate(self) -> None:
        if self.tile_rows <= 0 or self.tile_cols <= 0:
            raise BusinessError(
                E_GRID_INVALID,
                "分块网格行列必须为正整数",
                field="tile_grid",
                details={"tile_rows": self.tile_rows, "tile_cols": self.tile_cols},
            )
        if self.hist_bins <= 0:
            raise BusinessError(
                E_GRID_INVALID,
                "颜色直方图分箱数必须为正整数",
                field="hist_bins",
            )
        if self.texture_cell <= 0:
            raise BusinessError(
                E_GRID_INVALID,
                "纹理单元尺寸必须为正整数",
                field="texture_cell",
            )
        height, width, channels = self.canvas_shape
        if height <= 0 or width <= 0 or channels != 3:
            raise BusinessError(
                E_CONFIG_INVALID,
                "canvas_shape 必须为正的 RGB 画布尺寸",
                field="canvas_shape",
            )
        if self.mean_weight < 0 or self.hist_weight < 0:
            raise BusinessError(
                E_CONFIG_INVALID,
                "视觉权重不得为负数",
                field="mean_weight/hist_weight",
            )


@dataclass(frozen=True)
class RetrievalConfig:
    metric: str = "cosine"
    min_score: float = -1.0
    top_k: int = 5

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "RetrievalConfig":
        return cls(
            metric=str(value.get("metric", "cosine")),
            min_score=float(value.get("min_score", -1.0)),
            top_k=int(value.get("top_k", 5)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "min_score": self.min_score,
            "top_k": self.top_k,
        }

    def validate(self) -> None:
        if self.metric != "cosine":
            raise BusinessError(
                E_CONFIG_INVALID,
                "当前相似度口径仅支持 cosine",
                field="metric",
            )
        if not -1.0 <= self.min_score <= 1.0:
            raise BusinessError(
                E_CONFIG_INVALID,
                "min_score 必须位于 [-1, 1]",
                field="min_score",
            )
        if self.top_k <= 0:
            raise BusinessError(
                E_CONFIG_INVALID,
                "top_k 必须为正整数",
                field="top_k",
            )


@dataclass(frozen=True)
class ResourceLimits:
    max_pixel_bytes_per_image: int = 16 * 1024 * 1024
    max_cache_entries: int = 5000
    max_recompute_pairs: int = 128

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "ResourceLimits":
        return cls(
            max_pixel_bytes_per_image=int(value.get("max_pixel_bytes_per_image", 16 * 1024 * 1024)),
            max_cache_entries=int(value.get("max_cache_entries", 5000)),
            max_recompute_pairs=int(value.get("max_recompute_pairs", 128)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_pixel_bytes_per_image": self.max_pixel_bytes_per_image,
            "max_cache_entries": self.max_cache_entries,
            "max_recompute_pairs": self.max_recompute_pairs,
        }

    def validate(self) -> None:
        if (
            self.max_pixel_bytes_per_image <= 0
            or self.max_cache_entries <= 0
            or self.max_recompute_pairs <= 0
        ):
            raise BusinessError(
                E_CONFIG_INVALID,
                "资源上限必须为正整数",
                field="resource_limits",
            )


@dataclass
class Vocabulary:
    terms: List[str]
    tokenizer: str = "longest-match-v1"

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Vocabulary":
        terms = value.get("terms", [])
        if not isinstance(terms, list):
            raise BusinessError(E_CONFIG_INVALID, "词表 terms 必须为数组", field="terms")
        return cls(terms=[str(term) for term in terms], tokenizer=str(value.get("tokenizer", "longest-match-v1")))

    def to_dict(self) -> Dict[str, Any]:
        return {"terms": list(self.terms), "tokenizer": self.tokenizer}

    def validate(self) -> None:
        if self.tokenizer != "longest-match-v1":
            raise BusinessError(
                E_CONFIG_INVALID,
                "暂不支持该词项切分器版本",
                field="tokenizer",
            )
        normalized = [term.casefold() for term in self.terms]
        if not normalized or len(set(normalized)) != len(normalized):
            raise BusinessError(
                E_CONFIG_INVALID,
                "词表不得为空且不得包含重复词项",
                field="terms",
            )
        if any(not term.strip() for term in normalized):
            raise BusinessError(E_CONFIG_INVALID, "词表词项不得为空", field="terms")

    def token_index(self, term: str) -> int:
        normalized = term.casefold()
        try:
            return [item.casefold() for item in self.terms].index(normalized)
        except ValueError as exc:
            raise BusinessError(
                E_VOCAB_OUT_OF_RANGE,
                "词项不在受控词表范围内",
                field="terms",
                details={"term": term},
            ) from exc


@dataclass
class SampleRecord:
    pair_id: str
    image_seed: int
    image_shape: Tuple[int, int, int]
    slide_rect: Tuple[int, int, int, int]
    text: str
    locale: str

    @classmethod
    def from_dict(cls, value: Dict[str, Any], line_number: Optional[int] = None) -> "SampleRecord":
        pair_id_value = value.get("pair_id")
        pair_id = str(pair_id_value) if pair_id_value is not None else ""
        if not pair_id.strip():
            raise BusinessError(
                E_CONFIG_INVALID,
                "pair_id 不得为空",
                pair_id=pair_id or None,
                field="pair_id",
                details={"line": line_number},
            )

        seed = value.get("image_seed")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise BusinessError(
                E_IMAGE_SEED_INVALID,
                "image_seed 必须为整数",
                pair_id=pair_id,
                field="image_seed",
            )

        shape_value = value.get("image_shape")
        try:
            shape = tuple(int(part) for part in shape_value)
            if len(shape) != 3:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise BusinessError(
                E_IMAGE_SHAPE_MISMATCH,
                "image_shape 必须为 [height, width, 3]",
                pair_id=pair_id,
                field="image_shape",
            ) from exc

        rect_value = value.get("slide_rect")
        try:
            rect = tuple(int(part) for part in rect_value)
            if len(rect) != 4:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise BusinessError(
                E_RECT_INVALID,
                "slide_rect 必须为 [x0, y0, x1, y1]",
                pair_id=pair_id,
                field="slide_rect",
            ) from exc

        text = value.get("text")
        if not isinstance(text, str) or not text.strip():
            raise BusinessError(
                E_TEXT_EMPTY,
                "转写文本不得为空",
                pair_id=pair_id,
                field="text",
            )

        locale = value.get("locale")
        if not isinstance(locale, str) or not locale.strip():
            raise BusinessError(
                E_LOCALE_INVALID,
                "locale 不得为空",
                pair_id=pair_id,
                field="locale",
            )

        return cls(
            pair_id=pair_id,
            image_seed=int(seed),
            image_shape=(int(shape[0]), int(shape[1]), int(shape[2])),
            slide_rect=(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])),
            text=text,
            locale=locale.strip(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pair_id": self.pair_id,
            "image_seed": self.image_seed,
            "image_shape": list(self.image_shape),
            "slide_rect": list(self.slide_rect),
            "text": self.text,
            "locale": self.locale,
        }

    def validate(self, feature_config: FeatureConfig) -> None:
        if self.image_seed < 0 or self.image_seed >= 2**64:
            raise BusinessError(
                E_IMAGE_SEED_INVALID,
                "image_seed 必须位于 [0, 2^64)",
                pair_id=self.pair_id,
                field="image_seed",
            )
        if self.image_shape != feature_config.canvas_shape:
            raise BusinessError(
                E_IMAGE_SHAPE_MISMATCH,
                "画面种子对应的画布尺寸与固定配置不匹配",
                pair_id=self.pair_id,
                field="image_shape",
                details={
                    "expected": list(feature_config.canvas_shape),
                    "actual": list(self.image_shape),
                },
            )
        height, width, channels = self.image_shape
        if channels != 3 or height <= 0 or width <= 0:
            raise BusinessError(
                E_IMAGE_SHAPE_MISMATCH,
                "image_shape 必须为正的 RGB 画布尺寸",
                pair_id=self.pair_id,
                field="image_shape",
            )
        x0, y0, x1, y1 = self.slide_rect
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise BusinessError(
                E_RECT_INVALID,
                "slide_rect 必须位于画布内且满足 x0 < x1、y0 < y1",
                pair_id=self.pair_id,
                field="slide_rect",
                details={"image_shape": list(self.image_shape), "slide_rect": list(self.slide_rect)},
            )
        rect_height = y1 - y0
        rect_width = x1 - x0
        if feature_config.tile_rows > rect_height or feature_config.tile_cols > rect_width:
            raise BusinessError(
                E_GRID_INVALID,
                "分块网格超过片区像素边界",
                pair_id=self.pair_id,
                field="tile_grid",
                details={
                    "tile_grid": [feature_config.tile_rows, feature_config.tile_cols],
                    "slide_rect_size": [rect_width, rect_height],
                },
            )
        if not self.text.strip():
            raise BusinessError(
                E_TEXT_EMPTY,
                "转写文本不得为空",
                pair_id=self.pair_id,
                field="text",
            )


def natural_key(pair_id: str) -> Tuple[Any, ...]:
    """Natural ordering so p2 sorts before p10 when present in pair ids."""
    parts = re.split(r"(\d+)", pair_id)
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts)


@dataclass
class WorkspaceConfig:
    features: FeatureConfig = field(default_factory=FeatureConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "WorkspaceConfig":
        return cls(
            features=FeatureConfig.from_dict(value.get("features", {})),
            retrieval=RetrievalConfig.from_dict(value.get("retrieval", {})),
            limits=ResourceLimits.from_dict(value.get("limits", {})),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "features": self.features.to_dict(),
            "retrieval": self.retrieval.to_dict(),
            "limits": self.limits.to_dict(),
        }

    def validate(self) -> None:
        self.features.validate()
        self.retrieval.validate()
        self.limits.validate()

    def clone(self) -> "WorkspaceConfig":
        return WorkspaceConfig.from_dict(copy.deepcopy(self.to_dict()))
