"""确定性逐 step 采样核心。

流水线（每个解码 step）：

1. 从预生成 logits 矩阵取当前 step 的行；
2. 温度缩放 ``logits / temperature``；
3. ``top_k`` 截断（``top_k == 0`` 表示不启用，稳定排序保证并列可复现）；
4. softmax 归一化得到概率；
5. ``top_p`` 核采样截断，保留累计概率覆盖阈值所需的最小 token 集合；
6. 用请求私有 ``numpy.random.RandomState(seed)`` 的显式均匀随机数做逆 CDF 抽取。

随机源唯一且显式声明：除该请求种子驱动的 RandomState 外不读取任何随机源，
也不读取系统时间。同请求、同参数、同种子、同 logits 必然逐字节复现。
"""

from __future__ import annotations

import numpy as np

from .models import Request, SampleResult, StepRecord, Vocab


def _stable_descending_order(scores: np.ndarray) -> np.ndarray:
    """按分数降序、分数并列按 token id 升序的确定性排序。"""
    order = np.lexsort((np.arange(scores.shape[0]), -scores))
    return order


def softmax(scores: np.ndarray) -> np.ndarray:
    shifted = scores - np.max(scores)
    exp_scores = np.exp(shifted)
    return exp_scores / np.sum(exp_scores)


def apply_top_k(token_ids: np.ndarray, logits: np.ndarray, top_k: int) -> np.ndarray:
    """在候选集合上应用 top_k，返回保留的 token id（按确定性顺序）。"""
    if top_k <= 0 or top_k >= token_ids.shape[0]:
        return token_ids
    order = _stable_descending_order(logits[token_ids])
    kept_positions = order[:top_k]
    kept = token_ids[kept_positions]
    # 再次按 id 排序，保证集合表示稳定。
    return np.sort(kept)


def apply_top_p(
    token_ids: np.ndarray, probabilities: np.ndarray, top_p: float
) -> np.ndarray:
    """核采样：按概率降序累计，保留使累计概率 >= top_p 的最小前缀。

    至少保留一个 token；``top_p >= 1`` 时等价于全保留。
    """
    if token_ids.shape[0] <= 1 or top_p >= 1.0:
        return token_ids
    order = _stable_descending_order(probabilities[token_ids])
    sorted_ids = token_ids[order]
    sorted_probs = probabilities[sorted_ids]
    cumulative = np.cumsum(sorted_probs)
    # 第一个使累计概率达到 top_p 的位置（含），且至少保留首位。
    reached = np.nonzero(cumulative >= top_p - 1e-12)[0]
    cutoff = int(reached[0]) if reached.size else sorted_ids.shape[0] - 1
    kept = sorted_ids[: cutoff + 1]
    return np.sort(kept)


def inverse_cdf_sample(
    token_ids: np.ndarray, probabilities: np.ndarray, uniform: float
) -> int:
    """用显式均匀随机数在按 id 升序排列的候选上做逆 CDF 抽取。"""
    sorted_ids = np.sort(token_ids)
    probs = probabilities[sorted_ids]
    cumulative = np.cumsum(probs)
    # 归一化保护：候选截断后重新归一化，使最后一个桶的上界为 1。
    cumulative = cumulative / cumulative[-1]
    hits = np.nonzero(uniform < cumulative)[0]
    index = int(hits[0]) if hits.size else sorted_ids.shape[0] - 1
    return int(sorted_ids[index])


def sample_request(
    request: Request,
    vocab: Vocab,
    logits: np.ndarray,
    *,
    param_override: "SamplingParams | None" = None,
) -> SampleResult:
    """对单个请求执行完整确定性解码。

    ``param_override`` 用于策略裁决中模拟收紧 top-p 等场景；默认使用请求自带参数。
    """
    from .models import SamplingParams

    params = param_override or request.sampling_params
    if not isinstance(params, SamplingParams):
        params = SamplingParams.from_dict(params)  # type: ignore[arg-type]

    rng = np.random.RandomState(request.seed)
    context = list(request.prompt_tokens)
    result = SampleResult(
        request_id=request.request_id,
        seed=request.seed,
        vocab_version=vocab.vocab_version,
        prompt_tokens=tuple(request.prompt_tokens),
    )

    max_steps = request.target_length
    for step in range(max_steps):
        row = logits[step] if step < logits.shape[0] else logits[-1]
        scaled = row / params.temperature

        all_ids = np.arange(vocab.size, dtype=np.int64)
        top_k_kept = apply_top_k(all_ids, scaled, params.top_k)

        candidate_scores = np.full(vocab.size, -np.inf, dtype=np.float64)
        candidate_scores[top_k_kept] = scaled[top_k_kept]
        probabilities = softmax(candidate_scores)

        top_p_kept = apply_top_p(top_k_kept, probabilities, params.top_p)
        uniform = float(rng.random_sample())
        chosen = inverse_cdf_sample(
            top_p_kept, probabilities, uniform
        )

        kept_probs = probabilities[top_p_kept]
        kept_probs = kept_probs / np.sum(kept_probs)
        prob_pairs = tuple(
            (int(tid), float(prob))
            for tid, prob in zip(np.sort(top_p_kept), kept_probs)
        )

        finished = chosen == vocab.eos_token_id or step == max_steps - 1
        record = StepRecord(
            step=step,
            context_tokens=tuple(context),
            top_k_kept=tuple(int(t) for t in top_k_kept),
            top_p_kept=tuple(int(t) for t in top_p_kept),
            probabilities=prob_pairs,
            random_uniform=uniform,
            chosen_token_id=chosen,
            finished=finished,
        )
        result.steps.append(record)
        result.generated_tokens.append(chosen)
        context.append(chosen)

        if chosen == vocab.eos_token_id:
            result.finished_by_eos = True
            break

    return result
