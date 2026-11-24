"""High-level deterministic TSA audit engine."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Iterable

from .adjudication import adjudicate, artifact_key, propagation_closure
from .canon import canonical_text, sha256_obj
from .codes import Code
from .codes import REPORT_FORMAT
from .errors import AuditError
from .parsing import (
    load_manifest,
    parse_signatures_file,
    parse_tokens_file,
)
from .store import LedgerStore, compute_ledger_digest
from .trust import build_trust_view
from .trust_parse import load_v2_snapshot, parse_trust_update_file, parse_v1_snapshot


class AuditEngine:
    def __init__(self, workspace: str | Path = "state") -> None:
        self.workspace = Path(workspace)
        self.store = LedgerStore(self.workspace)

    def _sample_paths(self, samples: Path) -> dict[str, Path]:
        return {
            "manifest": samples / "manifest.json",
            "signatures": samples / "signatures.json",
            "tokens": samples / "tokens.json",
            "v1": samples / "snapshots" / "v1" / "trust_v1.json",
            "v2": samples / "snapshots" / "v2",
        }

    def load_sample_bundle(self, samples: str | Path) -> dict[str, Any]:
        sample_dir = Path(samples)
        paths = self._sample_paths(sample_dir)
        manifest, manifest_errors = load_manifest(paths["manifest"])
        if manifest is None:
            clock = 0
            algorithm = "sha256"
            files: dict[str, Any] = {}
        else:
            clock = int(manifest["observation_clock_ms"])
            algorithm = str(manifest.get("digest_algorithm", "sha256"))
            files = manifest.get("files", {}) if isinstance(manifest.get("files"), dict) else {}

        def configured_path(key: str, default: Path) -> Path:
            value = files.get(key)
            return sample_dir / value if isinstance(value, str) else default

        signature_path = configured_path("signatures", paths["signatures"])
        token_path = configured_path("tokens", paths["tokens"])
        v1_path = configured_path("v1", paths["v1"])
        v2_path = configured_path("v2", paths["v2"])

        artifacts, sig_errors = parse_signatures_file(signature_path, default_algorithm=algorithm)
        tokens, token_errors = parse_tokens_file(token_path, default_algorithm=algorithm)
        snapshots: list[dict[str, Any]] = []
        trust_records: list[dict[str, Any]] = []
        trust_errors: list[dict[str, Any]] = []
        if v1_path.is_file():
            snapshot, records, errors = parse_v1_snapshot(v1_path, clock)
            trust_errors.extend(errors)
            if snapshot:
                snapshots.append(snapshot)
                trust_records.extend(records)
        if v2_path.is_dir():
            snapshot, records, errors = load_v2_snapshot(v2_path)
            trust_errors.extend(errors)
            if snapshot:
                snapshots.append(snapshot)
                trust_records.extend(records)

        return {
            "manifest": manifest or {"observation_clock_ms": clock, "digest_algorithm": algorithm},
            "artifacts": artifacts,
            "tokens": tokens,
            "snapshots": snapshots,
            "trust_records": trust_records,
            "rejections": manifest_errors + sig_errors + token_errors + trust_errors,
            "source_paths": [
                str(paths["manifest"]), str(signature_path), str(token_path),
                str(v1_path), str(v2_path),
            ],
            "observation_clock_ms": clock,
        }

    def import_sample(self, samples: str | Path, *, arrival_order: tuple[str, str] = ("v1", "v2")) -> dict[str, Any]:
        """Import records and trust snapshots in a deterministic arrival order."""
        bundle = self.load_sample_bundle(samples)
        clock = bundle["observation_clock_ms"]
        record_bundle = {
            "artifacts": bundle["artifacts"],
            "tokens": bundle["tokens"],
            "snapshots": [],
            "trust_records": [],
            "rejections": [r for r in bundle["rejections"] if str(r.get("source_category", "")).startswith(("signature", "timestamp", "manifest"))],
            "source_paths": bundle["source_paths"][:3],
            "observation_clock_ms": clock,
        }
        record_report = self.process_bundle(
            record_bundle,
            batch_id=f"records-{sha256_obj({'a': bundle['artifacts'], 't': bundle['tokens']})[:14]}",
            kind="records",
        )

        final_report = record_report
        trust_by_format = {s["source_format"]: (s, [r for r in bundle["trust_records"] if r["snapshot_id"] == s["snapshot_id"]])
                           for s in bundle["snapshots"]}
        for format_name in arrival_order:
            key = "v2" if format_name == "v2" else "v1"
            pair = trust_by_format.get(key)
            if pair is None:
                continue
            snapshot, records = pair
            trust_bundle = {
                "artifacts": [], "tokens": [], "snapshots": [snapshot],
                "trust_records": records,
                "rejections": [r for r in bundle["rejections"]
                               if str(r.get("source_category", "")).startswith("trust")],
                "source_paths": [snapshot.get("source_file")],
                "observation_clock_ms": clock,
            }
            final_report = self.process_bundle(
                trust_bundle,
                batch_id=f"trust-{snapshot['snapshot_id']}-{sha256_obj(snapshot)[:12]}",
                kind="trust_snapshot",
            )
        return final_report

    def adjudicate_sample(
        self,
        samples: str | Path,
        *,
        arrival_order: tuple[str, str] = ("v1", "v2"),
    ) -> dict[str, Any]:
        return self.import_sample(samples, arrival_order=arrival_order)

    def import_trust_sample(
        self,
        samples: str | Path,
        *,
        arrival_order: tuple[str, str] = ("v1", "v2"),
    ) -> dict[str, Any]:
        bundle = self.load_sample_bundle(samples)
        clock = bundle["observation_clock_ms"]
        final_report: dict[str, Any] | None = None
        by_format = {
            snapshot["source_format"]: (
                snapshot,
                [r for r in bundle["trust_records"] if r["snapshot_id"] == snapshot["snapshot_id"]],
            )
            for snapshot in bundle["snapshots"]
        }
        for format_name in arrival_order:
            key = "v2" if format_name == "v2" else "v1"
            pair = by_format.get(key)
            if pair is None:
                continue
            snapshot, records = pair
            final_report = self.process_bundle(
                {
                    "artifacts": [],
                    "tokens": [],
                    "snapshots": [snapshot],
                    "trust_records": records,
                    "rejections": [
                        r for r in bundle["rejections"]
                        if str(r.get("source_category", "")).startswith("trust")
                    ],
                    "source_paths": [snapshot.get("source_file")],
                    "observation_clock_ms": clock,
                },
                batch_id=f"trust-{snapshot['snapshot_id']}-{sha256_obj(snapshot)[:12]}",
                kind="trust_snapshot",
            )
        if final_report is None:
            raise AuditError(Code.E_SOURCE_NOT_FOUND, "no v1/v2 trust snapshot found in sample")
        return final_report

    def process_bundle(
        self,
        bundle: dict[str, Any],
        *,
        batch_id: str | None = None,
        kind: str = "mixed",
    ) -> dict[str, Any]:
        with self.store.lock():
            return self._process_bundle_locked(bundle, batch_id=batch_id, kind=kind)

    def _process_bundle_locked(
        self,
        bundle: dict[str, Any],
        *,
        batch_id: str | None,
        kind: str,
    ) -> dict[str, Any]:
        incoming_artifacts = list(bundle.get("artifacts", []))
        incoming_tokens = list(bundle.get("tokens", []))
        incoming_records = list(bundle.get("trust_records", []))
        incoming_snapshots = list(bundle.get("snapshots", []))
        incoming_rejections = list(bundle.get("rejections", []))

        old_artifacts = self.store.load_artifacts()
        old_tokens = self.store.load_tokens()
        old_trust_records = self.store.load_trust_records()
        old_snapshots = self.store.load_snapshots()
        old_verdicts = self.store.load_previous_verdicts_map()

        artifact_ids: dict[str, str] = {a["artifact_id"]: a["signature_digest"] for a in old_artifacts}
        valid_artifacts: list[dict[str, Any]] = []
        for record in incoming_artifacts:
            existing_digest = artifact_ids.get(record["artifact_id"])
            if existing_digest is not None and existing_digest != record["signature_digest"]:
                incoming_rejections.append({
                    "source_category": record.get("source_category", "signature_record"),
                    "source_file": record.get("source_file"),
                    "record_seq": record.get("record_seq", 0),
                    "code": Code.E_DUP_ARTIFACT_ID,
                    "message": "artifact_id already exists with a different signature digest",
                    "details": {"artifact_id": record["artifact_id"]},
                    "sync_marker": record.get("sync_marker"),
                    "sender_time_repr": record.get("sender_time_repr"),
                })
                continue
            artifact_ids[record["artifact_id"]] = record["signature_digest"]
            valid_artifacts.append(record)

        token_ids = {t["token_id"] for t in old_tokens}
        valid_tokens: list[dict[str, Any]] = []
        for token in incoming_tokens:
            if token["token_id"] in token_ids and not any(
                t["token_id"] == token["token_id"] and t == token for t in old_tokens
            ):
                incoming_rejections.append({
                    "source_category": token.get("source_category", "timestamp_token"),
                    "source_file": token.get("source_file"),
                    "record_seq": token.get("record_seq", 0),
                    "code": Code.E_DUP_TOKEN_ID,
                    "message": "token_id already exists with different claim content",
                    "details": {"token_id": token["token_id"]},
                    "sync_marker": token.get("sync_marker"),
                    "sender_time_repr": token.get("sender_time_repr"),
                })
                continue
            token_ids.add(token["token_id"])
            valid_tokens.append(token)

        artifacts_map = {artifact_key(a["artifact_id"], a["signature_digest"]): a for a in old_artifacts}
        for artifact in valid_artifacts:
            artifacts_map[artifact_key(artifact["artifact_id"], artifact["signature_digest"])] = artifact
        tokens_map = {t["token_id"]: t for t in old_tokens}
        for token in valid_tokens:
            tokens_map[token["token_id"]] = token
        trust_map = {r["record_key"]: r for r in old_trust_records}
        for record in incoming_records:
            trust_map[record["record_key"]] = record
        snapshot_map = {s["snapshot_id"]: s for s in old_snapshots}
        for snapshot in incoming_snapshots:
            snapshot_map[snapshot["snapshot_id"]] = snapshot

        all_artifacts = sorted(artifacts_map.values(), key=lambda a: artifact_key(a["artifact_id"], a["signature_digest"]))
        all_tokens = sorted(tokens_map.values(), key=lambda t: t["token_id"])
        all_records = sorted(trust_map.values(), key=lambda r: r["record_key"])
        all_snapshots = sorted(snapshot_map.values(), key=lambda s: s["snapshot_id"])
        clock = bundle.get("observation_clock_ms")
        if clock is None:
            clock = next((s.get("observation_clock_ms") for s in all_snapshots if s.get("observation_clock_ms") is not None), 0)

        old_view = build_trust_view(old_snapshots, old_trust_records, observation_clock_ms=clock)
        new_view = build_trust_view(all_snapshots, all_records, observation_clock_ms=clock)
        old_closure = propagation_closure(old_view, old_tokens, old_artifacts)
        new_closure = propagation_closure(new_view, all_tokens, all_artifacts, previous_view=old_view)

        if batch_id is None:
            batch_id = "batch-" + sha256_obj({
                "kind": kind,
                "artifacts": valid_artifacts,
                "tokens": valid_tokens,
                "records": incoming_records,
                "snapshots": incoming_snapshots,
            })[:18]
        batch_watermark, _batch_seq, is_new_batch = self.store.preview_batch_watermark(batch_id)
        for rejection in incoming_rejections:
            rejection["batch_id"] = batch_id

        candidate_keys = self._affected_keys(
            old_verdicts=old_verdicts,
            valid_artifacts=valid_artifacts,
            valid_tokens=valid_tokens,
            incoming_records=incoming_records,
            all_artifacts=all_artifacts,
            all_tokens=all_tokens,
            old_closure=old_closure,
            new_closure=new_closure,
        )

        report = adjudicate(
            all_artifacts,
            all_tokens,
            new_view,
            decision_watermark="PENDING",
            previous_verdicts=old_verdicts,
            closure=new_closure,
        )
        self._apply_watermarks(report, new_view, int(clock or 0))
        report["reanchor_transitions"] = self._reanchor_transitions(old_verdicts, report)
        changed_keys = self._changed_verdict_keys(report, old_verdicts, candidate_keys)
        report["incremental"] = {
            "candidate_artifact_keys": sorted(candidate_keys),
            "updated_artifact_keys": sorted(changed_keys),
            "only_changed_rows_written": True,
        }
        all_rejections = [
            r for r in self.store.load_rejections()
            if is_new_batch or r.get("batch_id") != batch_id
        ] + incoming_rejections
        report["rejections"] = sorted(
            all_rejections,
            key=lambda r: (str(r.get("source_category")), str(r.get("source_file")),
                           int(r.get("record_seq", 0)), str(r.get("code"))),
        )
        report["summary"]["rejection_count"] = len(report["rejections"])
        report["summary"]["rejected_record_count"] = len({
            (r.get("source_category"), r.get("source_file"), r.get("record_seq"))
            for r in report["rejections"]
        })
        report["batch_id"] = batch_id
        report["batch_watermark"] = batch_watermark

        affected_keys = changed_keys
        expected_digest = compute_ledger_digest(report["artifacts"], batch_watermark)
        batch = {
            "batch_id": batch_id,
            "kind": kind,
            "content_digest": sha256_obj({
                "artifacts": valid_artifacts, "tokens": valid_tokens,
                "trust_records": incoming_records, "snapshots": incoming_snapshots,
                "rejections": incoming_rejections,
            }),
        }
        self._copy_sources(bundle.get("source_paths", []), batch_id)
        payload = self.store.commit_round(
            batch=batch,
            artifacts=all_artifacts,
            tokens=all_tokens,
            trust_records=all_records,
            snapshots=all_snapshots,
            rejections=incoming_rejections,
            report=report,
            affected_keys=sorted(affected_keys),
            expected_ledger_digest=expected_digest,
            trust_view=new_view,
        )
        persisted_report = dict(report)
        persisted_report.update({
            "format": REPORT_FORMAT,
            "report_version": 1,
            "checkpoint_seq": payload["checkpoint_seq"],
            "ledger_digest": payload["ledger_digest"],
            "affected_artifact_keys": payload["affected_keys"],
        })
        self.store.write_report(persisted_report)
        return persisted_report

    def _reanchor_transitions(
        self,
        old_verdicts: dict[str, dict[str, Any]],
        report: dict[str, Any],
    ) -> list[dict[str, Any]]:
        transitions: list[dict[str, Any]] = []
        for result in report["artifacts"]:
            previous = old_verdicts.get(result["artifact_key"])
            if previous is None:
                continue
            changed = (
                previous.get("primary_token_id") != result.get("primary_token_id")
                or bool(previous.get("anchored")) != bool(result.get("anchored"))
            )
            if changed:
                transitions.append({
                    "artifact_key": result["artifact_key"],
                    "artifact_id": result["artifact_id"],
                    "previous_status": previous.get("status"),
                    "new_status": result.get("status"),
                    "previous_primary_token_id": previous.get("primary_token_id"),
                    "new_primary_token_id": result.get("primary_token_id"),
                    "alternative_token_ids": result.get("alternative_token_ids", []),
                })
        return sorted(transitions, key=lambda item: item["artifact_key"])

    def _changed_verdict_keys(
        self,
        report: dict[str, Any],
        old_verdicts: dict[str, dict[str, Any]],
        candidate_keys: set[str],
    ) -> set[str]:
        changed: set[str] = set()
        for result in report["artifacts"]:
            key = result["artifact_key"]
            if key not in candidate_keys:
                continue
            previous = old_verdicts.get(key)
            if previous is None or canonical_text(previous) != canonical_text(result):
                changed.add(key)
        return changed

    def _affected_keys(
        self,
        *,
        old_verdicts: dict[str, dict[str, Any]],
        valid_artifacts: list[dict[str, Any]],
        valid_tokens: list[dict[str, Any]],
        incoming_records: list[dict[str, Any]],
        all_artifacts: list[dict[str, Any]],
        all_tokens: list[dict[str, Any]],
        old_closure: dict[str, Any],
        new_closure: dict[str, Any],
) -> set[str]:
        all_keys = {artifact_key(a["artifact_id"], a["signature_digest"]) for a in all_artifacts}
        if not old_verdicts:
            return set(all_keys)
        if incoming_records:
            return set(all_keys)

        affected: set[str] = {
            artifact_key(a["artifact_id"], a["signature_digest"]) for a in valid_artifacts
        }
        digest_to_keys: dict[str, set[str]] = {}
        for artifact in all_artifacts:
            digest_to_keys.setdefault(artifact["signature_digest"], set()).add(
                artifact_key(artifact["artifact_id"], artifact["signature_digest"])
            )
        for token in valid_tokens:
            affected.update(digest_to_keys.get(token["signature_digest"], set()))

        changed_issuers = {r["issuer_canonical_id"] for r in incoming_records}
        for token in all_tokens:
            if token["issuer_canonical_id"] in changed_issuers:
                affected.update(digest_to_keys.get(token["signature_digest"], set()))

        affected.update(old_closure.get("affected_artifact_keys", []))
        affected.update(new_closure.get("affected_artifact_keys", []))
        return {key for key in affected if key in all_keys}

    def _apply_watermarks(self, report: dict[str, Any], view: dict[str, Any], clock: int) -> None:
        snapshots = {s["snapshot_id"]: s for s in view.get("effective_snapshots", [])}
        for result in report["artifacts"]:
            basis = result.get("basis_generation")
            effective_ms: int | None = None
            if result.get("primary_token_id"):
                candidate = next(
                    (c for c in result["candidate_tokens"] if c["token_id"] == result["primary_token_id"]),
                    None,
                )
                if candidate:
                    basis = candidate.get("basis_generation")
                    snapshot = snapshots.get(candidate.get("effective_snapshot_id"))
                    if snapshot:
                        effective_ms = int(snapshot["effective_at_ms"])
            if effective_ms is None:
                basis_values = [
                    c.get("basis_generation") for c in result["candidate_tokens"]
                    if c.get("basis_generation") is not None
                ]
                if basis_values:
                    basis = max(basis_values)
                    candidate = max(
                        (c for c in result["candidate_tokens"] if c.get("basis_generation") is not None),
                        key=lambda c: (int(c["basis_generation"]), int(c["issued_at_ms"])),
                    )
                    snapshot = snapshots.get(candidate.get("effective_snapshot_id"))
                    effective_ms = int(snapshot["effective_at_ms"]) if snapshot else None
            if effective_ms is None:
                effective_ms = clock
            basis_value = int(basis) if basis is not None else 0
            result["basis_generation"] = basis if basis is not None else result.get("basis_generation")
            result["decision_watermark_ms"] = effective_ms
            result["decision_watermark"] = f"tsa-watermark:v1:{effective_ms}:g{basis_value}"

    def _copy_sources(self, paths: Iterable[str | None], batch_id: str) -> None:
        target_dir = self.store.inputs_dir / batch_id
        for raw_path in paths:
            if not raw_path:
                continue
            source = Path(raw_path)
            if not source.exists():
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / source.name
            try:
                if source.is_dir():
                    if target.exists():
                        shutil.rmtree(target)
                    shutil.copytree(source, target)
                elif not target.exists():
                    shutil.copy2(source, target)
            except OSError:
                # Source copies are recovery aids; the SQLite normalized records
                # remain the authoritative replay source.
                continue

    def import_signatures(self, path: str | Path, *, batch_id: str | None = None) -> dict[str, Any]:
        artifacts, rejections = parse_signatures_file(path)
        return self.process_bundle(
            {"artifacts": artifacts, "rejections": rejections, "source_paths": [str(path)]},
            batch_id=batch_id, kind="signature_batch",
        )

    def import_tokens(self, path: str | Path, *, batch_id: str | None = None) -> dict[str, Any]:
        tokens, rejections = parse_tokens_file(path)
        return self.process_bundle(
            {"tokens": tokens, "rejections": rejections, "source_paths": [str(path)]},
            batch_id=batch_id, kind="token_batch",
        )

    def import_v1(self, path: str | Path, observation_clock_ms: int, *, batch_id: str | None = None) -> dict[str, Any]:
        snapshot, records, errors = parse_v1_snapshot(path, observation_clock_ms)
        return self.process_bundle(
            {"snapshots": [snapshot] if snapshot else [], "trust_records": records,
             "rejections": errors, "source_paths": [str(path)],
             "observation_clock_ms": observation_clock_ms},
            batch_id=batch_id, kind="trust_snapshot_v1",
        )

    def import_v2(self, directory: str | Path, *, batch_id: str | None = None) -> dict[str, Any]:
        snapshot, records, errors = load_v2_snapshot(directory)
        return self.process_bundle(
            {"snapshots": [snapshot] if snapshot else [], "trust_records": records,
             "rejections": errors, "source_paths": [str(directory)]},
            batch_id=batch_id, kind="trust_snapshot_v2",
        )

    def import_update(self, path: str | Path, *, batch_id: str | None = None) -> dict[str, Any]:
        snapshot, records, errors = parse_trust_update_file(path)
        return self.process_bundle(
            {"snapshots": [snapshot] if snapshot else [], "trust_records": records,
             "rejections": errors, "source_paths": [str(path)]},
            batch_id=batch_id, kind="trust_update",
        )

    def anchor(self) -> dict[str, Any]:
        return self.process_bundle(
            {"batch_id_explicit": None},
            batch_id=f"anchor-{sha256_obj({'a': self.store.load_artifacts(), 't': self.store.load_tokens(), 'r': self.store.load_trust_records()})[:16]}",
            kind="anchor_round",
        )

    def get_report(self) -> dict[str, Any]:
        report = self.store.read_report()
        if report is not None:
            return report
        checkpoints = self.store.valid_db_checkpoints()
        if checkpoints:
            return checkpoints[0][1]["report"]
        raise AuditError(Code.E_NOT_FOUND, "no audit report has been produced")

    def export_report(self, output: str | Path | None = None) -> dict[str, Any]:
        report = self.get_report()
        if output is not None:
            from .io_utils import atomic_write_json
            atomic_write_json(output, report)
        return report

    def verify_checkpoint(self, *, recover: bool = False) -> dict[str, Any]:
        with self.store.lock():
            if self.store.rebuild_required:
                if not recover:
                    return {
                        "status": "version_rebuild_required",
                        "recovered": False,
                        "ledger_consistent": False,
                        "code": Code.E_VERSION_REBUILD,
                    }
                return self._full_rebuild_locked("version_change")

            candidates = dict(self.store.valid_db_checkpoints())
            external = self.store.external_checkpoint()
            external_seq = external[0] if external else None
            if external:
                candidates.setdefault(external[0], external[1])
            if not candidates:
                rows = self.store.load_verdict_results()
                if rows:
                    if not recover:
                        return {"status": "residue_without_checkpoint", "recovered": False,
                                "ledger_consistent": False, "code": Code.E_CHECKPOINT}
                    return self._full_rebuild_locked("missing_checkpoint")
                return {"status": "empty", "recovered": False, "ledger_consistent": True,
                        "checkpoint_seq": None}

            seq = max(candidates)
            payload = candidates[seq]
            if payload.get("_version_mismatch"):
                if not recover:
                    return {"status": "version_rebuild_required", "recovered": False,
                            "ledger_consistent": False, "code": Code.E_VERSION_REBUILD,
                            "checkpoint_seq": seq}
                return self._full_rebuild_locked("checkpoint_version_change")

            current_digest, current_watermark = self.store.current_digest()
            consistent = (
                current_digest == payload["ledger_digest"]
                and current_watermark == payload.get("batch_watermark")
                and self.store.columns_match_payload()
            )
            recovered = False
            recovery_actions: list[str] = []
            if not consistent:
                if not recover:
                    return {
                        "status": "ledger_mismatch",
                        "recovered": False,
                        "ledger_consistent": False,
                        "code": Code.E_LEDGER_MISMATCH,
                        "checkpoint_seq": seq,
                        "current_ledger_digest": current_digest,
                        "checkpoint_ledger_digest": payload["ledger_digest"],
                    }
                self.store.restore_payload(payload)
                recovered = True
                recovery_actions.append("restored_ledger_from_complete_checkpoint")

            artifacts_ok = self.store.checkpoint_path.is_file()
            if not artifacts_ok or external_seq != seq:
                if not recover:
                    return {
                        "status": "checkpoint_file_stale",
                        "recovered": False,
                        "ledger_consistent": True,
                        "code": Code.E_CHECKPOINT,
                        "checkpoint_seq": seq,
                        "checkpoint_file_seq": external_seq,
                    }
                self.store.write_checkpoint_artifacts(payload)
                recovery_actions.append("refreshed_atomic_checkpoint_files")

            report = payload.get("report")
            if isinstance(report, dict):
                self.store.write_report(report)

            return {
                "status": "consistent" if not recovered else "recovered",
                "recovered": recovered,
                "recovery_actions": recovery_actions,
                "ledger_consistent": True,
                "checkpoint_seq": seq,
                "batch_watermark": payload.get("batch_watermark"),
                "ledger_digest": payload["ledger_digest"],
                "code": Code.W_CHECKPOINT_RECOVERED if recovered else Code.OK,
            }

    def _full_rebuild_locked(self, reason: str) -> dict[str, Any]:
        self.store.reset_derived_state()
        artifacts = self.store.load_artifacts()
        tokens = self.store.load_tokens()
        records = self.store.load_trust_records()
        snapshots = self.store.load_snapshots()
        clock = next((s.get("observation_clock_ms") for s in snapshots if s.get("observation_clock_ms") is not None), 0)
        bundle = {
            "artifacts": artifacts,
            "tokens": tokens,
            "trust_records": records,
            "snapshots": snapshots,
            "rejections": [],
            "observation_clock_ms": clock,
        }
        report = self._process_bundle_locked(
            bundle,
            batch_id=f"full-rebuild-{sha256_obj({'reason': reason, 'a': artifacts, 't': tokens, 'r': records})[:16]}",
            kind="full_rebuild",
        )
        return {
            "status": "rebuilt",
            "recovered": True,
            "rebuild_reason": reason,
            "ledger_consistent": True,
            "checkpoint_seq": report["checkpoint_seq"],
            "ledger_digest": report["ledger_digest"],
            "report_summary": report["summary"],
        }
