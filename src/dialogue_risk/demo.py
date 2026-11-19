"""One-shot acceptance demonstration using a temporary runtime."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from .aggregate import verify_aggregation
from .consistency import compare_results
from .errors import AppError
from .service import DomainService
from .snapshot import SnapshotStore


def _capture(call: Callable[[], Any]) -> dict[str, Any]:
    try:
        value = call()
    except AppError as exc:
        return {"raised": True, **exc.to_dict()["error"]}
    return {"raised": False, "value": value}


def _append(report: dict[str, Any], name: str, payload: dict[str, Any]) -> None:
    report["steps"].append({"step": name, **payload})


def run_demo(data_dir: str | Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "ok": True,
        "seed_sample": str(Path(data_dir) / "sample_meta.json"),
        "steps": [],
        "anomalies": [],
    }
    with tempfile.TemporaryDirectory(prefix="dialogue-risk-demo-") as temporary:
        runtime = Path(temporary) / "runtime"
        service = DomainService(data_dir, runtime)
        old_snapshot = Path(temporary) / "old_snapshot.json"
        shutil.copy2(service.store.path, old_snapshot)

        batch = service.run_batch()
        _append(
            report,
            "baseline_batch",
            {
                "ok": True,
                "snapshot_version": batch["snapshot_version"],
                "versions": batch["versions"],
                "conversation_count": batch["conversation_count"],
                "message_count": batch["message_count"],
                "batch_count": batch["batch_count"],
            },
        )

        before_query_stats = service.cache.stats_snapshot()
        query = service.query("conv-0001")
        _append(
            report,
            "baseline_query",
            {
                "ok": True,
                "conv_id": query["conv_id"],
                "sentiment_label": query["sentiment_label"],
                "sentiment_score": query["sentiment_score"],
                "risk_level": query["risk_level"],
                "risk_score": query["risk_score"],
                "cache_hit": query["query_meta"]["cache_hit"],
                "latency": query["query_meta"]["latency"],
                "consistency": query["query_meta"]["consistency"],
                "cache_before": before_query_stats,
            },
        )

        other_key_before = None
        conv1_key = None
        expected_other = None
        for entry in service.cache.entries():
            if entry.conv_id == "conv-0001":
                conv1_key = entry.key
            elif other_key_before is None:
                other_key_before = entry.key
                expected_other = entry.conv_id
        edit = service.edit_text(
            "conv-0001",
            2,
            "对方让我提供验证码并转账，我非常失望，处理太慢",
        )
        keys_after = set(service.cache.keys())
        other_preserved = other_key_before in keys_after
        _append(
            report,
            "edit_text_cache_invalidation",
            {
                "ok": bool(
                    edit["invalidated_cache_entries"] >= 1
                    and conv1_key not in keys_after
                    and other_preserved
                ),
                "commit": edit,
                "edited_cache_key_removed": conv1_key not in keys_after,
                "unaffected_conversation": expected_other,
                "unaffected_cache_preserved": other_preserved,
                "cache_stats_after": service.cache.stats_snapshot(),
            },
        )
        post_edit_query = service.query("conv-0001")
        post_edit_hits = [
            {"category": hit["category"], "term": hit["term"]}
            for message in post_edit_query["messages"]
            for hit in message["risk_hits"]
        ]
        _append(
            report,
            "post_edit_query",
            {
                "ok": bool(
                    post_edit_query["query_meta"]["consistency"]["ok"]
                    and any(hit["term"] == "验证码" for hit in post_edit_hits)
                ),
                "risk_level": post_edit_query["risk_level"],
                "risk_score": post_edit_query["risk_score"],
                "risk_hits": post_edit_hits,
                "message_2_text": post_edit_query["messages"][1]["text"],
                "cache_hit": post_edit_query["query_meta"]["cache_hit"],
            },
        )

        weight_commit = service.edit_weight("conv-0002", 3, 1.75)
        weight_query = service.query("conv-0002")
        _append(
            report,
            "edit_weight",
            {
                "ok": bool(
                    weight_commit["versions"]["weights"] > 1
                    and weight_query["messages"][2]["weight"] == 1.75
                ),
                "commit": weight_commit,
                "message_3_weight": weight_query["messages"][2]["weight"],
            },
        )

        split = service.split_conversation("conv-0003", 3, "conv-0003b")
        split_batch = service.run_batch()
        split_maps = {
            item["conv_id"]: [
                message["msg_no"] for message in item["messages"]
            ]
            for item in split_batch["results"]
            if item["conv_id"] in {"conv-0003", "conv-0003b"}
        }
        merge = service.merge_conversations("conv-0003", "conv-0003b")
        merged_batch = service.run_batch()
        merged_record = next(
            item for item in merged_batch["results"] if item["conv_id"] == "conv-0003"
        )
        _append(
            report,
            "split_and_merge",
            {
                "ok": bool(
                    sorted(split_maps) == ["conv-0003", "conv-0003b"]
                    and merge["affected_conversations"]
                    and [
                        message["msg_no"] for message in merged_record["messages"]
                    ]
                    == list(range(1, len(merged_record["messages"]) + 1))
                ),
                "split_commit": split,
                "split_msg_nos": split_maps,
                "merge_commit": merge,
                "merged_msg_nos": [
                    message["msg_no"] for message in merged_record["messages"]
                ],
            },
        )

        latency = _capture(
            lambda: service.query(
                "conv-0001", budget_ms=20.0, inject_delay_ms=30.0
            )
        )
        report["anomalies"].append({"name": "latency_budget", **latency})

        normalized = service.query("conv-0001")["messages"][1]["normalized_text"]
        report["anomalies"].append(
            {
                "name": "risk_span_category",
                **_capture(
                    lambda: service.validate_span(normalized, "not-a-category", 0, 1)
                ),
            }
        )
        report["anomalies"].append(
            {
                "name": "risk_span_out_of_range",
                **_capture(
                    lambda: service.validate_span(
                        normalized, "fraud", 0, len(normalized) + 10
                    )
                ),
            }
        )

        tampered_aggregate = json.loads(json.dumps(service.query("conv-0001")))
        tampered_aggregate["query_meta"] = {}
        tampered_aggregate["risk_score"] = min(
            1.0, tampered_aggregate["risk_score"] + 0.25
        )
        report["anomalies"].append(
            {
                "name": "aggregation_mismatch",
                **_capture(
                    lambda: verify_aggregation(
                        tampered_aggregate,
                        service._snapshot.config,
                        service._snapshot.risk_words,
                    )
                ),
            }
        )

        expected = json.loads(
            json.dumps(service._snapshot.results["conv-0001"])
        )
        actual = json.loads(json.dumps(expected))
        actual["risk_level"] = "critical"
        report["anomalies"].append(
            {
                "name": "query_batch_inconsistent",
                **_capture(
                    lambda: compare_results(
                        expected,
                        actual,
                        int(service._snapshot.config["precision"]["decimals"]),
                    )
                ),
            }
        )

        with service.commit_gate.hold_for_test("demo_hold"):
            busy = _capture(
                lambda: service.edit_text("conv-0001", 1, "should not commit")
            )
        report["anomalies"].append({"name": "stage_busy", **busy})

        limit_config = json.loads(json.dumps(service._snapshot.config))
        limit_config["resource_limits"]["max_conversations"] = 1
        report["anomalies"].append(
            {
                "name": "resource_limit",
                **_capture(lambda: service.update_config(limit_config)),
            }
        )
        report["anomalies"].append(
            {
                "name": "snapshot_rollback",
                **_capture(lambda: service.verify_snapshot_file(old_snapshot)),
            }
        )

        corrupt = Path(temporary) / "corrupt_snapshot.json"
        payload = json.loads(service.store.path.read_text(encoding="utf-8"))
        payload["results"]["conv-0001"]["risk_level"] = "critical"
        corrupt.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        report["anomalies"].append(
            {
                "name": "snapshot_corrupt",
                **_capture(lambda: SnapshotStore(corrupt).load()),
            }
        )

        check = service.self_check()
        _append(report, "self_check", check)
        report["ok"] = bool(
            all(step.get("ok", True) for step in report["steps"])
            and all(anomaly.get("raised", False) for anomaly in report["anomalies"])
        )
        report["runtime_temporary"] = True
        report["final_snapshot"] = service.snapshot_info()
    return report

