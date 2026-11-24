"""Parsing, per-record validation, and deterministic normalization."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .constants import (
    CANONICAL_DECIMALS,
    COUNT_QUOTA_VERSION,
    INTERVAL_QUOTA_VERSION,
    SUPPORTED_PLATFORMS,
)
from .errors import Issue
from .fingerprint import canonicalize
from .io_utils import read_json
from .models import (
    CountQuota,
    FingerprintDeclaration,
    IntervalQuota,
    PlatformProfile,
    RenewalRequest,
    RevocationRequest,
    TicketDeclaration,
)


@dataclass
class NormalizedDataset:
    platforms: dict[str, PlatformProfile] = field(default_factory=dict)
    fingerprints: dict[str, FingerprintDeclaration] = field(default_factory=dict)
    tickets: dict[str, TicketDeclaration] = field(default_factory=dict)
    renewals: list[RenewalRequest] = field(default_factory=list)
    revocations: list[RevocationRequest] = field(default_factory=list)
    count_quota: CountQuota | None = None
    interval_quota: IntervalQuota | None = None
    issues: list[Issue] = field(default_factory=list)

    @property
    def quota_profile_version(self) -> str:
        count_version = (
            self.count_quota.version if self.count_quota else "missing-count"
        )
        interval_version = (
            self.interval_quota.version if self.interval_quota else "missing-interval"
        )
        return f"{count_version}+{interval_version}"

    def renewal_order(self) -> list[RenewalRequest]:
        return sorted(
            self.renewals,
            key=lambda item: (
                item.observed_at_ms,
                item.request_id,
                item.batch_id,
            ),
        )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _issue(
    issues: list[Issue],
    source_type: str,
    record_index: int,
    code: str,
    message: str,
    field: str | None = None,
) -> None:
    issues.append(
        Issue(
            source_type=source_type,
            record_index=record_index,
            code=code,
            message=message,
            field=field,
        )
    )


def _records(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        return payload["records"]
    raise ValueError("declaration file must contain a JSON array or a records array")


def _parse_platforms(
    records: Iterable[Any],
    dataset: NormalizedDataset,
) -> None:
    seen: set[str] = set()
    for index, raw in enumerate(records, start=1):
        if not isinstance(raw, Mapping):
            _issue(dataset.issues, "platform", index, "E_INVALID_RECORD", "record must be an object")
            continue
        platform = raw.get("platform")
        decimals = raw.get("decimals")
        quantum = raw.get("clock_quantum_ms")
        if not isinstance(platform, str) or platform not in SUPPORTED_PLATFORMS:
            _issue(dataset.issues, "platform", index, "E_PLATFORM", "platform is not whitelisted", "platform")
            continue
        if platform in seen:
            _issue(dataset.issues, "platform", index, "E_ID_DUPLICATE", "platform profile is duplicated", "platform")
            continue
        if not _is_int(decimals) or not 0 <= decimals <= CANONICAL_DECIMALS:
            _issue(dataset.issues, "platform", index, "E_INVALID_RECORD", "decimals must be an integer in 0..6", "decimals")
            continue
        if not _is_int(quantum) or quantum <= 0:
            _issue(dataset.issues, "platform", index, "E_INVALID_RECORD", "clock_quantum_ms must be a positive integer", "clock_quantum_ms")
            continue
        seen.add(platform)
        dataset.platforms[platform] = PlatformProfile(
            platform=platform,
            decimals=decimals,
            clock_quantum_ms=quantum,
        )


def _parse_components(
    raw_components: Any,
    *,
    source_type: str,
    index: int,
    issues: list[Issue],
) -> tuple[int, ...] | None:
    if not isinstance(raw_components, list) or not raw_components:
        _issue(issues, source_type, index, "E_INVALID_RECORD", "fingerprint must contain at least one component", "components")
        return None
    components: list[int] = []
    for component_index, component in enumerate(raw_components):
        if not _is_int(component) or component < 0:
            _issue(
                issues,
                source_type,
                index,
                "E_INVALID_RECORD",
                "fingerprint components must be non-negative integers",
                f"components[{component_index}]",
            )
            return None
        components.append(component)
    return tuple(components)


def _parse_fingerprints(
    records: Iterable[Any],
    dataset: NormalizedDataset,
) -> None:
    seen: set[str] = set()
    for index, raw in enumerate(records, start=1):
        if not isinstance(raw, Mapping):
            _issue(dataset.issues, "fingerprint", index, "E_INVALID_RECORD", "record must be an object")
            continue
        device_id = raw.get("device_id")
        platform = raw.get("platform")
        if not isinstance(device_id, str) or not device_id:
            _issue(dataset.issues, "fingerprint", index, "E_INVALID_RECORD", "device_id must be a non-empty string", "device_id")
            continue
        if device_id in seen:
            _issue(dataset.issues, "fingerprint", index, "E_ID_DUPLICATE", "device_id is duplicated", "device_id")
            continue
        if not isinstance(platform, str) or platform not in dataset.platforms:
            _issue(dataset.issues, "fingerprint", index, "E_PLATFORM", "platform has no valid declaration", "platform")
            continue
        components = _parse_components(
            raw.get("components"),
            source_type="fingerprint",
            index=index,
            issues=dataset.issues,
        )
        if components is None:
            continue
        seen.add(device_id)
        fingerprint = canonicalize(
            device_id=device_id,
            platform=platform,
            profile=dataset.platforms[platform],
            components=components,
        )
        dataset.fingerprints[device_id] = FingerprintDeclaration(
            device_id=device_id,
            platform=platform,
            fingerprint=fingerprint,
        )


def _parse_tickets(
    records: Iterable[Any],
    dataset: NormalizedDataset,
) -> None:
    seen: set[str] = set()
    for index, raw in enumerate(records, start=1):
        if not isinstance(raw, Mapping):
            _issue(dataset.issues, "ticket", index, "E_INVALID_RECORD", "record must be an object")
            continue
        ticket_id = raw.get("ticket_id")
        device_id = raw.get("device_id")
        if not isinstance(ticket_id, str) or not ticket_id:
            _issue(dataset.issues, "ticket", index, "E_INVALID_RECORD", "ticket_id must be a non-empty string", "ticket_id")
            continue
        if ticket_id in seen:
            _issue(dataset.issues, "ticket", index, "E_ID_DUPLICATE", "ticket_id is duplicated", "ticket_id")
            continue
        if not isinstance(device_id, str) or not device_id:
            _issue(dataset.issues, "ticket", index, "E_INVALID_RECORD", "device_id must be a non-empty string", "device_id")
            continue
        if device_id not in dataset.fingerprints:
            _issue(dataset.issues, "ticket", index, "E_UNKNOWN_DEVICE", "ticket binds an unknown device", "device_id")
            continue
        generation = raw.get("generation")
        renewal_limit = raw.get("renewal_limit")
        issued_at_ms = raw.get("issued_at_ms")
        expires_at_ms = raw.get("expires_at_ms")
        usage_count = raw.get("usage_count", 0)
        if not _is_int(generation) or generation < 0:
            _issue(dataset.issues, "ticket", index, "E_INVALID_RECORD", "generation must be a non-negative integer", "generation")
            continue
        if not _is_int(renewal_limit) or renewal_limit < 0:
            _issue(dataset.issues, "ticket", index, "E_INVALID_RECORD", "renewal_limit must be a non-negative integer", "renewal_limit")
            continue
        if generation > renewal_limit:
            _issue(dataset.issues, "ticket", index, "E_GENERATION_INCONSISTENT", "generation exceeds renewal_limit", "generation")
            continue
        if not _is_int(issued_at_ms) or not _is_int(expires_at_ms):
            _issue(dataset.issues, "ticket", index, "E_INVALID_RECORD", "ticket times must be integer milliseconds", "issued_at_ms")
            continue
        if issued_at_ms >= expires_at_ms:
            _issue(dataset.issues, "ticket", index, "E_TICKET_PERIOD", "issued_at_ms must be less than expires_at_ms", "issued_at_ms")
            continue
        if not _is_int(usage_count) or usage_count < 0:
            _issue(dataset.issues, "ticket", index, "E_INVALID_RECORD", "usage_count must be a non-negative integer", "usage_count")
            continue
        parent = raw.get("parent_ticket_id")
        if parent is not None and (not isinstance(parent, str) or not parent):
            _issue(dataset.issues, "ticket", index, "E_INVALID_RECORD", "parent_ticket_id must be null or a non-empty string", "parent_ticket_id")
            continue
        seen.add(ticket_id)
        dataset.tickets[ticket_id] = TicketDeclaration(
            ticket_id=ticket_id,
            device_id=device_id,
            generation=generation,
            renewal_limit=renewal_limit,
            issued_at_ms=issued_at_ms,
            expires_at_ms=expires_at_ms,
            usage_count=usage_count,
            parent_ticket_id=parent,
        )


def _parse_renewals(
    records: Iterable[Any],
    dataset: NormalizedDataset,
) -> None:
    seen: set[str] = set()
    for index, raw in enumerate(records, start=1):
        if not isinstance(raw, Mapping):
            _issue(dataset.issues, "renewal", index, "E_INVALID_RECORD", "record must be an object")
            continue
        request_id = raw.get("request_id")
        ticket_id = raw.get("ticket_id")
        device_id = raw.get("device_id")
        observed_at_ms = raw.get("observed_at_ms")
        batch_id = raw.get("batch_id", 1)
        if not isinstance(request_id, str) or not request_id:
            _issue(dataset.issues, "renewal", index, "E_INVALID_RECORD", "request_id must be a non-empty string", "request_id")
            continue
        if request_id in seen:
            _issue(dataset.issues, "renewal", index, "E_ID_DUPLICATE", "request_id is duplicated", "request_id")
            continue
        if not isinstance(ticket_id, str) or not ticket_id or not isinstance(device_id, str) or not device_id:
            _issue(dataset.issues, "renewal", index, "E_INVALID_RECORD", "ticket_id and device_id are required", "ticket_id")
            continue
        if not _is_int(observed_at_ms) or observed_at_ms < 0:
            _issue(dataset.issues, "renewal", index, "E_INVALID_RECORD", "observed_at_ms must be a non-negative integer", "observed_at_ms")
            continue
        if not _is_int(batch_id) or batch_id <= 0:
            _issue(dataset.issues, "renewal", index, "E_INVALID_RECORD", "batch_id must be a positive integer", "batch_id")
            continue
        raw_fingerprint = raw.get("fingerprint")
        if not isinstance(raw_fingerprint, Mapping):
            _issue(dataset.issues, "renewal", index, "E_INVALID_RECORD", "fingerprint object is required", "fingerprint")
            continue
        platform = raw_fingerprint.get("platform")
        if not isinstance(platform, str) or platform not in dataset.platforms:
            _issue(dataset.issues, "renewal", index, "E_PLATFORM", "request fingerprint platform is invalid", "fingerprint.platform")
            continue
        components = _parse_components(
            raw_fingerprint.get("components"),
            source_type="renewal",
            index=index,
            issues=dataset.issues,
        )
        if components is None:
            continue
        successor = raw.get("successor_ticket_id")
        if successor is not None and (not isinstance(successor, str) or not successor):
            _issue(dataset.issues, "renewal", index, "E_INVALID_RECORD", "successor_ticket_id must be null or a non-empty string", "successor_ticket_id")
            continue
        seen.add(request_id)
        dataset.renewals.append(
            RenewalRequest(
                request_id=request_id,
                ticket_id=ticket_id,
                device_id=device_id,
                observed_at_ms=observed_at_ms,
                batch_id=batch_id,
                fingerprint=canonicalize(
                    device_id=device_id,
                    platform=platform,
                    profile=dataset.platforms[platform],
                    components=components,
                ),
                successor_ticket_id=successor,
            )
        )


def _parse_quotas(
    records: Iterable[Any],
    dataset: NormalizedDataset,
) -> None:
    for index, raw in enumerate(records, start=1):
        if not isinstance(raw, Mapping):
            _issue(dataset.issues, "quota", index, "E_INVALID_RECORD", "record must be an object")
            continue
        kind = raw.get("kind")
        if kind == "device_window":
            version = raw.get("version")
            window_seconds = raw.get("window_seconds")
            anchor_seconds = raw.get("window_anchor_seconds")
            limit = raw.get("max_renewals_per_window")
            if version != COUNT_QUOTA_VERSION:
                _issue(dataset.issues, "quota", index, "E_INVALID_RECORD", "unexpected device-window quota version", "version")
                continue
            if not _is_int(window_seconds) or window_seconds <= 0:
                _issue(dataset.issues, "quota", index, "E_INVALID_RECORD", "window_seconds must be positive", "window_seconds")
                continue
            if not _is_int(anchor_seconds):
                _issue(dataset.issues, "quota", index, "E_INVALID_RECORD", "window_anchor_seconds must be integer seconds", "window_anchor_seconds")
                continue
            if not _is_int(limit) or limit < 0:
                _issue(dataset.issues, "quota", index, "E_INVALID_RECORD", "max_renewals_per_window must be non-negative", "max_renewals_per_window")
                continue
            if dataset.count_quota is not None:
                _issue(dataset.issues, "quota", index, "E_ID_DUPLICATE", "device-window quota is duplicated", "kind")
                continue
            dataset.count_quota = CountQuota(
                version=version,
                window_seconds=window_seconds,
                window_anchor_seconds=anchor_seconds,
                max_renewals_per_window=limit,
            )
        elif kind == "ticket_interval":
            version = raw.get("version")
            interval = raw.get("min_interval_ms")
            if version != INTERVAL_QUOTA_VERSION:
                _issue(dataset.issues, "quota", index, "E_INVALID_RECORD", "unexpected ticket-interval quota version", "version")
                continue
            if not _is_int(interval) or interval < 0:
                _issue(dataset.issues, "quota", index, "E_INVALID_RECORD", "min_interval_ms must be non-negative", "min_interval_ms")
                continue
            if dataset.interval_quota is not None:
                _issue(dataset.issues, "quota", index, "E_ID_DUPLICATE", "ticket-interval quota is duplicated", "kind")
                continue
            dataset.interval_quota = IntervalQuota(
                version=version,
                min_interval_ms=interval,
            )
        else:
            _issue(dataset.issues, "quota", index, "E_INVALID_RECORD", "unknown quota kind", "kind")


def _parse_revocations(
    records: Iterable[Any],
    dataset: NormalizedDataset,
) -> None:
    seen: set[str] = set()
    for index, raw in enumerate(records, start=1):
        if not isinstance(raw, Mapping):
            _issue(dataset.issues, "revocation", index, "E_INVALID_RECORD", "record must be an object")
            continue
        request_id = raw.get("request_id")
        target_type = raw.get("target_type")
        target_id = raw.get("target_id")
        observed_at_ms = raw.get("observed_at_ms")
        if not isinstance(request_id, str) or not request_id:
            _issue(dataset.issues, "revocation", index, "E_INVALID_RECORD", "request_id must be non-empty", "request_id")
            continue
        if request_id in seen:
            _issue(dataset.issues, "revocation", index, "E_ID_DUPLICATE", "request_id is duplicated", "request_id")
            continue
        if target_type not in {"device", "ticket"}:
            _issue(dataset.issues, "revocation", index, "E_INVALID_RECORD", "target_type must be device or ticket", "target_type")
            continue
        if not isinstance(target_id, str) or not target_id:
            _issue(dataset.issues, "revocation", index, "E_INVALID_RECORD", "target_id must be non-empty", "target_id")
            continue
        if not _is_int(observed_at_ms) or observed_at_ms < 0:
            _issue(dataset.issues, "revocation", index, "E_INVALID_RECORD", "observed_at_ms must be a non-negative integer", "observed_at_ms")
            continue
        seen.add(request_id)
        dataset.revocations.append(
            RevocationRequest(
                request_id=request_id,
                target_type=target_type,
                target_id=target_id,
                observed_at_ms=observed_at_ms,
            )
        )


def normalize_json_dataset(payload: Mapping[str, Any]) -> NormalizedDataset:
    dataset = NormalizedDataset()
    _parse_platforms(payload.get("platforms", []), dataset)
    _parse_fingerprints(payload.get("fingerprints", []), dataset)
    _parse_tickets(payload.get("tickets", []), dataset)
    _parse_quotas(payload.get("quotas", []), dataset)
    _parse_renewals(payload.get("renewals", []), dataset)
    _parse_revocations(payload.get("revocations", []), dataset)
    return dataset


def load_dataset(samples_dir: Path) -> NormalizedDataset:
    samples_dir = Path(samples_dir)
    payload = {
        "platforms": read_json(samples_dir / "platforms.json"),
        "fingerprints": read_json(samples_dir / "fingerprints.json"),
        "tickets": read_json(samples_dir / "tickets.json"),
        "renewals": read_json(samples_dir / "renewals.json"),
        "quotas": read_json(samples_dir / "quotas.json"),
        "revocations": read_json(samples_dir / "revocations.json"),
    }
    return normalize_json_dataset(payload)

