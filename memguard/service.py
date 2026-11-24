"""Application services used by the CLI and tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from .closure import closure_for_credential
from .constants import (
    CheckpointCorrupt,
    CODE_CRED_CYCLE,
    CredentialCycle,
    HalfWriteError,
    UsageError,
)
from .engine import GuardEngine
from .importer import import_inputs, load_bundle, save_bundle
from .locking import ExclusiveFileLock
from .precision import format_mask, mask_names
from .reporting import assert_replay_matches, build_report
from .storage import (
    apply_verdict,
    count_credentials,
    find_temp_residue,
    get_watermark,
    load_credentials,
    open_ledger,
    maybe_crash,
    quarantine_temp_files,
    read_checkpoint,
    reconcile_checkpoint,
    verify_sqlite,
    write_checkpoint,
)


def load_or_import_bundle(args) -> Dict[str, Any]:
    state_dir = Path(args.state_dir)
    bundle = load_bundle(state_dir)
    if bundle is not None:
        return bundle
    bundle = import_inputs(
        getattr(args, "sample_dir", None),
        manifest_path=getattr(args, "manifest", None),
        events_path=getattr(args, "events", None),
        regions_path=getattr(args, "regions", None),
        v1_path=getattr(args, "v1", None),
        v2_manifest_path=getattr(args, "v2_manifest", None),
    )
    save_bundle(bundle, state_dir)
    return bundle


def run_import(args) -> Dict[str, Any]:
    state_dir = Path(args.state_dir)
    bundle = import_inputs(
        args.sample_dir,
        manifest_path=args.manifest,
        events_path=args.events,
        regions_path=args.regions,
        v1_path=args.v1,
        v2_manifest_path=args.v2_manifest,
    )
    state_dir.mkdir(parents=True, exist_ok=True)
    with ExclusiveFileLock(state_dir / "guard.lock"):
        save_bundle(bundle, state_dir)
        report_path = Path(args.report_dir) / "import-report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "schema": "memguard-import-report/v1",
            "counts": bundle["counts"],
            "rejections": bundle["rejections"],
            "policy_conflicts": bundle["policy_view"]["conflicts"],
            "fingerprint": bundle["fingerprint"],
        }
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return {
        "command": "import",
        "bundle_path": str(state_dir / "bundle.json"),
        "import_report_path": str(report_path),
        **report,
    }


def run_disposition(args) -> Dict[str, Any]:
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    bundle = load_or_import_bundle(args)
    force_rebuild = bool(getattr(args, "rebuild", False))

    with ExclusiveFileLock(state_dir / "guard.lock"):
        quarantined = quarantine_temp_files(state_dir)
        conn, rebuild_info = open_ledger(
            state_dir, bundle, force_rebuild=force_rebuild
        )
        try:
            reconcile = reconcile_checkpoint(conn, bundle, state_dir)
            start_watermark = get_watermark(conn)
            watermark = start_watermark
            engine = GuardEngine(bundle)
            processed = []
            skipped = 0
            credential_count = count_credentials(conn)

            for event in bundle["events"]:
                seq = int(event["event_seq"])
                if seq <= start_watermark or seq <= watermark:
                    skipped += 1
                    continue

                verdict = engine.adjudicate(event)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    apply_verdict(conn, verdict)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

                watermark = seq
                processed.append(
                    {
                        "event_seq": seq,
                        "sync_marker": verdict.get("sync_marker"),
                        "verdict": verdict["verdict"],
                        "business_code": verdict["business_code"],
                        "credential_id": verdict.get("credential_id"),
                    }
                )
                if verdict.get("credential"):
                    credential_count += 1

                maybe_crash("after_commit", credential_count)
                write_checkpoint(conn, bundle, state_dir)

            checkpoint_path = write_checkpoint(conn, bundle, state_dir)
            final_watermark = get_watermark(conn)
            return {
                "command": "process",
                "status": "partial" if bundle.get("partial") else "ok",
                "start_watermark": start_watermark,
                "final_watermark": final_watermark,
                "adjudicated_events": len(processed),
                "skipped_events": skipped,
                "processed": processed,
                "credential_count": count_credentials(conn),
                "rebuild": rebuild_info,
                "quarantined_half_writes": quarantined
                + reconcile.get("warnings", []),
                "checkpoint_path": str(checkpoint_path),
                "partial": bool(bundle.get("partial")) or bool(quarantined),
                "rejections": bundle.get("rejections", []),
            }
        finally:
            conn.close()


def run_query(args) -> Dict[str, Any]:
    state_dir = Path(args.state_dir)
    db_path = state_dir / "guard.sqlite"
    if not db_path.exists():
        raise UsageError("ledger does not exist; run import/process first")
    from .storage import connect_database

    conn = connect_database(db_path)
    try:
        clauses = []
        params = []
        if getattr(args, "endpoint_id", None):
            clauses.append("endpoint_id = ?")
            params.append(args.endpoint_id)
        if getattr(args, "process_guid", None):
            clauses.append("process_guid = ?")
            params.append(args.process_guid)
        if getattr(args, "region_id", None):
            clauses.append("region_id = ?")
            params.append(args.region_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM region_state{where} ORDER BY endpoint_id, process_guid, region_id",
            params,
        ).fetchall()
        regions = []
        for row in rows:
            item = dict(row)
            mask = int(item.get("current_prot_mask", 0))
            item["current_prot_named"] = mask_names(mask)
            item["current_prot_compact"] = format_mask(mask)
            item["current_prot_octal"] = f"0o{mask:o}"
            regions.append(item)
        return {
            "command": "query",
            "count": len(regions),
            "regions": regions,
        }
    finally:
        conn.close()


def run_report(args) -> Dict[str, Any]:
    state_dir = Path(args.state_dir)
    bundle = load_bundle(state_dir)
    if bundle is None:
        bundle = import_inputs(
            args.sample_dir,
            manifest_path=args.manifest,
            events_path=args.events,
            regions_path=args.regions,
            v1_path=args.v1,
            v2_manifest_path=args.v2_manifest,
        )
    db_path = state_dir / "guard.sqlite"
    if not db_path.exists():
        raise UsageError("ledger does not exist; run process first")
    from .storage import atomic_write_json, connect_database

    conn = connect_database(db_path)
    try:
        report = build_report(conn, bundle, state_dir=str(state_dir))
    finally:
        conn.close()

    out_path = Path(args.out) if args.out else Path(args.report_dir) / "guard-report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(out_path, report)
    report["report_path"] = str(out_path)
    report["partial"] = bool(bundle.get("partial"))
    return report


def run_verify(args) -> Dict[str, Any]:
    state_dir = Path(args.state_dir)
    with ExclusiveFileLock(state_dir / "guard.lock"):
        temps = find_temp_residue(state_dir)
        if temps:
            raise HalfWriteError(
                "half-written snapshot temporary files must be quarantined before recovery",
                details={"temporary_files": [str(path) for path in temps]},
            )

        checkpoint_path = state_dir / "checkpoint.json"
        if not checkpoint_path.exists():
            raise HalfWriteError(
                "checkpoint.json is missing; only a complete credential point is recoverable"
            )
        try:
            checkpoint = read_checkpoint(checkpoint_path)
        except (json.JSONDecodeError, ValueError) as exc:
            raise CheckpointCorrupt(
                "checkpoint.json is unparsable or failed checksum/version validation",
                details={"checkpoint_path": str(checkpoint_path), "message": str(exc)},
            ) from exc

        db_path = state_dir / "guard.sqlite"
        if not db_path.exists():
            raise UsageError("SQLite ledger does not exist")
        from .storage import connect_database

        conn = connect_database(db_path)
        try:
            sqlite_check = verify_sqlite(conn)
            watermark = get_watermark(conn)
            result = {
                "command": "verify-checkpoint",
                "business_code": "E_OK",
                "sqlite": sqlite_check,
                "checkpoint_watermark": checkpoint["last_event_seq"],
                "sqlite_watermark": watermark,
                "checkpoint_credential_count": checkpoint["credential_count"],
                "sqlite_credential_count": count_credentials(conn),
                "layout_version": checkpoint["layout_version"],
            }
            if int(checkpoint["last_event_seq"]) > watermark:
                from .constants import ReplayDiverged

                raise ReplayDiverged(
                    "checkpoint is ahead of SQLite ledger",
                    details=result,
                )
            if getattr(args, "full_replay", False):
                bundle = load_bundle(state_dir)
                if bundle is None:
                    bundle = import_inputs(
                        args.sample_dir,
                        manifest_path=args.manifest,
                        events_path=args.events,
                        regions_path=args.regions,
                        v1_path=args.v1,
                        v2_manifest_path=args.v2_manifest,
                    )
                result["full_replay"] = assert_replay_matches(conn, bundle)
            return result
        finally:
            conn.close()


def run_rollback(args) -> Dict[str, Any]:
    state_dir = Path(args.state_dir)
    bundle = load_bundle(state_dir)
    if bundle is None:
        raise UsageError("imported bundle is required before rollback")

    with ExclusiveFileLock(state_dir / "guard.lock"):
        quarantine_temp_files(state_dir)
        conn, _ = open_ledger(state_dir, bundle)
        try:
            credentials = load_credentials(conn)
            closure = closure_for_credential(
                args.credential_id,
                credentials,
                bundle["regions"],
                scope=args.scope,
            )
            if closure.get("status") == "cycle":
                raise CredentialCycle(
                    "credential dependency cycle prevents rollback closure",
                    closure["cycles"],
                    root_credential_id=args.credential_id,
                )
            if closure.get("status") != "ok":
                raise UsageError(
                    closure.get("business_code", "invalid closure request"),
                    details=closure,
                )

            by_id = {cred["credential_id"]: cred for cred in credentials}
            conn.execute("BEGIN IMMEDIATE")
            try:
                for cid in closure["rollback_order"]:
                    cred = by_id[cid]
                    cred["status"] = "rolled_back"
                    conn.execute(
                        "UPDATE credentials SET status='rolled_back', payload_json=? WHERE credential_id=?",
                        (json.dumps(cred, sort_keys=True), cid),
                    )
                    conn.execute(
                        """
                        UPDATE region_state
                        SET current_prot_mask=?, verdict='rolled_back',
                            business_code='E_ROLLBACK_OK', credential_id=?
                        WHERE endpoint_id=? AND process_guid=? AND region_id=?
                          AND region_generation=?
                        """,
                        (
                            cred["restore_prot_mask"],
                            cid,
                            cred["endpoint_id"],
                            cred["process_guid"],
                            cred["region_id"],
                            cred["region_generation"],
                        ),
                    )
                rollback_id = f"RB-{args.scope}-{args.credential_id}"
                conn.execute(
                    "INSERT OR REPLACE INTO rollback_journal VALUES (?, ?, ?, ?, ?)",
                    (
                        rollback_id,
                        args.scope,
                        args.credential_id,
                        json.dumps(closure["credential_ids"], sort_keys=True),
                        json.dumps(closure, sort_keys=True),
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            write_checkpoint(conn, bundle, state_dir)
            closure["rolled_back"] = True
            closure["rollback_id"] = rollback_id
            return {"command": "rollback", **closure}
        finally:
            conn.close()
