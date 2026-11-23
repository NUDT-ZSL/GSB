"""确定性批次分块调度层。

职责：

* 按可配置并发槽位与每批请求数，把准入请求切成确定性批次；
* 通过本地进程池（``ProcessPoolExecutor``）并行采样，分块边界、派发顺序、
  归并顺序全部由请求标识的确定性排序决定；
* 对每个请求比对「进程池结果」与「单进程独立重算结果」，任何差异都返回
  ``E_BATCH_MISMATCH`` 并指出请求标识与首个偏差 step。
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .errors import BenchError, E_BATCH_MISMATCH
from .models import Request, SampleResult, StepRecord


# 进程池 worker 的全局上下文，由 initializer 一次性加载，避免重复传大数组。
_WORKER_CTX: dict[str, Any] = {}


def _worker_init(data_dir: str, vocab_version: str) -> None:
    from .dataset import load_logits, load_vocab

    _WORKER_CTX["data_dir"] = data_dir
    _WORKER_CTX["vocab"] = load_vocab(data_dir)
    if _WORKER_CTX["vocab"].vocab_version != vocab_version:
        raise RuntimeError(
            "worker vocab version mismatch: "
            f"{_WORKER_CTX['vocab'].vocab_version} != {vocab_version}"
        )


def _worker_sample(task: tuple[dict[str, Any], int, str | None]) -> dict[str, Any]:
    from .dataset import load_logits
    from .models import SamplingParams
    from .sampler import sample_request

    request_dict, effective_length, override_dict = task
    request = Request.from_dict(request_dict)
    if effective_length != request.target_length:
        request = Request(
            request_id=request.request_id,
            prompt_tokens=request.prompt_tokens,
            sampling_params=request.sampling_params,
            target_length=effective_length,
            seed=request.seed,
            priority=request.priority,
        )
    override = SamplingParams.from_dict(override_dict) if override_dict else None
    logits = load_logits(_WORKER_CTX["data_dir"], request.request_id)
    result = sample_request(request, _WORKER_CTX["vocab"], logits, param_override=override)
    return result.to_dict()


def chunk_requests(
    requests: Sequence[Request], batch_size: int
) -> list[list[Request]]:
    """按确定性顺序切块；``batch_size`` 为每批请求数。"""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    ordered = sorted(requests, key=lambda req: req.request_id)
    return [
        list(ordered[index : index + batch_size])
        for index in range(0, len(ordered), batch_size)
    ]


def first_divergence(left: SampleResult, right: SampleResult) -> int | None:
    """返回两个采样结果首个偏差 step，完全一致返回 None。"""
    left_tokens = left.generated_tokens
    right_tokens = right.generated_tokens
    max_len = max(len(left_tokens), len(right_tokens))
    for step in range(max_len):
        if step >= len(left_tokens) or step >= len(right_tokens):
            return step
        if left_tokens[step] != right_tokens[step]:
            return step
    if _records_signature(left.steps) != _records_signature(right.steps):
        for index, (a, b) in enumerate(zip(left.steps, right.steps)):
            if _record_signature(a) != _record_signature(b):
                return index
    return None


def _record_signature(record: StepRecord) -> tuple[Any, ...]:
    return (
        record.step,
        tuple(record.top_k_kept),
        tuple(record.top_p_kept),
        tuple((tid, round(prob, 12)) for tid, prob in record.probabilities),
        round(record.random_uniform, 15),
        record.chosen_token_id,
        record.finished,
    )


def _records_signature(records: list[StepRecord]) -> tuple[tuple[Any, ...], ...]:
    return tuple(_record_signature(record) for record in records)


@dataclass
class BatchTask:
    request: Request
    effective_length: int
    override_params: dict[str, Any] | None

    def as_worker_task(self) -> tuple[dict[str, Any], int, dict[str, Any] | None]:
        return (
            self.request.to_dict(),
            self.effective_length,
            self.override_params,
        )


class Scheduler:
    def __init__(
        self,
        data_dir: str,
        vocab,
        *,
        max_workers: int,
        batch_size: int,
        verify: bool = True,
    ):
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        self.data_dir = data_dir
        self.vocab = vocab
        self.max_workers = max_workers
        self.batch_size = batch_size
        self.verify = verify

    def run_batches(
        self,
        tasks: Sequence[BatchTask],
        *,
        on_batch_committed: Callable[[list[SampleResult], list[BatchTask]], None]
        | None = None,
        crash_after_committed: int | None = None,
        already_committed: int = 0,
    ) -> list[SampleResult]:
        """按确定性批次执行，归并顺序与请求标识排序一致。

        ``on_batch_committed`` 在每个批次边界回调，用于提交检查点。
        ``crash_after_committed`` 用于演示强制终止：累计提交达到该请求数后，
        在写下一个检查点之前抛出 :class:`ForcedTermination`。
        """
        ordered = sorted(tasks, key=lambda task: task.request.request_id)
        batches = [
            ordered[index : index + self.batch_size]
            for index in range(0, len(ordered), self.batch_size)
        ]

        merged: list[SampleResult] = []
        committed_count = already_committed
        executor = ProcessPoolExecutor(
            max_workers=self.max_workers,
            initializer=_worker_init,
            initargs=(self.data_dir, self.vocab.vocab_version),
        )
        try:
            for batch in batches:
                worker_tasks = [task.as_worker_task() for task in batch]
                pooled_dicts = list(executor.map(_worker_sample, worker_tasks))
                pooled_results = [SampleResult.from_dict(item) for item in pooled_dicts]

                if self.verify:
                    self._verify_batch(batch, pooled_results)

                # 归并顺序固定为请求标识升序。
                pooled_results.sort(key=lambda result: result.request_id)
                merged.extend(pooled_results)

                if on_batch_committed is not None:
                    on_batch_committed(pooled_results, list(batch))
                committed_count += len(batch)
                if (
                    crash_after_committed is not None
                    and committed_count >= crash_after_committed
                    and committed_count < already_committed + len(ordered)
                ):
                    raise ForcedTermination(
                        f"forced termination after {committed_count} committed requests"
                    )
        finally:
            executor.shutdown(wait=True)

        merged.sort(key=lambda result: result.request_id)
        return merged

    def _verify_batch(
        self, batch: Sequence[BatchTask], pooled_results: Sequence[SampleResult]
    ) -> None:
        from .dataset import load_logits
        from .sampler import sample_request

        pooled_by_id = {result.request_id: result for result in pooled_results}
        for task in batch:
            request = task.request
            if task.effective_length != request.target_length:
                request = Request(
                    request_id=request.request_id,
                    prompt_tokens=request.prompt_tokens,
                    sampling_params=request.sampling_params,
                    target_length=task.effective_length,
                    seed=request.seed,
                    priority=request.priority,
                )
            override = None
            if task.override_params is not None:
                from .models import SamplingParams

                override = SamplingParams.from_dict(task.override_params)
            logits = load_logits(self.data_dir, request.request_id)
            solo = sample_request(request, self.vocab, logits, param_override=override)
            pooled = pooled_by_id[request.request_id]
            divergence = first_divergence(pooled, solo)
            if divergence is not None:
                raise BenchError(
                    E_BATCH_MISMATCH,
                    (
                        f"pooled result diverges from solo result at step {divergence}"
                    ),
                    request_id=request.request_id,
                    stage="batch.verify",
                    field=f"steps[{divergence}]",
                    detail={"first_divergence_step": divergence},
                )


class ForcedTermination(RuntimeError):
    """模拟执行器在请求边界被强制终止（SIGTERM/kill -9 语义）。"""
