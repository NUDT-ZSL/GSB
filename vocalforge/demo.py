"""Repeatable end-to-end demonstration and validation report."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .embedding import compute_embedding_drift, replaced_frame_guard
from .errorprop import validate_error_spectrum
from .service import LabService
from .util import to_jsonable


def _verification_flags(service: LabService) -> dict[str, bool]:
    alignment_digests = {
        profile: service._profiles[profile].alignment_digest
        for profile in ("float64", "float32", "fixed")
    }
    decisions_by_utterance: dict[str, set[bool]] = {}
    for profile in ("float64", "float32", "fixed"):
        payload = service._similarities[profile]
        for row in payload["rows"]:
            decisions_by_utterance.setdefault(row["utterance_id"], set()).add(
                bool(row["accepted"])
            )
    topk_signatures = {
        profile: tuple(row["utterance_id"] for row in payload["top_k"])
        for profile, payload in service._similarities.items()
    }
    return {
        "alignment_labels_invariant": len(set(alignment_digests.values())) == 1,
        "threshold_decisions_invariant": all(
            len(values) == 1 for values in decisions_by_utterance.values()
        ),
        "topk_invariant": len(set(topk_signatures.values())) == 1,
        "snapshot_verified": bool(service.snapshots.verify().get("ok")),
    }


def run_demo(root: str | Path) -> dict[str, Any]:
    root = Path(root).resolve()
    service = LabService(root)
    initial_snapshot = service.snapshot_state()
    initial_consistency = service.run_consistency()
    initial_embeddings = service.registry.all_summaries()
    old_summary_bytes = {
        speaker_id: summary.summary_bytes
        for speaker_id, summary in initial_embeddings.items()
    }
    alignment_digests = {
        profile: service._profiles[profile].alignment_digest
        for profile in ("float64", "float32", "fixed")
    }
    spectra_validation = {
        profile: validate_error_spectrum(service._spectra[profile])
        for profile in ("float64", "float32", "fixed")
    }
    initial_flags = _verification_flags(service)

    registration = service.append_registration(speaker_id=1)
    after_embeddings = service.registry.all_summaries()
    drift = registration["registration"]["drifts"][0]
    old = initial_embeddings[1]
    new = after_embeddings[1]
    recomputed_drift = compute_embedding_drift(
        old.vector,
        old.normalized,
        new.vector,
        new.normalized,
        old.reference_versions,
        new.reference_versions,
    )
    unchanged_bytes = {
        speaker_id: old_summary_bytes[speaker_id]
        == after_embeddings[speaker_id].summary_bytes
        for speaker_id in (2, 3, 4)
    }
    guard = replaced_frame_guard(
        service.last_registration,
        tuple(range(service.dataset.total_frames)),
    )
    consistency_after_registration = service.run_consistency()
    fixed_spectrum_validation = validate_error_spectrum(
        service.error_spectrum("fixed")
    )
    service.switch_profile("fixed")
    fixed_active_state = {
        "active_profile": service.config.profile,
        "alignment_invariant": service.precision_payload()["labels_invariant"],
        "top_k": [
            row["utterance_id"]
            for row in service.similarity_payload()["top_k"]
        ],
    }

    injections: dict[str, Any] = {}
    for stage in (
        "sample_rate",
        "seed_shape",
        "ref_missing",
        "duplicate_utterance",
        "phoneme_out_of_range",
    ):
        result = service.inject_error(stage)
        injections[stage] = {
            "ok": result["ok"],
            "code": result["error"]["code"],
            "field": result["error"].get("field"),
            "degradation": result["degradation"],
            "active_state_preserved": result["active_state_preserved"],
        }
    rollback = service.attempt_snapshot_rollback(1)
    final_consistency = service.run_consistency()
    final_flags = _verification_flags(service)

    with tempfile.TemporaryDirectory(prefix="vocalforge-determinism-a-") as temp_a, \
            tempfile.TemporaryDirectory(prefix="vocalforge-determinism-b-") as temp_b:
        twin_a = LabService(temp_a)
        twin_b = LabService(temp_b)
        deterministic = {
            "dataset_digest": twin_a.dataset_summary["dataset_digest"]
            == twin_b.dataset_summary["dataset_digest"],
            "float64_feature_digest": twin_a._profiles["float64"].feature_digest
            == twin_b._profiles["float64"].feature_digest,
            "alignment_digest": twin_a._profiles["float64"].alignment_digest
            == twin_b._profiles["float64"].alignment_digest,
            "adaptive_decision_digest": twin_a.adaptive["decision_digest"]
            == twin_b.adaptive["decision_digest"],
            "fixed_spectrum_digest": twin_a._spectra["fixed"]["spectrum_digest"]
            == twin_b._spectra["fixed"]["spectrum_digest"],
        }

    similarity_rows = service.similarity_payload()["rows"]
    accepted = [
        row["utterance_id"] for row in similarity_rows if row["accepted"]
    ]
    rejected = [
        row["utterance_id"] for row in similarity_rows if not row["accepted"]
    ]
    report = {
        "ok": all(
            [
                initial_consistency["ok"],
                consistency_after_registration["ok"],
                final_consistency["ok"],
                all(initial_flags.values()),
                all(final_flags.values()),
                all(deterministic.values()),
                all(unchanged_bytes.values()),
                guard["disjoint"] and guard["covers_all"],
                drift["recompute_digest"] == recomputed_drift["recompute_digest"],
                fixed_spectrum_validation["ok"],
                rollback["error"]["code"] == "E_SNAPSHOT_ROLLBACK",
                all(item["ok"] is False for item in injections.values()),
            ]
        ),
        "environment": {
            "sample_rate": service.dataset.sample_rate,
            "frame_ms": service.dataset.frame_ms,
            "frame_hop_ms": service.dataset.frame_hop_ms,
            "dataset_seed": service.dataset.seed,
            "python_requires": ">=3.11",
            "python_actual": __import__("sys").version.split()[0],
            "numpy": __import__("numpy").__version__,
        },
        "dataset": {
            "dataset_digest": service.dataset_summary["dataset_digest"],
            "speakers": list(service.dataset.speakers),
            "utterances": len(service.dataset.utterances),
            "frames": service.dataset.total_frames,
            "controlled_phonemes": list(service.dataset.controlled_phonemes),
        },
        "snapshot": {
            "initial": initial_snapshot,
            "final": service.snapshot_state(),
            "rollback_attempt": rollback,
        },
        "reference_drift": {
            "changed_speakers": registration["registration"]["changed_speakers"],
            "unchanged_speakers": registration["registration"]["unchanged_speakers"],
            "drift": drift,
            "independently_recomputed": recomputed_drift,
            "recompute_match": drift["recompute_digest"]
            == recomputed_drift["recompute_digest"],
            "unchanged_summary_bytes_stable": unchanged_bytes,
            "affected_intervals": registration["registration"]["affected_intervals"],
            "recompute_guard": guard,
        },
        "precision": {
            "alignment_digests": alignment_digests,
            "fixed_active_state": fixed_active_state,
            "flags": final_flags,
            "error_spectra_validation": spectra_validation,
            "fixed_spectrum_validation": fixed_spectrum_validation,
            "dominant_sources": {
                profile: service._spectra[profile]["dominant_source"]
                for profile in ("float64", "float32", "fixed")
            },
        },
        "similarity": {
            "config": service.config.to_dict(),
            "accepted": accepted,
            "rejected": rejected,
            "rows": similarity_rows,
            "top_k": service.similarity_payload()["top_k"],
        },
        "adaptive": service.adaptive,
        "path_consistency": service._path_compare,
        "injections": injections,
        "consistency": {
            "initial": initial_consistency,
            "after_registration": consistency_after_registration,
            "final": final_consistency,
        },
        "determinism": deterministic,
    }
    report = to_jsonable(report)
    report_path = root / "artifacts" / "reports" / "demo_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report["report_paths"] = {
        "json": str(report_path),
        "markdown": str(report_path.parent / "demo_report.md"),
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown = _markdown_report(report)
    (report_path.parent / "demo_report.md").write_text(markdown, encoding="utf-8")
    return report


def _markdown_report(report: dict[str, Any]) -> str:
    drift = report["reference_drift"]
    path = report["path_consistency"]
    precision = report["precision"]
    lines = [
        "# VocalForge Local 验证报告",
        "",
        f"- 总体验证：**{'PASS' if report['ok'] else 'FAIL'}**",
        f"- 数据集：{report['dataset']['speakers']} speakers / "
        f"{report['dataset']['utterances']} utterances / {report['dataset']['frames']} frames",
        f"- 数据集摘要：`{report['dataset']['dataset_digest']}`",
        f"- 最终快照：v{report['snapshot']['final']['current']['version']} / "
        f"`{report['snapshot']['final']['current']['checksum']}`",
        "",
        "## 参考音色漂移",
        "",
        f"- 变化说话人：{drift['changed_speakers']}",
        f"- 未变化说话人：{drift['unchanged_speakers']}",
        f"- 归一化 L2 漂移：{drift['drift']['drift_normalized_l2']}",
        f"- 独立复算一致：{drift['recompute_match']}",
        f"- 未变化摘要字节稳定：{drift['unchanged_summary_bytes_stable']}",
        f"- 重算/复用集合：{drift['recompute_guard']}",
        "",
        "## 精度与误差传播",
        "",
        f"- 对齐摘要：`{precision['alignment_digests']}`",
        f"- 主导来源：{precision['dominant_sources']}",
        f"- 定点谱复核：{precision['fixed_spectrum_validation']}",
        "",
        "## 路径一致性",
        "",
        f"- 相似度一致率：{path['similarity_agreement_rate']}",
        f"- 门限判定一致率：{path['threshold_decision_agreement_rate']}",
        f"- 对齐失配率：{path['alignment_offset']['mismatch_rate']}",
        f"- 置信偏差谱：{path['confidence_deviation_spectrum']}",
        "",
        "## 注入与降级",
        "",
    ]
    for stage, result in report["injections"].items():
        lines.append(
            f"- `{stage}` → `{result['code']}` / {result['degradation']['category']} / state preserved={result['active_state_preserved']}"
        )
    lines.extend(
        [
            "",
            "## 自检",
            "",
            f"- initial：{report['consistency']['initial']['ok']}",
            f"- after registration：{report['consistency']['after_registration']['ok']}",
            f"- final：{report['consistency']['final']['ok']}",
            f"- determinism：{report['determinism']}",
            "",
        ]
    )
    return "\n".join(lines)


def print_demo_summary(report: dict[str, Any]) -> None:
    print("=" * 70)
    print("VocalForge Local · deterministic validation")
    print("=" * 70)
    print(f"overall:               {'PASS' if report['ok'] else 'FAIL'}")
    print(f"dataset:               {report['dataset']['speakers']} speakers, "
          f"{report['dataset']['utterances']} utterances, {report['dataset']['frames']} frames")
    print(f"alignment invariant:   {report['precision']['flags']['alignment_labels_invariant']}")
    print(f"dominant fixed source: {report['precision']['dominant_sources']['fixed']}")
    drift = report["reference_drift"]
    print(f"drift S{drift['changed_speakers'][0]}:           "
          f"{drift['drift']['drift_normalized_l2']} (recompute={drift['recompute_match']})")
    print(f"path agreement:        {report['path_consistency']['similarity_agreement_rate']}")
    print(f"snapshot:              v{report['snapshot']['final']['current']['version']} verified="
          f"{report['snapshot']['final']['verification']['ok']}")
    print("injections:            " + ", ".join(
        f"{stage}={item['code']}" for stage, item in report["injections"].items()
    ))
    print(f"report:                {report['report_paths']['json']}")
    print("=" * 70)
