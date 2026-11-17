"""编码变换序列 -> 规范编码族链。

归一两步走，均在单次栈扫描内完成：
1. 连续同类合并：相邻且 (编码族, 方向) 相同的变换合并为一个规范步骤，
   合并后保留全部原始序号；
2. 同族反向抵消：栈顶与新变换编码族相同、方向相反时成对消去，
   抵消记录保留双方原始序号，供报告定位。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .transforms import ENCODE, DECODE, resolve


@dataclass
class ChainStep:
    """规范链中的一个步骤（已合并连续同类变换）。"""

    family: str
    direction: str
    indices: list[int] = field(default_factory=list)
    names: list[str] = field(default_factory=list)

    def label(self) -> str:
        return f"{self.family}:{self.direction}"


@dataclass
class CancelRecord:
    """一次编码-解码抵消，保留被消去变换的原始序号。"""

    family: str
    encode_indices: list[int]
    decode_indices: list[int]

    def is_encode_then_decode(self) -> bool:
        """顺序颠倒判定：编码序号整体早于解码序号。"""
        return max(self.encode_indices) < min(self.decode_indices)

    def revert_index(self) -> int:
        """触发还原的解码变换序号（取最后一个解码）。"""
        return max(self.decode_indices)


@dataclass
class NormalizedChain:
    steps: list[ChainStep]
    cancels: list[CancelRecord]

    @property
    def signature(self) -> str:
        """规范链签名，用作编码族等价类的键。"""
        return ">".join(step.label() for step in self.steps)

    @property
    def encode_families(self) -> list[str]:
        return [step.family for step in self.steps if step.direction == ENCODE]

    @property
    def decode_families(self) -> list[str]:
        return [step.family for step in self.steps if step.direction == DECODE]

    def describe(self) -> list[dict]:
        return [
            {
                "family": step.family,
                "direction": step.direction,
                "original_indices": step.indices,
                "transform_names": step.names,
            }
            for step in self.steps
        ]


def normalize(transform_names: list[str]) -> NormalizedChain:
    """把变换名序列归一为规范编码族链。

    调用方需保证变换名均在已知集合内（解析层已校验）。
    """
    stack: list[ChainStep] = []
    cancels: list[CancelRecord] = []

    for index, name in enumerate(transform_names, start=1):
        family, direction = resolve(name)
        if stack and stack[-1].family == family:
            top = stack[-1]
            if top.direction == direction:
                # 连续同类合并
                top.indices.append(index)
                top.names.append(name)
                continue
            # 同族方向相反 -> 成对抵消并出栈
            popped = stack.pop()
            if direction == DECODE:
                encode_indices, decode_indices = popped.indices, [index]
            else:
                encode_indices, decode_indices = [index], popped.indices
            cancels.append(
                CancelRecord(
                    family=family,
                    encode_indices=encode_indices,
                    decode_indices=decode_indices,
                )
            )
            continue
        stack.append(ChainStep(family=family, direction=direction, indices=[index], names=[name]))

    return NormalizedChain(steps=stack, cancels=cancels)
