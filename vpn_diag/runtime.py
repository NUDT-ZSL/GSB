"""High-level workspace execution and recovery helpers."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .checkpoint import CheckpointManager, CheckpointVersionError, remove_residue_files
from .echo import EchoServer, UdpEchoTransport
from .engine import ForwardingEngine
from .parser import parse_sample
from .report import build_report


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def start_echo_servers(parsed: Any) -> tuple[list[EchoServer], list[dict[str, Any]]]:
    servers: list[EchoServer] = []
    info: list[dict[str, Any]] = []
    for peer in parsed.peers:
        try:
            server = EchoServer(peer.host, peer.port).start()
        except OSError as exc:
            info.append({
                "peer": peer.name,
                "requested_address": peer.address,
                "started": False,
                "reason": str(exc),
                "assumed_external_echo": True,
            })
            continue
        if peer.port == 0:
            peer.port = server.port
            peer.address = f"{peer.host}:{peer.port}"
        servers.append(server)
        info.append({
            "peer": peer.name,
            "address": f"{server.host}:{server.port}",
            "started": True,
        })
    return servers, info


def execute_sample(
    sample_path: str | Path,
    workspace: str | Path,
    *,
    max_rounds: Optional[int] = None,
    resume: bool = True,
    start_echo: bool = True,
    rebuild_on_version_mismatch: bool = True,
    udp_timeout_ms: Optional[int] = None,
) -> dict[str, Any]:
    sample_path = Path(sample_path)
    workspace = Path(workspace)
    sample_bytes_hash = file_sha256(sample_path)
    parsed = parse_sample(__import__("vpn_diag.parser", fromlist=["load_raw_sample"]).load_raw_sample(sample_path))
    if parsed.policy is None:
        raise ValueError("sample has no valid policy; cannot execute forwarding engine")
    if udp_timeout_ms is not None:
        parsed.policy.udp_timeout_ms = int(udp_timeout_ms)

    manager, rebuild_info = CheckpointManager.prepare(
        workspace, rebuild_on_mismatch=rebuild_on_version_mismatch
    )
    checkpoint_verification: Optional[dict[str, Any]] = None
    checkpoint = None
    try:
        if resume:
            loaded = manager.load_latest_intact()
            if loaded is not None:
                checkpoint, checkpoint_verification = loaded
                if checkpoint.state.get("sample_sha256") not in {None, sample_bytes_hash}:
                    raise ValueError("checkpoint was created from a different sample version")

        transport = UdpEchoTransport(parsed.policy.udp_timeout_ms)
        if checkpoint is not None:
            engine = ForwardingEngine.from_checkpoint(
                parsed,
                checkpoint.state,
                transport=transport,
                checkpoint_manager=manager,
            )
            resumed = True
        else:
            engine = ForwardingEngine(
                parsed, transport=transport, checkpoint_manager=manager
            )
            engine.state["sample_sha256"] = sample_bytes_hash
            manager.save_checkpoint(engine.state)
            resumed = False

        servers: list[EchoServer] = []
        echo_info: list[dict[str, Any]] = []
        try:
            if start_echo:
                servers, echo_info = start_echo_servers(parsed)
            engine.run(max_rounds=max_rounds)
        finally:
            for server in servers:
                server.stop()

        verification = manager.verify()
        report = build_report(
            parsed,
            engine.state,
            sample_path=sample_path,
            sample_sha256=sample_bytes_hash,
            checkpoint_verification=verification,
            rebuild_info=rebuild_info,
            run_meta={
                "resumed_from_checkpoint": resumed,
                "resumed_watermark": checkpoint.round_watermark if checkpoint else None,
                "echo_servers": echo_info,
                "start_echo": start_echo,
                "udp_timeout_ms": parsed.policy.udp_timeout_ms,
            },
        )
        manager.save_named_json("latest_report", report)
        manager.save_named_json("latest_sample_meta", {
            "path": str(sample_path.resolve()),
            "sha256": sample_bytes_hash,
        })
        return report
    finally:
        manager.close()


def verify_workspace(
    workspace: str | Path,
    *,
    sample_path: Optional[str | Path] = None,
    clean_residue: bool = False,
) -> dict[str, Any]:
    workspace = Path(workspace)
    manager = CheckpointManager(workspace)
    try:
        try:
            versions = manager.initialize()
        except CheckpointVersionError as exc:
            result = {
                "valid": False,
                "code": exc.code,
                "message": str(exc),
            }
            return result
        except (sqlite3.DatabaseError, Exception) as exc:
            code = getattr(exc, "code", None)
            if code is None:
                code = "E_CHECKPOINT_CORRUPT" if isinstance(exc, sqlite3.DatabaseError) else "E_CHECKPOINT_VERSION"
            result = {"valid": False, "code": code, "message": str(exc)}
            return result
        result = manager.verify()
        result["versions"] = versions

        target_sample = Path(sample_path) if sample_path else workspace / "imported_sample.json"
        if target_sample.exists():
            try:
                parsed = parse_sample(
                    __import__("vpn_diag.parser", fromlist=["load_raw_sample"]).load_raw_sample(target_sample)
                )
                loaded = manager.load_latest_intact()
                if loaded is not None and parsed.policy is not None:
                    checkpoint, verification = loaded
                    engine = ForwardingEngine.from_checkpoint(
                        parsed, checkpoint.state, transport=None, checkpoint_manager=None
                    )
                    result["replay_consistency"] = {
                        "consistent": True,
                        "planned_fragments": sum(len(keys) for keys in engine.plans.values()),
                        "checkpoint_watermark": checkpoint.round_watermark,
                        "sample_sha256_matches": checkpoint.state.get("sample_sha256") == file_sha256(target_sample),
                    }
                else:
                    result["replay_consistency"] = {"consistent": False, "reason": "no intact checkpoint or policy"}
            except Exception as exc:
                result["valid"] = False
                result["code"] = "E_CHECKPOINT_STATE_MISMATCH"
                result["replay_consistency"] = {"consistent": False, "reason": str(exc)}

        if clean_residue:
            result["removed_residue"] = remove_residue_files(result)
        return result
    finally:
        manager.close()


def query_progress(workspace: str | Path, peer_name: Optional[str] = None) -> dict[str, Any]:
    manager = CheckpointManager(workspace)
    try:
        manager.initialize()
        loaded = manager.load_latest_intact()
        if loaded is None:
            raise FileNotFoundError("no intact checkpoint found")
        checkpoint, verification = loaded
        peers = checkpoint.state.get("peers", {})
        if peer_name:
            if peer_name not in peers:
                raise KeyError(peer_name)
            return peers[peer_name]
        return {
            "round_watermark": checkpoint.round_watermark,
            "logical_clock_ms": checkpoint.logical_clock_q / 1000,
            "peers": {
                name: {
                    "status": data.get("status"),
                    "acknowledged_fragments": data.get("acknowledged_fragments"),
                    "total_fragments": data.get("total_fragments"),
                    "retransmit_used": data.get("retransmit_used"),
                    "last_valid_progress": data.get("last_valid_progress"),
                    "stall": data.get("stall"),
                }
                for name, data in peers.items()
            },
            "verification": {"valid": verification.get("valid"), "issues": verification.get("issues")},
        }
    finally:
        manager.close()


def export_latest_report(workspace: str | Path, output: str | Path) -> Path:
    manager = CheckpointManager.prepare(workspace, rebuild_on_mismatch=False)[0]
    try:
        report = manager.load_named_json("latest_report")
        if report is None:
            raise FileNotFoundError("no latest report in checkpoint database; run forwarding first")
        from .report import write_report

        return write_report(output, report)
    finally:
        manager.close()
