"""本地确定性样本生成与加载。

所有数据都由一个显式声明的主种子（master seed）驱动 :class:`numpy.random.RandomState`
生成，不依赖系统时间或任何外部服务。生成产物包括：

* ``vocab.json``        词表映射与词表版本；
* ``requests.jsonl``    请求清单（每行一个请求）；
* ``logits/<rid>.npy``  每个请求的 logits 矩阵，行对应解码 step。

同一种子与同一规模参数下，产物在不同机器/进程上逐字节一致。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

from .errors import BenchError, E_VOCAB_VERSION_MISSING
from .models import Request, SamplingParams, Vocab

VOCAB_FILENAME = "vocab.json"
REQUESTS_FILENAME = "requests.jsonl"
LOGITS_DIRNAME = "logits"

# 四档固定采样参数模板，请求按序号轮转选取，保证确定性。
PARAM_PROFILES: tuple[SamplingParams, ...] = (
    SamplingParams(temperature=1.0, top_k=0, top_p=1.0),
    SamplingParams(temperature=0.8, top_k=5, top_p=0.9),
    SamplingParams(temperature=1.2, top_k=3, top_p=0.7),
    SamplingParams(temperature=0.6, top_k=10, top_p=0.95),
)


def _stable_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def generate_dataset(
    out_dir: str | Path,
    *,
    num_requests: int,
    vocab_size: int = 32,
    seed: int = 20261124,
    max_prompt: int = 6,
    max_length: int = 8,
) -> dict[str, str]:
    """生成完整的确定性数据集并落盘。

    返回各产物文件的绝对路径字典。
    """
    if num_requests <= 0:
        raise ValueError("num_requests must be positive")
    if vocab_size < 4:
        raise ValueError("vocab_size must be >= 4")

    out = Path(out_dir)
    _stable_dir(out)
    logits_dir = _stable_dir(out / LOGITS_DIRNAME)
    rng = np.random.RandomState(seed)

    eos_token_id = 0
    id_to_token = [f"<eos>"] + [f"tok{idx:04d}" for idx in range(1, vocab_size)]
    vocab = Vocab(
        vocab_version=f"vocab-det-v1-s{seed}",
        eos_token_id=eos_token_id,
        id_to_token=tuple(id_to_token),
    )
    (out / VOCAB_FILENAME).write_text(
        json.dumps(vocab.to_dict(), ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    request_lines: list[str] = []
    for index in range(num_requests):
        request_id = f"req-{index:04d}"
        prompt_len = int(rng.randint(2, max_prompt + 1))
        prompt_tokens = tuple(int(t) for t in rng.randint(1, vocab_size, size=prompt_len))
        params = PARAM_PROFILES[index % len(PARAM_PROFILES)]
        target_length = int(rng.randint(3, max_length + 1))
        # 请求种子由主 RNG 显式派生，仍然是声明式的固定整数。
        request_seed = int(rng.randint(1, 2**31 - 1))
        priority = int(index % 5)  # 0 为最高优先级
        request = Request(
            request_id=request_id,
            prompt_tokens=prompt_tokens,
            sampling_params=params,
            target_length=target_length,
            seed=request_seed,
            priority=priority,
        )
        request_lines.append(
            json.dumps(request.to_dict(), ensure_ascii=False, sort_keys=True)
        )

        # logits 行数覆盖 target_length 个解码 step；固定 dtype 保证跨平台一致。
        logits = rng.normal(
            loc=0.0,
            scale=2.0,
            size=(target_length, vocab_size),
        ).astype(np.float64)
        # 给 EOS 一个温和的负偏置，使自然结束与长度结束都可能出现，且可复现。
        logits[:, eos_token_id] -= 1.5
        np.save(logits_dir / f"{request_id}.npy", logits, allow_pickle=False)

    (out / REQUESTS_FILENAME).write_text(
        "\n".join(request_lines) + "\n", encoding="utf-8"
    )

    return {
        "vocab": str((out / VOCAB_FILENAME).resolve()),
        "requests": str((out / REQUESTS_FILENAME).resolve()),
        "logits_dir": str(logits_dir.resolve()),
    }


def load_vocab(data_dir: str | Path) -> Vocab:
    path = Path(data_dir) / VOCAB_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("vocab_version"):
        raise BenchError(
            E_VOCAB_VERSION_MISSING,
            "vocab file lacks vocab_version",
            stage="load_vocab",
            field="vocab_version",
        )
    return Vocab.from_dict(data)


def load_requests(data_dir: str | Path) -> list[Request]:
    path = Path(data_dir) / REQUESTS_FILENAME
    requests: list[Request] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            requests.append(Request.from_dict(json.loads(line)))
    return requests


def load_logits(data_dir: str | Path, request_id: str) -> np.ndarray:
    path = Path(data_dir) / LOGITS_DIRNAME / f"{request_id}.npy"
    return np.load(path, allow_pickle=False)


def dataset_summary(data_dir: str | Path) -> dict[str, object]:
    vocab = load_vocab(data_dir)
    requests = load_requests(data_dir)
    return {
        "vocab_version": vocab.vocab_version,
        "vocab_size": vocab.size,
        "num_requests": len(requests),
        "request_ids": [r.request_id for r in requests],
        "data_dir": str(Path(data_dir).resolve()),
    }
