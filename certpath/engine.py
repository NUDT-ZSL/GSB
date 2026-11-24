"""High-level API for import, adjudication, query, reporting, and recovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canon import (
    make_sync_marker,
    sha256_file,
    sha256_obj,
    strict_json_loads,
)
from .errors import E_NOT_FOUND, E_SCHEMA, CertPathError
from .model import (
    Certificate,
    ChainDeclaration,
    ChainResult,
    TrustView,
    V1Snapshot,
    V2Snapshot,
)
from .parsing import (
    parse_certificates,
    parse_chains,
    parse_manifest,
    parse_v1_snapshot,
    parse_v2_snapshot,
)
from .paths import adjudicate_chain, aggregate_invalidation_propagation
from .store import LedgerStore, atomic_write_json
from .trust import build_trust_view


@dataclass
class ParsedSample:
    directory: Path
    manifest: dict[str, Any]
    observation_clock_ms: int
    certificates: list[Certificate]
    certificate_rejections: list[Any]
    chains: list[ChainDeclaration]
    chain_rejections: list[Any]
    v1: V1Snapshot | None
    v1_rejections: list[Any]
    v2: V2Snapshot | None
    v2_rejections: list[Any]
    sync_marker: str | None
    sync_marker_verified: bool
    source_digests: dict[str, str]


def _sample_path(sample_dir: Path, rel: str) -> Path:
    path = (sample_dir / rel).resolve()
    try:
        path.relative_to(sample_dir.resolve())
    except ValueError as exc:
        raise CertPathError(
            E_SCHEMA, f"sample path escapes sample directory: {rel}"
        ) from exc
    return path


def _read_sync_marker(
    sample_dir: Path,
) -> tuple[str | None, bool]:
    marker_path = sample_dir / "sync.marker.json"
    if not marker_path.is_file():
        return None, False
    try:
        marker = strict_json_loads(marker_path.read_text(encoding="utf-8"))
    except Exception:
        return None, False
    if not isinstance(marker, dict) or marker.get("format") != "certpath.sync":
        return None, False
    marker_value = marker.get("sync_marker")
    files = marker.get("files")
    if not isinstance(marker_value, str) or not isinstance(files, dict):
        return marker_value if isinstance(marker_value, str) else None, False
    paths: dict[str, str | Path] = {}
    for name, expected in files.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            return marker_value, False
        path = _sample_path(sample_dir, name)
        if not path.is_file():
            return marker_value, False
        paths[name] = path
    actual = make_sync_marker(paths)
    return marker_value, actual["sync_marker"] == marker_value


def parse_sample(sample_dir: str | Path) -> ParsedSample:
    directory = Path(sample_dir).resolve()
    manifest_path = directory / "manifest.json"
    manifest = parse_manifest(manifest_path)
    files = manifest["files"]
    clock = int(manifest["observation_clock_ms"])

    cert_path = _sample_path(directory, files["certificates"])
    chain_path = _sample_path(directory, files["chains"])
    v1_path = _sample_path(directory, files["v1"])
    v2_path = _sample_path(directory, files["v2"])

    certificates, cert_rejections = parse_certificates(cert_path)
    chains, chain_rejections = parse_chains(chain_path)
    v1, v1_rejections = parse_v1_snapshot(v1_path, certificates, clock)
    v2, v2_rejections = parse_v2_snapshot(v2_path)
    sync_marker, sync_verified = _read_sync_marker(directory)
    source_digests = {
        "manifest": sha256_file(manifest_path),
        "certificates": sha256_file(cert_path),
        "chains": sha256_file(chain_path),
        "v1": sha256_file(v1_path),
        "v2_meta": sha256_file(v2_path / "meta.db"),
    }
    return ParsedSample(
        directory=directory,
        manifest=manifest,
        observation_clock_ms=clock,
        certificates=certificates,
        certificate_rejections=cert_rejections,
        chains=chains,
        chain_rejections=chain_rejections,
        v1=v1,
        v1_rejections=v1_rejections,
        v2=v2,
        v2_rejections=v2_rejections,
        sync_marker=sync_marker,
        sync_marker_verified=sync_verified,
        source_digests=source_digests,
    )


def _all_rejections(parsed: ParsedSample) -> list[dict[str, Any]]:
    values = (
        list(parsed.certificate_rejections)
        + list(parsed.v1_rejections)
        + list(parsed.v2_rejections)
        + list(parsed.chain_rejections)
    )
    result = [item.to_dict() for item in values]
    result.sort(
        key=lambda item: (
            item["file_id"],
            item["record_index"],
            item["code"],
            item["message"],
        )
    )
    return result


def _build_view(
    certificates: list[Certificate],
    v1: V1Snapshot | None,
    v2: V2Snapshot | None,
    clock: int,
) -> TrustView:
    return build_trust_view(certificates, v1, v2, clock)


def _round_key(
    snapshot_digest: str,
    chains: list[ChainDeclaration],
    results: list[ChainResult],
) -> str:
    return sha256_obj(
        {
            "snapshot_digest": snapshot_digest,
            "chains": [
                item.to_dict()
                for item in sorted(chains, key=lambda value: value.chain_id)
            ],
            "results": [
                item.to_dict()
                for item in sorted(results, key=lambda value: value.chain_id)
            ],
        }
    )


class CertPathEngine:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.store = LedgerStore(self.workspace)

    def import_sample(
        self,
        sample_dir: str | Path,
        *,
        arrival_order: tuple[str, ...] = ("v1", "v2"),
    ) -> dict[str, Any]:
        """Parse the whole sample before changing state, then replay source arrival."""
        if set(arrival_order) != {"v1", "v2"} or len(arrival_order) != 2:
            raise CertPathError(
                E_SCHEMA,
                "arrival_order must contain v1 and v2 exactly once",
            )
        parsed = parse_sample(sample_dir)
        rejections = _all_rejections(parsed)
        if not parsed.certificates:
            raise CertPathError(
                E_SCHEMA,
                "no valid certificate records were accepted; state left unchanged",
            )
        snapshots: dict[str, Any] = {"v1": parsed.v1, "v2": parsed.v2}
        present: dict[str, Any] = {}
        final_payload: dict[str, Any] | None = None
        final_view: TrustView | None = None
        with self.store.lock():
            for source in arrival_order:
                present[source] = snapshots[source]
                view = _build_view(
                    parsed.certificates,
                    present.get("v1"),
                    present.get("v2"),
                    parsed.observation_clock_ms,
                )
                final_payload = self.store.commit_round(
                    snapshot_digest=view.snapshot_digest,
                    trust_generation=view.generation,
                    observation_clock_ms=parsed.observation_clock_ms,
                    sync_marker=parsed.sync_marker,
                    trust_view=view.to_dict(),
                    rejections=rejections,
                    results=[],
                    round_key=sha256_obj({"import": view.snapshot_digest}),
                    source_digests=parsed.source_digests,
                )
                final_view = view
            assert final_payload is not None
            assert final_view is not None
            self._persist_sources(parsed)
            self.store.write_report(
                self._compose_import_report(
                    parsed, final_view, rejections, final_payload
                )
            )
        return {
            "format": "certpath.import-result",
            "version": 1,
            "status": "imported",
            "arrival_order": list(arrival_order),
            "snapshot_digest": final_payload["snapshot_digest"],
            "trust_generation": final_payload["trust_generation"],
            "observation_clock_ms": parsed.observation_clock_ms,
            "sync_marker": parsed.sync_marker,
            "sync_marker_verified": parsed.sync_marker_verified,
            "rejections": rejections,
            "ledger_cleared": True,
        }

    def _compose_import_report(
        self,
        parsed: ParsedSample,
        view: TrustView,
        rejections: list[dict[str, Any]],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "format": "certpath.report",
            "version": 1,
            "round_key": payload["round_key"],
            "ledger_state": "imported_verdicts_pending",
            "snapshot": {
                "digest": view.snapshot_digest,
                "trust_generation": view.generation,
                "observation_clock_ms": view.observation_clock_ms,
                "v1_quantization": view.v1_quantization,
                "conflicts": view.conflicts,
                "pending_confirmation": sorted(
                    key
                    for key, item in view.anchors.items()
                    if item.pending_confirmation
                ),
            },
            "sync_marker": parsed.sync_marker,
            "sync_marker_verified": parsed.sync_marker_verified,
            "trust_view": view.to_dict(),
            "rejections": rejections,
            "chains": [],
            "invalidation_propagation": [],
            "summary": {
                "chains_total": 0,
                "trusted": 0,
                "partial": 0,
                "untrusted": 0,
                "candidate_paths_total": 0,
                "parse_rejections": len(rejections),
                "all_inputs_valid": not rejections,
            },
        }

    def _persist_sources(self, parsed: ParsedSample) -> None:
        inputs = self.workspace / "inputs"
        atomic_write_json(
            inputs / "certificates.json",
            {
                "accepted": [item.to_dict() for item in parsed.certificates],
                "rejected": [
                    item.to_dict() for item in parsed.certificate_rejections
                ],
            },
        )
        atomic_write_json(
            inputs / "v1.json",
            {
                "accepted": (
                    {
                        "relative_effective_seconds": parsed.v1.relative_effective_seconds,
                        "conversion": parsed.v1.conversion,
                        "anchors": [
                            {
                                "certificate_id": item.certificate_id,
                                "fingerprint": item.fingerprint,
                                "trusted": item.trusted,
                                "revoked": item.revoked,
                            }
                            for item in parsed.v1.anchors
                        ],
                    }
                    if parsed.v1 is not None
                    else None
                ),
                "rejected": [item.to_dict() for item in parsed.v1_rejections],
            },
        )
        atomic_write_json(
            inputs / "v2.json",
            {
                "accepted": (
                    {
                        "generation": parsed.v2.generation,
                        "effective_ms": parsed.v2.effective_ms,
                        "anchors": [
                            {
                                "anchor_id": item.anchor_id,
                                "fingerprint": item.fingerprint,
                                "generation": item.generation,
                                "effective_ms": item.effective_ms,
                                "path_length": item.path_length,
                                "trusted": item.trusted,
                                "tombstone": item.tombstone,
                                "revoked": item.revoked,
                            }
                            for item in parsed.v2.anchors
                        ],
                    }
                    if parsed.v2 is not None
                    else None
                ),
                "rejected": [item.to_dict() for item in parsed.v2_rejections],
            },
        )

    def adjudicate(
        self,
        sample_dir: str | Path,
        *,
        arrival_order: tuple[str, ...] = ("v1", "v2"),
        reimport: bool = True,
    ) -> dict[str, Any]:
        parsed = parse_sample(sample_dir)
        rejections = _all_rejections(parsed)
        if not parsed.chains:
            raise CertPathError(
                E_SCHEMA,
                "no valid chain declarations were accepted; ledger left unchanged",
            )
        if not parsed.certificates:
            raise CertPathError(
                E_SCHEMA,
                "no valid certificate records were accepted; ledger left unchanged",
            )
        if reimport:
            self.import_sample(sample_dir, arrival_order=arrival_order)
        view = _build_view(
            parsed.certificates, parsed.v1, parsed.v2, parsed.observation_clock_ms
        )
        ordered_chains = sorted(parsed.chains, key=lambda item: item.chain_id)
        results = [
            adjudicate_chain(chain, parsed.certificates, view)
            for chain in ordered_chains
        ]
        propagation = aggregate_invalidation_propagation(results)
        round_key = _round_key(view.snapshot_digest, ordered_chains, results)
        with self.store.lock():
            payload = self.store.commit_round(
                snapshot_digest=view.snapshot_digest,
                trust_generation=view.generation,
                observation_clock_ms=parsed.observation_clock_ms,
                sync_marker=parsed.sync_marker,
                trust_view=view.to_dict(),
                rejections=rejections,
                results=[item.to_dict() for item in results],
                round_key=round_key,
                source_digests=parsed.source_digests,
            )
            report = self._compose_report(
                parsed,
                view,
                results,
                rejections,
                propagation,
                payload,
            )
            self.store.write_report(report)
            self._persist_sources(parsed)
        return report

    def _compose_report(
        self,
        parsed: ParsedSample,
        view: TrustView,
        results: list[ChainResult],
        rejections: list[dict[str, Any]],
        propagation: list[dict[str, Any]],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        chains_json = [item.to_dict() for item in results]
        counts = {
            "chains_total": len(chains_json),
            "trusted": sum(1 for item in chains_json if item["conclusion"] == "trusted"),
            "partial": sum(1 for item in chains_json if item["conclusion"] == "partial"),
            "untrusted": sum(
                1 for item in chains_json if item["conclusion"] == "untrusted"
            ),
            "candidate_paths_total": sum(
                item["candidates_total"] for item in chains_json
            ),
            "parse_rejections": len(rejections),
            "all_inputs_valid": not rejections,
        }
        view_json = view.to_dict()
        return {
            "format": "certpath.report",
            "version": 1,
            "round_key": payload["round_key"],
            "snapshot": {
                "digest": view.snapshot_digest,
                "trust_generation": view.generation,
                "observation_clock_ms": view.observation_clock_ms,
                "v1_quantization": view.v1_quantization,
                "conflicts": view.conflicts,
                "pending_confirmation": sorted(
                    key
                    for key, item in view.anchors.items()
                    if item.pending_confirmation
                ),
            },
            "sync_marker": parsed.sync_marker,
            "sync_marker_verified": parsed.sync_marker_verified,
            "trust_view": view_json,
            "rejections": rejections,
            "chains": chains_json,
            "invalidation_propagation": propagation,
            "summary": counts,
        }

    def query(self, chain_id: str | None = None) -> dict[str, Any]:
        rows = self.store.load_verdict_rows()
        if chain_id is not None:
            rows = [row for row in rows if row["chain_id"] == chain_id]
            if not rows:
                raise CertPathError(
                    E_NOT_FOUND,
                    f"no verdict for chain_id {chain_id!r}",
                )
        return {
            "format": "certpath.query-result",
            "version": 1,
            "chain_id": chain_id,
            "verdicts": rows,
        }

    def export_report(self, output: str | Path | None = None) -> dict[str, Any]:
        report = self.store.read_report()
        if report is None:
            report = self._reconstruct_report()
        target = Path(output) if output is not None else self.store.report_path
        atomic_write_json(target, report)
        return report

    def _reconstruct_report(self) -> dict[str, Any]:
        view = self.store.read_trust_view()
        rows = self.store.load_verdict_rows()
        meta = self.store.read_meta()
        if view is None and not rows:
            raise CertPathError(E_NOT_FOUND, "no diagnostic report is available")
        chain_results = [row["result"] for row in rows]
        rejections = self.store.read_rejections()
        return {
            "format": "certpath.report",
            "version": 1,
            "round_key": meta.get("round_key"),
            "snapshot": {
                "digest": meta.get("snapshot_digest"),
                "trust_generation": int(meta.get("trust_generation", "0")),
                "observation_clock_ms": int(
                    meta.get("observation_clock_ms", "0")
                ),
                "v1_quantization": (
                    view.get("v1_quantization") if view is not None else None
                ),
                "conflicts": view.get("conflicts", []) if view else [],
                "pending_confirmation": (
                    view.get("pending_confirmation", []) if view else []
                ),
            },
            "sync_marker": meta.get("sync_marker") or None,
            "sync_marker_verified": False,
            "trust_view": view,
            "rejections": rejections,
            "chains": chain_results,
            "invalidation_propagation": aggregate_invalidation_propagation(
                [
                    ChainResult(
                        chain_key=row["chain_key"],
                        chain_id=row["chain_id"],
                        declaration=row["result"]["declaration"],
                        conclusion=row["result"]["conclusion"],
                        selected_path=None,
                        candidates=[],
                        candidates_total=row["result"]["candidates_total"],
                        truncated=row["result"]["enumeration_truncated"],
                        truncated_count=row["result"]["truncated_count"],
                        truncated_has_valid=row["result"]["truncated_has_valid"],
                        business_codes=row["result"]["business_codes"],
                        invalidations=row["result"]["invalidations"],
                        remaining_available_paths=[],
                        enumeration_gaps=[],
                    )
                    for row in rows
                ]
            ),
            "summary": {
                "chains_total": len(chain_results),
                "trusted": sum(
                    1
                    for item in chain_results
                    if item["conclusion"] == "trusted"
                ),
                "partial": sum(
                    1
                    for item in chain_results
                    if item["conclusion"] == "partial"
                ),
                "untrusted": sum(
                    1
                    for item in chain_results
                    if item["conclusion"] == "untrusted"
                ),
                "candidate_paths_total": sum(
                    int(item.get("candidates_total", 0))
                    for item in chain_results
                ),
                "parse_rejections": len(rejections),
                "all_inputs_valid": not rejections,
            },
        }

    def verify_checkpoint(self, *, recover: bool = False) -> dict[str, Any]:
        return self.store.verify_checkpoint(recover=recover)


def adjudicate_sample(
    sample_dir: str | Path,
    workspace: str | Path,
    *,
    arrival_order: tuple[str, ...] = ("v1", "v2"),
) -> dict[str, Any]:
    engine = CertPathEngine(workspace)
    return engine.adjudicate(sample_dir, arrival_order=arrival_order)
