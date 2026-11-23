from __future__ import annotations

from dataclasses import asdict, dataclass

from .errors import fail
from .hashing import digest_obj


@dataclass(frozen=True)
class Block:
    index: int
    row: int
    col: int
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def pixel_count(self) -> int:
        return self.width * self.height

    @property
    def pixel_range(self) -> dict[str, int]:
        return {"x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1}

    def to_dict(self) -> dict[str, int]:
        return {
            "index": self.index,
            "row": self.row,
            "col": self.col,
            "x0": self.x0,
            "y0": self.y0,
            "x1": self.x1,
            "y1": self.y1,
        }


@dataclass(frozen=True)
class BlockPlan:
    width: int = 256
    height: int = 192
    tile_width: int = 64
    tile_height: int = 48

    def __post_init__(self) -> None:
        for name, value in (
            ("width", self.width),
            ("height", self.height),
            ("tile_width", self.tile_width),
            ("tile_height", self.tile_height),
        ):
            if not isinstance(value, int) or value <= 0:
                fail("E_PARAM_INVALID", f"plan.{name} 必须为正整数", field=f"plan.{name}")
        if self.tile_width > 4096 or self.tile_height > 4096:
            fail("E_PARAM_INVALID", "块尺寸不得超过 4096", field="plan")

    @property
    def rows(self) -> int:
        return (self.height + self.tile_height - 1) // self.tile_height

    @property
    def cols(self) -> int:
        return (self.width + self.tile_width - 1) // self.tile_width

    @property
    def block_count(self) -> int:
        return self.rows * self.cols

    @property
    def pixel_count(self) -> int:
        return self.width * self.height

    def block(self, index: int) -> Block:
        if not 0 <= index < self.block_count:
            fail(
                "E_BLOCK_NOT_FOUND",
                "切片编号超出分块计划范围",
                block=index,
                block_count=self.block_count,
            )
        row, col = divmod(index, self.cols)
        x0 = col * self.tile_width
        y0 = row * self.tile_height
        return Block(index, row, col, x0, y0, min(self.width, x0 + self.tile_width), min(self.height, y0 + self.tile_height))

    def blocks(self) -> list[Block]:
        return [self.block(index) for index in range(self.block_count)]

    def digest(self) -> str:
        return digest_obj(asdict(self))

    def to_dict(self) -> dict[str, int]:
        return {
            **asdict(self),
            "rows": self.rows,
            "cols": self.cols,
            "block_count": self.block_count,
        }

    def influence_pixels(self, index: int) -> int:
        block = self.block(index)
        return (block.width + 2) * (block.height + 2)

