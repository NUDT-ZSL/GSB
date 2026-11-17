"""CLI-triggered anomaly injection with business-code reporting."""

from __future__ import annotations

import copy
import json
import socket
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .config import Declaration, parse_declaration
from .errors import E_LEDGER, E_PORT, E_VERSION, BusinessError
from .ledger import Ledger
from .logging_utils import EventLogger
from .service_manager import ClusterServiceManager, check_loopback_port_available
from .verifier import AuthorityVerifier


def _business_report(anomaly: str, error: BusinessError) -> dict[str, Any]:
    return {
        "ok": False,
        "anomaly": anomaly,
        "code": error.code,
        "message": error.message,
        "field_path": error.field_path,
        "details": error.details,
    }


def _inject_port_anomaly(config_path: str, sample_path: str, logger: EventLogger) -> dict[str, Any]:
    from .config import load_declaration

    declaration = load_declaration(config_path)
    cluster = declaration.clusters[0]
    family = socket.AF_INET6 if cluster.host == "::1" else socket.AF_INET
    blocker = socket.socket(family, socket.SOCK_STREAM)
    try:
        if cluster.host != "::1":
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            blocker.bind((cluster.host, cluster.port))
        except OSError as exc:
            error = BusinessError(
                E_PORT,
                "port was already occupied before injection",
                details={
                    "cluster_id": cluster.cluster_id,
                    "host": cluster.host,
                    "port": cluster.port,
                    "os_error": str(exc),
                },
            )
            logger.error(error, event="anomaly_injected", anomaly="port_occupied")
            return _business_report("port_occupied", error)
        blocker.listen(1)
        manager = ClusterServiceManager(config_path, sample_path)
        try:
            try:
                manager.start_cluster(cluster)
            except BusinessError as exc:
                logger.error(
                    exc,
                    event="anomaly_injected",
                    anomaly="port_occupied",
                    cluster_id=cluster.cluster_id,
                    host=cluster.host,
                    port=cluster.port,
                )
                return _business_report("port_occupied", exc)
            return {
                "ok": False,
                "anomaly": "port_occupied",
                "code": E_PORT,
                "message": "injection failed: service unexpectedly started",
            }
        finally:
            manager.stop_all()
    finally:
        blocker.close()


def _inject_version_rollback(declaration: Declaration,
                             logger: EventLogger) -> dict[str, Any]:
    payload = copy.deepcopy(declaration.to_dict())
    target = payload["clusters"][0]
    old_version = int(target["config_version"])
    seeded_version = old_version + 1
    target["config_version"] = seeded_version
    target["version_history"] = list(target.get("version_history", [old_version])) + [seeded_version]
    seed_declaration = parse_declaration(payload)
    with tempfile.TemporaryDirectory(prefix="cluster-ledger-version-anomaly-") as temp_dir:
        ledger = Ledger(Path(temp_dir) / "seed.sqlite3")
        try:
            ledger.reconcile(seed_declaration)
            try:
                ledger.reconcile(declaration)
            except BusinessError as exc:
                logger.error(
                    exc,
                    event="anomaly_injected",
                    anomaly="version_rollback",
                    cluster_id=target["cluster_id"],
                    seeded_version=seeded_version,
                    declared_version=old_version,
                )
                return _business_report("version_rollback", exc)
        finally:
            ledger.close()
    return {
        "ok": False,
        "anomaly": "version_rollback",
        "code": E_VERSION,
        "message": "injection failed: rollback was not rejected",
    }


def _copy_sqlite(source_path: str, target_path: Path) -> None:
    source = sqlite3.connect(source_path, timeout=30.0)
    target = sqlite3.connect(str(target_path), timeout=30.0)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _inject_ledger_corruption(declaration: Declaration, sample_data: dict[str, Any],
                              ledger_path: str, logger: EventLogger) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="cluster-ledger-corruption-") as temp_dir:
        copy_path = Path(temp_dir) / "ledger.sqlite3"
        _copy_sqlite(ledger_path, copy_path)
        ledger = Ledger(copy_path)
        try:
            try:
                corrupted = ledger.corrupt_result_summary()
            except BusinessError as exc:
                logger.error(exc, event="anomaly_injected", anomaly="ledger_summary_corruption")
                return _business_report("ledger_summary_corruption", exc)
            verifier = AuthorityVerifier(declaration, sample_data, ledger, logger)
            report = verifier.verify()
            first_error = report["errors"][0] if report["errors"] else None
            if first_error and first_error.get("code") == E_LEDGER:
                error = BusinessError(
                    E_LEDGER,
                    "injected ledger summary corruption was detected",
                    field_path="results.summary",
                    details={
                        "corrupted_row": corrupted,
                        "first_diff_key": first_error.get("first_diff_key"),
                        "verification_errors": report["errors"],
                    },
                )
                logger.error(error, event="anomaly_injected", anomaly="ledger_summary_corruption")
                return _business_report("ledger_summary_corruption", error)
            return {
                "ok": False,
                "anomaly": "ledger_summary_corruption",
                "code": E_LEDGER,
                "message": "injection could not be confirmed by verification",
                "details": {"verification": report},
            }
        finally:
            ledger.close()


def run_anomaly(anomaly: str, *, config_path: str, sample_path: str,
                ledger_path: str, logger: EventLogger) -> dict[str, Any]:
    from .config import load_declaration, load_sample_data

    declaration = load_declaration(config_path)
    sample_data = load_sample_data(sample_path)
    if anomaly == "port":
        return _inject_port_anomaly(config_path, sample_path, logger)
    if anomaly == "version_rollback":
        return _inject_version_rollback(declaration, logger)
    if anomaly == "ledger":
        return _inject_ledger_corruption(declaration, sample_data, ledger_path, logger)
    raise BusinessError(
        E_LEDGER,
        f"unknown anomaly {anomaly!r}",
        details={"supported": ["port", "version_rollback", "ledger"]},
    )
