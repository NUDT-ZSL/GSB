"""Top-level, picklable worker entry points for the local process pool."""

from __future__ import annotations

from dataclasses import asdict

from .errors import ErrorCode, PipelineError
from .fusion import fuse
from .merge import merge_candidates
from .models import ChannelHit, FusionDetail, Query, QueryResult
from .recall import RecallIndex


def process_batch(
    documents: list,
    config_dict: dict,
    batch_id: int,
    assignments: list[dict],
) -> dict:
    """Run recall -> merge -> fusion for every admitted query in a batch.

    ``documents`` and the reconstructed config are passed explicitly so the
    function is importable/picklable and works under Windows spawn start method.
    Any per-query failure is captured as a structured error and never aborts
    sibling queries in the same batch.
    """
    from .config import FusionConfig, ChannelConfig, BudgetConfig, SchedulerConfig

    channels = {
        name: ChannelConfig(**patch) for name, patch in config_dict["channels"].items()
    }
    config = FusionConfig(
        channels=channels,
        budgets=BudgetConfig(**config_dict["budgets"]),
        scheduler=SchedulerConfig(**config_dict["scheduler"]),
        seed=config_dict.get("seed", 20261124),
    )
    index = RecallIndex(documents)
    results: list[dict] = []
    errors: list[dict] = []

    for assignment in assignments:
        query = Query(**assignment["query"])
        caps = assignment["caps"]
        try:
            channel_candidates = index.run_all(query, caps)
            merged = merge_candidates(
                channel_candidates,
                config.scheduler.merge_policy,
                query.query_id,
            )
            fused = fuse(
                merged, channel_candidates, config, query.query_id
            )
            fused = fused[: query.top_k]
            recall_cost = sum(len(hits) for hits in channel_candidates.values())
            fusion_cost = len(merged)
            result = QueryResult(
                query_id=query.query_id,
                batch_id=batch_id,
                channel_candidates=channel_candidates,
                merged=merged,
                fused=fused,
                per_channel_caps=dict(caps),
                cost_recall=recall_cost,
                cost_fusion=fusion_cost,
                two_stage=config.scheduler.two_stage_enabled,
            )
            results.append(_serialize_result(result))
        except PipelineError as exc:
            exc.batch_id = batch_id
            errors.append(exc.to_dict())
        except Exception as exc:  # noqa: BLE001 - surfaced as structured worker error
            errors.append(
                PipelineError(
                    code=ErrorCode.E_WORKER,
                    stage="worker",
                    message=f"{type(exc).__name__}: {exc}",
                    query_id=query.query_id,
                    batch_id=batch_id,
                ).to_dict()
            )

    return {"batch_id": batch_id, "results": results, "errors": errors}


def _serialize_result(result: QueryResult) -> dict:
    return {
        "query_id": result.query_id,
        "batch_id": result.batch_id,
        "channel_candidates": {
            channel: [asdict(candidate) for candidate in candidates]
            for channel, candidates in result.channel_candidates.items()
        },
        "merged": [
            {
                "doc_id": record.doc_id,
                "hits": [asdict(hit) for hit in record.hits],
                "kept_channel": record.kept_channel,
                "kept_raw_score": record.kept_raw_score,
            }
            for record in result.merged
        ],
        "fused": [
            {
                "doc_id": candidate.doc_id,
                "final_score": candidate.final_score,
                "kept_channel": candidate.kept_channel,
                "details": [asdict(detail) for detail in candidate.details],
                "all_hits": [asdict(hit) for hit in candidate.all_hits],
            }
            for candidate in result.fused
        ],
        "per_channel_caps": result.per_channel_caps,
        "cost_recall": result.cost_recall,
        "cost_fusion": result.cost_fusion,
        "two_stage": result.two_stage,
    }


def deserialize_result(row: dict) -> QueryResult:
    from .models import Candidate, FusedCandidate, MergedCandidate

    channel_candidates = {
        channel: [Candidate(**hit) for hit in hits]
        for channel, hits in row["channel_candidates"].items()
    }
    merged = [
        MergedCandidate(
            doc_id=record["doc_id"],
            hits=[ChannelHit(**hit) for hit in record["hits"]],
            kept_channel=record["kept_channel"],
            kept_raw_score=record["kept_raw_score"],
        )
        for record in row["merged"]
    ]
    fused = [
        FusedCandidate(
            doc_id=record["doc_id"],
            final_score=record["final_score"],
            kept_channel=record["kept_channel"],
            details=[FusionDetail(**detail) for detail in record["details"]],
            all_hits=[ChannelHit(**hit) for hit in record["all_hits"]],
        )
        for record in row["fused"]
    ]
    return QueryResult(
        query_id=row["query_id"],
        batch_id=row["batch_id"],
        channel_candidates=channel_candidates,
        merged=merged,
        fused=fused,
        per_channel_caps=row["per_channel_caps"],
        cost_recall=row["cost_recall"],
        cost_fusion=row["cost_fusion"],
        two_stage=row["two_stage"],
    )
