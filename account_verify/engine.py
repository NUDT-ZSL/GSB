from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .adjudication import (
    evaluate_all_tokens,
    evaluate_user,
    evaluate_token,
    group_credentials,
)
from .atomicio import atomic_write_json, ensure_writable_directory
from .checkpoint import CheckpointStore
from .codes import E_PARAM, E_TOKEN_NOT_FOUND, OK
from .digests import file_sha256
from .errors import CrashInjected, VerificationError
from .ledger import Ledger
from .locks import FileWriteLock
from .parsing import ParseBatch, parse_sample_sources
from .policy import PolicyView, compute_closure


class Engine:
    def __init__(self, state_dir: Path, *, lock_timeout_seconds: float = 0.0) -> None:
        self.state_dir = ensure_writable_directory(Path(state_dir))
        self.lock = FileWriteLock(self.state_dir / "write.lock", timeout_seconds=lock_timeout_seconds)
        self.lock.acquire()
        try:
            self.ledger = Ledger(self.state_dir / "ledger.sqlite3")
            self.checkpoints = CheckpointStore(self.state_dir)
            self.recovery = self.checkpoints.recover(self.ledger)
            self.bundle_path = self.state_dir / "imported_bundle.json"
            self.bundle = self._load_bundle()
        except Exception:
            self.lock.release()
            raise

    def close(self) -> None:
        try:
            self.ledger.close()
        finally:
            self.lock.release()

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _load_bundle(self) -> Optional[Dict[str, Any]]:
        if not self.bundle_path.exists():
            return None
        with open(self.bundle_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def require_bundle(self) -> Dict[str, Any]:
        if self.bundle is None:
            raise VerificationError(
                "no imported bundle; run import first",
                code=E_PARAM,
                details={"bundle_path": str(self.bundle_path)},
            )
        return self.bundle

    def observed_clock_ms(self) -> int:
        return int(self.require_bundle()["observed_clock_ms"])

    def policy_view(self, bundle: Optional[Dict[str, Any]] = None) -> PolicyView:
        bundle = bundle or self.require_bundle()
        return PolicyView.from_batches(
            bundle.get("policy_v1", []),
            bundle.get("policy_v2", []),
            int(bundle["observed_clock_ms"]),
        )

    def credentials_by_user(self, bundle: Optional[Dict[str, Any]] = None):
        bundle = bundle or self.require_bundle()
        return group_credentials(bundle.get("credentials", []))

    def user_ids(self, bundle: Optional[Dict[str, Any]] = None) -> List[str]:
        bundle = bundle or self.require_bundle()
        ids = {item["user_id"] for item in bundle.get("credentials", [])}
        ids.update(item["user_id"] for item in bundle.get("users", []) if item.get("user_id"))
        return sorted(ids)

    def tokens_by_id(self, bundle: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
        bundle = bundle or self.require_bundle()
        return {item["token_id"]: item for item in bundle.get("tokens", [])}

    @staticmethod
    def discover_sample_paths(samples_dir: Path) -> Dict[str, Path]:
        samples_dir = Path(samples_dir)
        return {
            "credentials_path": samples_dir / "credentials.json",
            "tokens_path": samples_dir / "tokens.json",
            "policy_v1_path": samples_dir / "policy_v1.json",
            "policy_v2_db_path": samples_dir / "policy_v2_meta.sqlite",
        }

    def import_samples(self, samples_dir: Path) -> Dict[str, Any]:
        samples_dir = Path(samples_dir)
        manifest_path = samples_dir / "manifest.json"
        if not manifest_path.exists():
            raise VerificationError(
                "sample manifest.json is required and declares the observation clock",
                code=E_PARAM,
                details={"path": str(manifest_path)},
            )
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        if not isinstance(manifest.get("observed_clock_ms"), int) or isinstance(manifest["observed_clock_ms"], bool):
            raise VerificationError("manifest.observed_clock_ms must be an integer", code=E_PARAM)

        batch = parse_sample_sources(**self.discover_sample_paths(samples_dir))
        view = PolicyView.from_batches(batch.policy_v1, batch.policy_v2, manifest["observed_clock_ms"])
        bundle = {
            "schema": "account-verify/imported-bundle/v1",
            "samples_dir": str(samples_dir.resolve()),
            "manifest": manifest,
            "observed_clock_ms": manifest["observed_clock_ms"],
            "users": batch.users,
            "credentials": batch.credentials,
            "tokens": batch.tokens,
            "policy_v1": batch.policy_v1,
            "policy_v2": batch.policy_v2,
            "rejections": batch.rejections,
            "source_hashes": batch.source_hashes,
            "policy_fingerprint": view.fingerprint,
        }

        self.ledger.begin()
        try:
            self.ledger.replace_rejections(batch.rejections)
            self.ledger.meta_set("observed_clock_ms", manifest["observed_clock_ms"])
            self.ledger.meta_set("policy_fingerprint", view.fingerprint)
            self.ledger.meta_set("last_import", str(self.bundle_path))
            self.ledger.commit()
        except Exception:
            self.ledger.rollback()
            raise

        atomic_write_json(self.bundle_path, bundle, required_keys=["schema", "observed_clock_ms", "credentials"])
        self.bundle = bundle
        return {
            "imported": {
                "users": len(batch.users),
                "credentials": len(batch.credentials),
                "tokens": len(batch.tokens),
                "policy_v1": len(batch.policy_v1),
                "policy_v2": len(batch.policy_v2),
                "rejections": len(batch.rejections),
            },
            "rejections": batch.rejections,
            "policy_fingerprint": view.fingerprint,
            "bundle_path": str(self.bundle_path),
        }

    def _write_checkpoint_snapshot(self, checkpoint_row: Dict[str, Any], detail: Dict[str, Any]) -> None:
        payload = self.checkpoints.snapshot_for_checkpoint(checkpoint_row, detail)
        self.checkpoints.write_snapshot(payload)

    def adjudicate(
        self,
        *,
        batch_size: int = 3,
        crash_after_batch: Optional[int] = None,
        crash_phase: str = "before_snapshot",
    ) -> Dict[str, Any]:
        bundle = self.require_bundle()
        if not isinstance(batch_size, int) or batch_size <= 0:
            raise VerificationError("batch_size must be a positive integer", code=E_PARAM)

        view = self.policy_view(bundle)
        creds = self.credentials_by_user(bundle)
        user_ids = self.user_ids(bundle)
        tokens = bundle.get("tokens", [])
        token_ids = [token["token_id"] for token in sorted(tokens, key=lambda x: x["token_id"])]
        work_items = [("user", user_id) for user_id in user_ids]
        work_items.extend(("token", token_id) for token_id in token_ids)

        last = self.ledger.latest_checkpoint()
        resume_from = int(last["batch_watermark"]) if last else int(self.recovery.get("resumed_from_batch_watermark") or 0)
        if resume_from > len(work_items):
            resume_from = 0
        processed_batches = 0
        crash_info = None

        for start in range(resume_from, len(work_items), batch_size):
            end = min(start + batch_size, len(work_items))
            batch_items = work_items[start:end]
            processed_batches += 1
            batch_number = start // batch_size + 1

            self.ledger.begin()
            try:
                watermark = self.ledger.next_watermark()
                user_decisions_in_tx: Dict[str, Dict[str, Any]] = {}
                for kind, identifier in batch_items:
                    if kind == "user":
                        decision = evaluate_user(identifier, creds, view)
                        self.ledger.upsert_user(decision, watermark)
                        user_decisions_in_tx[identifier] = decision
                    else:
                        token = self.tokens_by_id(bundle)[identifier]
                        existing = self.ledger.get_token(identifier) or {}
                        user_decision = user_decisions_in_tx.get(token["user_id"])
                        if user_decision is None:
                            stored = self.ledger.list_user_decisions().get(token["user_id"])
                            user_decision = stored or evaluate_user(token["user_id"], creds, view)
                        decision = evaluate_token(
                            identifier,
                            self.tokens_by_id(bundle),
                            creds,
                            {token["user_id"]: user_decision},
                            view,
                            ledger_consumed=bool(existing.get("consumed")),
                            ledger_use_count=int(existing.get("use_count") or 0),
                        )
                        self.ledger.upsert_token(decision, watermark)

                digest = self.ledger.compute_digest()
                detail = {
                    "start_index": start,
                    "end_index": end,
                    "items": [{"kind": kind, "id": identifier} for kind, identifier in batch_items],
                    "policy_fingerprint": view.fingerprint,
                }
                self.ledger.insert_checkpoint(watermark, end, digest, batch_size, detail)
                self.ledger.commit()
            except Exception:
                self.ledger.rollback()
                raise

            checkpoint_row = self.ledger.latest_checkpoint()
            assert checkpoint_row is not None

            if crash_after_batch == batch_number and crash_phase == "before_snapshot":
                raise CrashInjected(
                    "simulated forced termination after SQLite commit and before snapshot replacement",
                )
            if crash_after_batch == batch_number and crash_phase == "temp_residue":
                self.checkpoints.temp_path.write_text('{"broken": ', encoding="utf-8")
                raise CrashInjected("simulated forced termination while writing temporary checkpoint")

            self._write_checkpoint_snapshot(checkpoint_row, json.loads(checkpoint_row["detail_json"]))

            if crash_after_batch == batch_number and crash_phase == "after_snapshot":
                crash_info = {"batch_number": batch_number, "phase": "after_snapshot"}
                raise CrashInjected("simulated forced termination after complete checkpoint")

        final_checkpoint = self.ledger.latest_checkpoint()
        verification = self.ledger.verify_digest(final_checkpoint["ledger_digest"] if final_checkpoint else None)
        return {
            "total_items": len(work_items),
            "user_count": len(user_ids),
            "token_count": len(token_ids),
            "resumed_from_batch_watermark": resume_from,
            "processed_batches": processed_batches,
            "batch_size": batch_size,
            "final_batch_watermark": final_checkpoint["batch_watermark"] if final_checkpoint else 0,
            "watermark": final_checkpoint["watermark"] if final_checkpoint else 0,
            "ledger_digest": verification["ledger_digest"],
            "recovery": self.recovery,
            "crash_info": crash_info,
        }

    def adjudicate_subset(
        self,
        user_ids: List[str],
        token_ids: List[str],
        *,
        closure: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        bundle = self.require_bundle()
        view = self.policy_view(bundle)
        creds = self.credentials_by_user(bundle)
        tokens_by_id = self.tokens_by_id(bundle)
        user_ids = sorted(set(user_ids))
        token_ids = sorted(set(token_ids))

        self.ledger.begin()
        try:
            watermark = self.ledger.next_watermark()
            decisions: Dict[str, Dict[str, Any]] = {}
            for user_id in user_ids:
                decision = evaluate_user(user_id, creds, view)
                decisions[user_id] = decision
                self.ledger.upsert_user(decision, watermark)

            stored_users = self.ledger.list_user_decisions()
            for token_id in token_ids:
                token = tokens_by_id[token_id]
                user_decision = decisions.get(token["user_id"]) or stored_users.get(token["user_id"])
                if user_decision is None:
                    user_decision = evaluate_user(token["user_id"], creds, view)
                existing = self.ledger.get_token(token_id) or {}
                decision = evaluate_token(
                    token_id,
                    tokens_by_id,
                    creds,
                    {token["user_id"]: user_decision},
                    view,
                    ledger_consumed=bool(existing.get("consumed")),
                    ledger_use_count=int(existing.get("use_count") or 0),
                )
                self.ledger.upsert_token(decision, watermark)

            digest = self.ledger.compute_digest()
            last = self.ledger.latest_checkpoint()
            batch_watermark = int(last["batch_watermark"]) if last else len(self.user_ids(bundle)) + len(bundle.get("tokens", []))
            detail = {
                "incremental": True,
                "user_ids": user_ids,
                "token_ids": token_ids,
                "closure": closure,
                "policy_fingerprint": view.fingerprint,
            }
            self.ledger.insert_checkpoint(watermark, batch_watermark, digest, last["batch_size"] if last else 3, detail)
            if closure is not None:
                self.ledger.meta_set("last_closure", json.dumps(closure, ensure_ascii=False, sort_keys=True))
            self.ledger.commit()
        except Exception:
            self.ledger.rollback()
            raise

        row = self.ledger.latest_checkpoint()
        assert row is not None
        self._write_checkpoint_snapshot(row, json.loads(row["detail_json"]))
        return {"user_ids": user_ids, "token_ids": token_ids, "watermark": row["watermark"], "ledger_digest": row["ledger_digest"]}

    def apply_policy_upgrade(self, upgrade_path: Path) -> Dict[str, Any]:
        bundle = self.require_bundle()
        upgrade_path = Path(upgrade_path)
        with open(upgrade_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        validation = ParseBatch()
        from .parsing import parse_policy_v2_shard

        parse_policy_v2_shard(payload, validation, f"policy_upgrade:{upgrade_path.name}")
        if validation.rejections:
            raise VerificationError(
                "policy upgrade failed strict parsing",
                code="E_PARAM",
                details={"rejections": validation.rejections},
            )

        old_view = self.policy_view(bundle)
        upgraded_bundle = dict(bundle)
        upgraded_bundle["policy_v2"] = list(bundle.get("policy_v2", [])) + validation.policy_v2
        new_view = self.policy_view(upgraded_bundle)
        closure = compute_closure(
            old_view,
            new_view,
            self.user_ids(bundle),
            bundle.get("tokens", []),
        )
        closure["upgrade_file"] = str(upgrade_path)
        closure["upgrade_sha256"] = file_sha256(upgrade_path)

        upgraded_bundle["policy_fingerprint"] = new_view.fingerprint
        upgraded_bundle.setdefault("policy_upgrades", []).append(
            {"path": str(upgrade_path), "fingerprint": new_view.fingerprint, "closure": closure}
        )
        atomic_write_json(self.bundle_path, upgraded_bundle, required_keys=["schema", "observed_clock_ms"])
        self.bundle = upgraded_bundle
        result = self.adjudicate_subset(
            closure["rejudge_user_ids"],
            closure["invalidated_token_ids"],
            closure=closure,
        )
        return {"closure": closure, "adjudication": result}

    def consume_token(self, token_id: str) -> Dict[str, Any]:
        bundle = self.require_bundle()
        tokens_by_id = self.tokens_by_id(bundle)
        if token_id not in tokens_by_id:
            return {
                "token_id": token_id,
                "status": "NOT_FOUND",
                "allowed": False,
                "code": E_TOKEN_NOT_FOUND,
            }

        view = self.policy_view(bundle)
        creds = self.credentials_by_user(bundle)
        token = tokens_by_id[token_id]

        self.ledger.begin()
        try:
            existing = self.ledger.get_token(token_id) or {}
            user_decision = evaluate_user(token["user_id"], creds, view)
            decision = evaluate_token(
                token_id,
                tokens_by_id,
                creds,
                {token["user_id"]: user_decision},
                view,
                ledger_consumed=bool(existing.get("consumed")),
                ledger_use_count=int(existing.get("use_count") or 0),
            )
            if not decision.get("allowed"):
                self.ledger.rollback()
                return decision

            current_use_count = int(existing.get("use_count") or token["use_count"] or 0)
            new_use_count = current_use_count + 1
            stored_decision = dict(decision)
            stored_decision["status"] = "USED"
            stored_decision["consumed"] = True
            stored_decision["use_count"] = new_use_count
            stored_decision["used_at_ms"] = view.observed_clock_ms
            watermark = self.ledger.next_watermark()
            self.ledger.upsert_token(stored_decision, watermark, consumed=True, use_count=new_use_count)
            digest = self.ledger.compute_digest()
            last = self.ledger.latest_checkpoint()
            batch_watermark = int(last["batch_watermark"]) if last else len(self.user_ids(bundle)) + len(bundle.get("tokens", []))
            detail = {"token_consumed": token_id, "policy_fingerprint": view.fingerprint}
            self.ledger.insert_checkpoint(watermark, batch_watermark, digest, last["batch_size"] if last else 1, detail)
            self.ledger.commit()
        except Exception:
            self.ledger.rollback()
            raise

        row = self.ledger.latest_checkpoint()
        assert row is not None
        self._write_checkpoint_snapshot(row, json.loads(row["detail_json"]))
        return stored_decision

    def load_closure(self) -> Optional[Dict[str, Any]]:
        raw = self.ledger.meta_get("last_closure")
        return json.loads(raw) if raw else None

    def export_report_data(self, *, input_integrity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        bundle = self.require_bundle()
        view = self.policy_view(bundle)
        creds = self.credentials_by_user(bundle)
        user_ids = self.user_ids(bundle)
        user_decisions = {uid: evaluate_user(uid, creds, view) for uid in user_ids}
        ledger_tokens = self.ledger.list_tokens()
        token_results = evaluate_all_tokens(
            bundle.get("tokens", []),
            self.tokens_by_id(bundle),
            creds,
            user_decisions,
            view,
            ledger_tokens=ledger_tokens,
        )
        # Reflect durable consumption/status even if pure projection sees a reused token.
        for result in token_results:
            row = ledger_tokens.get(result["token_id"])
            if row and row["consumed"]:
                result["ledger_consumed"] = True
                result["ledger_use_count"] = row["use_count"]
                result["status"] = "USED"
                result["code"] = OK
                result["allowed"] = False
                result["durable_state"] = "consumed once; a new use attempt returns E_TOKEN_REUSED"
        checkpoint = self.ledger.latest_checkpoint()
        verification = self.ledger.verify_digest(checkpoint["ledger_digest"] if checkpoint else None)
        return {
            "schema": "account-verify/diagnostic-report/v1",
            "generated_at_ms": view.observed_clock_ms,
            "observed_clock_ms": view.observed_clock_ms,
            "input_integrity": input_integrity or {"checked": False},
            "summary": {
                "user_count": len(user_ids),
                "token_count": len(bundle.get("tokens", [])),
                "rejection_count": len(bundle.get("rejections", [])),
                "policy_conflict_count": len(view.conflict_report()),
                "checkpoint_watermark": checkpoint["watermark"] if checkpoint else 0,
                "checkpoint_batch_watermark": checkpoint["batch_watermark"] if checkpoint else 0,
            },
            "parse_rejections": bundle.get("rejections", []),
            "policy_merge": {
                "observed_clock_ms": view.observed_clock_ms,
                "v1_conversion": "effective_at_ms = observed_clock_ms + effective_relative_seconds * 1000",
                "fingerprint": view.fingerprint,
                "authoritative": view.authoritative_report(),
                "conflicts": view.conflict_report(),
            },
            "users": [user_decisions[uid] for uid in user_ids],
            "tokens": token_results,
            "failure_closure": self.load_closure(),
            "checkpoint": {
                "watermark": checkpoint["watermark"] if checkpoint else None,
                "batch_watermark": checkpoint["batch_watermark"] if checkpoint else None,
                "ledger_digest": verification["ledger_digest"],
                "snapshot_path": str(self.checkpoints.path),
                "recovery": self.recovery,
            },
        }

    def write_report(self, output_path: Path, *, input_integrity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        report = self.export_report_data(input_integrity=input_integrity)
        atomic_write_json(Path(output_path), report, required_keys=["schema", "users", "tokens", "summary"])
        return report
