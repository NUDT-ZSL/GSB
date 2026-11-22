"""Sample loading: digest verification + all-or-nothing validation/persistence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Tuple

from .errors import BusinessError, E_BAD_INPUT, E_SAMPLE_MISMATCH
from .models import (
    ClockConfig,
    Event,
    SLO,
    parse_clock_config,
    parse_event_list,
    parse_slo_list,
)
from .repository import Repository
from .storage import Workspace, transaction


def _read_json(path: Path) -> Any:
    if not path.exists():
        raise BusinessError(
            E_BAD_INPUT, "required sample file is missing", fields={"path": str(path)}
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BusinessError(
            E_BAD_INPUT,
            "sample file is not valid JSON",
            fields={"path": str(path), "detail": str(exc)},
        ) from exc


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_manifest(sample_dir: Path) -> Dict[str, str]:
    """Check the deterministic manifest; raise ``E_SAMPLE_MISMATCH`` on drift."""
    manifest_path = sample_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or "files" not in manifest:
        raise BusinessError(
            E_SAMPLE_MISMATCH,
            "manifest.json must contain a files digest map",
            fields={"path": str(manifest_path)},
        )
    verified: Dict[str, str] = {}
    for relative, expected in sorted(manifest["files"].items()):
        target = sample_dir / relative
        if not target.exists():
            raise BusinessError(
                E_SAMPLE_MISMATCH,
                "manifest references a missing file",
                fields={"file": relative},
            )
        actual = _sha256_file(target)
        if actual != expected:
            raise BusinessError(
                E_SAMPLE_MISMATCH,
                "sample digest does not match manifest",
                fields={
                    "file": relative,
                    "expected_sha256": expected,
                    "actual_sha256": actual,
                },
            )
        verified[relative] = actual
    return verified


def load_sample_bundle(
    workspace: Workspace, sample_dir: Path, *, skip_manifest: bool = False
) -> Dict[str, Any]:
    """Validate and atomically load SLOs + events.

    Parsing/validation happens *before* any write, and the persistence step is
    one IMMEDIATE transaction.  Any ``E_BAD_SLO`` / ``E_BAD_EVENT`` therefore
    leaves the database untouched.
    """
    if not skip_manifest:
        verify_manifest(sample_dir)

    slos = parse_slo_list(_read_json(sample_dir / "slos.json"))
    events = parse_event_list(_read_json(sample_dir / "events.json"))
    clock_config = parse_clock_config(_read_json(sample_dir / "clock.json"))

    known_services = {slo.service for slo in slos}
    for event in events:
        if event.service not in known_services:
            raise BusinessError(
                "E_BAD_EVENT",
                "event references an undefined service",
                fields={"event_seq": event.seq, "service": event.service},
            )

    conn = workspace.connect()
    try:
        with transaction(conn):
            repo = Repository(conn)
            version = repo.replace_slos(slos)
            repo.replace_events(events)
            repo.set_meta("sample_dir", str(sample_dir.resolve()))
            repo.set_meta(
                "rollback_tolerance_ms", str(clock_config.rollback_tolerance_ms)
            )
        return {
            "loaded": True,
            "version": version,
            "slo_count": len(slos),
            "event_count": len(events),
            "clock_ticks": len(clock_config.ticks_ms),
        }
    finally:
        conn.close()


def read_bundle(
    sample_dir: Path, *, skip_manifest: bool = False
) -> Tuple[list[SLO], list[Event], ClockConfig]:
    """Parse-only helper (also used by tests and the sample generator)."""
    if not skip_manifest:
        verify_manifest(sample_dir)
    slos = parse_slo_list(_read_json(sample_dir / "slos.json"))
    events = parse_event_list(_read_json(sample_dir / "events.json"))
    clock_config = parse_clock_config(_read_json(sample_dir / "clock.json"))
    return slos, events, clock_config
