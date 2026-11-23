"""核心数据模型。

所有模型都提供 ``to_dict`` / ``from_dict``，保证可以序列化为 JSON 并逐字节
复现；模型内部不持有任何隐式随机源或时间相关字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(frozen=True)
class SamplingParams:
    """单个请求的采样参数。"""

    temperature: float
    top_k: int
    top_p: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SamplingParams":
        return cls(
            temperature=float(data["temperature"]),
            top_k=int(data["top_k"]),
            top_p=float(data["top_p"]),
        )


@dataclass(frozen=True)
class Request:
    """本地确定性生成的推理请求。"""

    request_id: str
    prompt_tokens: tuple[int, ...]
    sampling_params: SamplingParams
    target_length: int
    seed: int
    priority: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "prompt_tokens": list(self.prompt_tokens),
            "sampling_params": self.sampling_params.to_dict(),
            "target_length": self.target_length,
            "seed": self.seed,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Request":
        return cls(
            request_id=str(data["request_id"]),
            prompt_tokens=tuple(int(t) for t in data["prompt_tokens"]),
            sampling_params=SamplingParams.from_dict(data["sampling_params"]),
            target_length=int(data["target_length"]),
            seed=int(data["seed"]),
            priority=int(data["priority"]),
        )


@dataclass(frozen=True)
class Vocab:
    """词表标识与 token id -> token 文本的映射。"""

    vocab_version: str
    eos_token_id: int
    id_to_token: tuple[str, ...]

    @property
    def size(self) -> int:
        return len(self.id_to_token)

    def to_dict(self) -> dict[str, Any]:
        return {
            "vocab_version": self.vocab_version,
            "eos_token_id": self.eos_token_id,
            "id_to_token": list(self.id_to_token),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Vocab":
        return cls(
            vocab_version=str(data["vocab_version"]),
            eos_token_id=int(data["eos_token_id"]),
            id_to_token=tuple(str(t) for t in data["id_to_token"]),
        )


@dataclass(frozen=True)
class StepRecord:
    """单个解码 step 的候选截断与抽取记录。"""

    step: int
    context_tokens: tuple[int, ...]
    top_k_kept: tuple[int, ...]
    top_p_kept: tuple[int, ...]
    probabilities: tuple[tuple[int, float], ...]
    random_uniform: float
    chosen_token_id: int
    finished: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "context_tokens": list(self.context_tokens),
            "top_k_kept": list(self.top_k_kept),
            "top_p_kept": list(self.top_p_kept),
            "probabilities": [[tid, prob] for tid, prob in self.probabilities],
            "random_uniform": self.random_uniform,
            "chosen_token_id": self.chosen_token_id,
            "finished": self.finished,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StepRecord":
        return cls(
            step=int(data["step"]),
            context_tokens=tuple(int(t) for t in data["context_tokens"]),
            top_k_kept=tuple(int(t) for t in data["top_k_kept"]),
            top_p_kept=tuple(int(t) for t in data["top_p_kept"]),
            probabilities=tuple(
                (int(tid), float(p)) for tid, p in data["probabilities"]
            ),
            random_uniform=float(data["random_uniform"]),
            chosen_token_id=int(data["chosen_token_id"]),
            finished=bool(data["finished"]),
        )


@dataclass
class SampleResult:
    """单个请求的完整采样产物。"""

    request_id: str
    seed: int
    vocab_version: str
    prompt_tokens: tuple[int, ...]
    generated_tokens: list[int] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)
    finished_by_eos: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "seed": self.seed,
            "vocab_version": self.vocab_version,
            "prompt_tokens": list(self.prompt_tokens),
            "generated_tokens": list(self.generated_tokens),
            "steps": [s.to_dict() for s in self.steps],
            "finished_by_eos": self.finished_by_eos,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SampleResult":
        return cls(
            request_id=str(data["request_id"]),
            seed=int(data["seed"]),
            vocab_version=str(data["vocab_version"]),
            prompt_tokens=tuple(int(t) for t in data["prompt_tokens"]),
            generated_tokens=[int(t) for t in data["generated_tokens"]],
            steps=[StepRecord.from_dict(s) for s in data["steps"]],
            finished_by_eos=bool(data["finished_by_eos"]),
        )


@dataclass(frozen=True)
class LedgerEvent:
    """预算账本中的一条确定性扣减/释放事件。"""

    seq: int
    request_id: str
    kind: str  # reserve | release
    dimension: str  # steps | cache | slots
    amount: int
    balance_after: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LedgerEvent":
        return cls(
            seq=int(data["seq"]),
            request_id=str(data["request_id"]),
            kind=str(data["kind"]),
            dimension=str(data["dimension"]),
            amount=int(data["amount"]),
            balance_after=int(data["balance_after"]),
        )
