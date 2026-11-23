"""Immutable task and variant-snapshot models."""

from __future__ import annotations

import threading
import math
import errno
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .errors import (
    DomainError,
    E_INVALID_CASE,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_MISSING_CASE,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
)
from .hashing import atomic_write_json, sha256_json

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


VARIANT_NAMES = (
    "rope",
    "position_interpolation",
    "ntk_aware",
    "block_frequency",
)

VARIANT_ALIASES = {
    "rope": "rope",
    "classic": "rope",
    "classic_rope": "rope",
    "position_interpolation": "position_interpolation",
    "position-interpolation": "position_interpolation",
    "pi": "position_interpolation",
    "ntk": "ntk_aware",
    "ntk_aware": "ntk_aware",
    "ntk-aware": "ntk_aware",
    "block": "block_frequency",
    "block_freq": "block_frequency",
    "block_frequency": "block_frequency",
    "block-frequency": "block_frequency",
}

SNAPSHOT_SCHEMA = "rope-variant-snapshot-v1"


def _require(condition: bool, message: str, **context: Any) -> None:
    if not condition:
        raise DomainError(E_INVALID_CASE, message, context)


def _normalize_freq_block(value: Any) -> tuple[float, ...]:
    if isinstance(value, bool):
        raise DomainError(
            E_INVALID_CASE,
            "freq_block must be a list, an integer count, or a decay mapping",
            {"freq_block": value},
        )
    if isinstance(value, int):
        if value < 1:
            raise DomainError(
                E_INVALID_CASE,
                "integer freq_block must be a positive block count",
                {"freq_block": value},
            )
        return tuple(0.5**index for index in range(value))
    if isinstance(value, Mapping):
        count = value.get("count")
        decay = value.get("decay", 0.5)
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise DomainError(
                E_INVALID_CASE,
                "freq_block.count must be a positive integer",
                {"freq_block": value},
            )
        if not isinstance(decay, (int, float)) or isinstance(decay, bool):
            raise DomainError(
                E_INVALID_CASE,
                "freq_block.decay must be numeric",
                {"freq_block": value},
            )
        decay = float(decay)
        if not 0.0 < decay <= 1.0:
            raise DomainError(
                E_INVALID_CASE,
                "freq_block.decay must be in (0, 1]",
                {"freq_block": value},
            )
        return tuple(decay**index for index in range(count))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if not value:
            raise DomainError(
                E_INVALID_CASE,
                "freq_block list must not be empty",
                {"freq_block": value},
            )
        scales: list[float] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise DomainError(
                    E_INVALID_CASE,
                    "freq_block entries must be numeric",
                    {"freq_block": value},
                )
            scale = float(item)
            if not math.isfinite(scale) or not scale > 0.0:
                raise DomainError(
                    E_INVALID_CASE,
                    "freq_block entries must be finite and positive",
                    {"freq_block": value},
                )
            scales.append(scale)
        return tuple(scales)
    raise DomainError(
        E_INVALID_CASE,
        "unsupported freq_block representation",
        {"freq_block": value},
    )


def _normalize_variant_pair(value: Any) -> tuple[str, str]:
    if isinstance(value, str):
        normalized = value.replace("->", ":").replace(",", ":")
        parts = [part.strip() for part in normalized.split(":") if part.strip()]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        parts = [str(part).strip() for part in value]
    else:
        parts = []
    if len(parts) != 2:
        raise DomainError(
            E_INVALID_CASE,
            "variant_pair must contain exactly two variant names",
            {"variant_pair": value},
        )
    canonical_parts = [
        VARIANT_ALIASES.get(part.lower(), part.lower()) for part in parts
    ]
    if canonical_parts[0] == canonical_parts[1]:
        raise DomainError(
            E_INVALID_CASE,
            "variant_pair must contain two distinct variants",
            {"variant_pair": value},
        )
    unknown = sorted(set(canonical_parts) - set(VARIANT_NAMES))
    if unknown:
        raise DomainError(
            E_INVALID_CASE,
            f"unknown variant names: {unknown}",
            {"variant_pair": value, "unknown_variants": unknown},
        )
    return canonical_parts[0], canonical_parts[1]


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    seq_len: int
    head_dim: int
    num_heads: int
    base_theta: float
    variant_pair: tuple[str, str]
    scale_ratio: float
    freq_block: tuple[float, ...]
    block_size: int
    causal: bool
    seed: int

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CaseSpec":
        required = (
            "case_id",
            "seq_len",
            "head_dim",
            "num_heads",
            "base_theta",
            "variant_pair",
            "scale_ratio",
            "freq_block",
            "block_size",
            "causal",
            "seed",
        )
        missing = [field_name for field_name in required if field_name not in value]
        if missing:
            raise DomainError(
                E_INVALID_CASE,
                "case is missing required fields",
                {"case_id": value.get("case_id"), "missing_fields": missing},
            )

        case_id = str(value["case_id"])
        _require(bool(case_id), "case_id must not be empty", case_id=case_id)
        seq_len = value["seq_len"]
        head_dim = value["head_dim"]
        num_heads = value["num_heads"]
        block_size = value["block_size"]
        seed = value["seed"]
        for field_name, item in (
            ("seq_len", seq_len),
            ("head_dim", head_dim),
            ("num_heads", num_heads),
            ("block_size", block_size),
            ("seed", seed),
        ):
            _require(
                isinstance(item, int) and not isinstance(item, bool),
                f"{field_name} must be an integer",
                case_id=case_id,
                field=field_name,
            )
        _require(seq_len > 0, "seq_len must be positive", case_id=case_id)
        _require(head_dim > 0 and head_dim % 2 == 0, "head_dim must be even", case_id=case_id)
        _require(num_heads > 0, "num_heads must be positive", case_id=case_id)
        _require(block_size > 0, "block_size must be positive", case_id=case_id)
        _require(seed >= 0, "seed must be non-negative", case_id=case_id)

        base_theta = value["base_theta"]
        scale_ratio = value["scale_ratio"]
        for field_name, item in (("base_theta", base_theta), ("scale_ratio", scale_ratio)):
            _require(
                isinstance(item, (int, float)) and not isinstance(item, bool),
                f"{field_name} must be numeric",
                case_id=case_id,
                field=field_name,
            )
        base_theta = float(base_theta)
        scale_ratio = float(scale_ratio)
        _require(
            math.isfinite(base_theta) and base_theta > 1.0,
            "base_theta must be finite and greater than 1",
            case_id=case_id,
        )
        _require(
            math.isfinite(scale_ratio) and scale_ratio > 0.0,
            "scale_ratio must be finite and positive",
            case_id=case_id,
        )

        freq_block = _normalize_freq_block(value["freq_block"])
        half_dim = head_dim // 2
        _require(
            len(freq_block) <= half_dim,
            "freq_block count must not exceed head_dim / 2",
            case_id=case_id,
        )
        causal = value["causal"]
        _require(isinstance(causal, bool), "causal must be boolean", case_id=case_id)

        return cls(
            case_id=case_id,
            seq_len=seq_len,
            head_dim=head_dim,
            num_heads=num_heads,
            base_theta=base_theta,
            variant_pair=_normalize_variant_pair(value["variant_pair"]),
            scale_ratio=scale_ratio,
            freq_block=freq_block,
            block_size=block_size,
            causal=causal,
            seed=seed,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "seq_len": self.seq_len,
            "head_dim": self.head_dim,
            "num_heads": self.num_heads,
            "base_theta": self.base_theta,
            "variant_pair": list(self.variant_pair),
            "scale_ratio": self.scale_ratio,
            "freq_block": list(self.freq_block),
            "block_size": self.block_size,
            "causal": self.causal,
            "seed": self.seed,
        }


@dataclass(frozen=True)
class SnapshotCaseParams:
    base_theta: float
    scale_ratio: float
    freq_block: tuple[float, ...]
    block_size: int

    @classmethod
    def from_case(cls, case: CaseSpec) -> "SnapshotCaseParams":
        return cls(
            base_theta=case.base_theta,
            scale_ratio=case.scale_ratio,
            freq_block=case.freq_block,
            block_size=case.block_size,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], case_id: str) -> "SnapshotCaseParams":
        try:
            block_size = value["block_size"]
            if not isinstance(block_size, int) or isinstance(block_size, bool):
                raise ValueError("block_size must be an integer")
            params = cls(
                base_theta=float(value["base_theta"]),
                scale_ratio=float(value["scale_ratio"]),
                freq_block=_normalize_freq_block(value["freq_block"]),
                block_size=block_size,
            )
        except DomainError as exc:
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                f"snapshot case parameters are invalid: {exc.message}",
                {"case_id": case_id, "variant": "*"},
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                f"snapshot case parameters are invalid: {exc}",
                {"case_id": case_id, "variant": "*"},
            ) from exc
        if (
            not math.isfinite(params.base_theta)
            or params.base_theta <= 1.0
            or not math.isfinite(params.scale_ratio)
            or params.scale_ratio <= 0.0
            or params.block_size < 1
        ):
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                "snapshot case parameters are outside their valid domain",
                {"case_id": case_id, "variant": "*"},
            )
        return params

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_theta": self.base_theta,
            "scale_ratio": self.scale_ratio,
            "freq_block": list(self.freq_block),
            "block_size": self.block_size,
        }

    def replace(self, **changes: Any) -> "SnapshotCaseParams":
        values = self.to_dict()
        values.update(changes)
        if "freq_block" in changes:
            values["freq_block"] = list(_normalize_freq_block(values["freq_block"]))
        return SnapshotCaseParams.from_dict(values, "<override>")


@dataclass(frozen=True)
class ResolvedCase:
    case_id: str
    seq_len: int
    head_dim: int
    num_heads: int
    base_theta: float
    variant_pair: tuple[str, str]
    scale_ratio: float
    freq_block: tuple[float, ...]
    block_size: int
    causal: bool
    seed: int

    @classmethod
    def resolve(
        cls, case: CaseSpec, snapshot: "VariantSnapshot"
    ) -> "ResolvedCase":
        if case.case_id not in snapshot.cases:
            raise DomainError(
                E_SNAPSHOT_MISSING_CASE,
                "snapshot does not contain this case",
                {"case_id": case.case_id, "variant": "*"},
            )
        params = snapshot.cases[case.case_id]
        return cls(
            case_id=case.case_id,
            seq_len=case.seq_len,
            head_dim=case.head_dim,
            num_heads=case.num_heads,
            base_theta=params.base_theta,
            variant_pair=case.variant_pair,
            scale_ratio=params.scale_ratio,
            freq_block=params.freq_block,
            block_size=params.block_size,
            causal=case.causal,
            seed=case.seed,
        )


def _case_checksum(params: SnapshotCaseParams) -> str:
    return sha256_json(params.to_dict())


def _variant_checksum(variant: str, params: SnapshotCaseParams) -> str:
    return sha256_json(
        {
            "base_theta": params.base_theta,
            "scale_ratio": (
                params.scale_ratio
                if variant in {"position_interpolation", "ntk_aware"}
                else None
            ),
            "freq_block": (
                list(params.freq_block)
                if variant == "block_frequency"
                else None
            ),
            "block_size": params.block_size,
        }
    )


@dataclass(frozen=True)
class VariantSnapshot:
    version: int
    cases: Mapping[str, SnapshotCaseParams]
    case_checksums: Mapping[str, str]
    variant_checksums: Mapping[str, Mapping[str, str]]
    content_checksum: str
    schema: str = SNAPSHOT_SCHEMA

    @classmethod
    def create(
        cls,
        version: int,
        cases: Mapping[str, SnapshotCaseParams],
    ) -> "VariantSnapshot":
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                "snapshot version must be a positive integer",
                {"case_id": "*", "variant": "*"},
            )
        case_copy = {str(case_id): params for case_id, params in cases.items()}
        if not case_copy:
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                "snapshot must contain at least one case",
                {"case_id": "*", "variant": "*"},
            )
        checksums = {case_id: _case_checksum(params) for case_id, params in case_copy.items()}
        variant_checksums = {
            case_id: {
                variant: _variant_checksum(variant, params)
                for variant in VARIANT_NAMES
            }
            for case_id, params in case_copy.items()
        }
        payload = {
            "schema": SNAPSHOT_SCHEMA,
            "cases": {case_id: params.to_dict() for case_id, params in case_copy.items()},
            "case_checksums": checksums,
        }
        return cls(
            version=version,
            cases=MappingProxyType(case_copy),
            case_checksums=MappingProxyType(checksums),
            variant_checksums=MappingProxyType(
                {
                    case_id: MappingProxyType(checksums_for_case)
                    for case_id, checksums_for_case in variant_checksums.items()
                }
            ),
            content_checksum=sha256_json(payload),
        )

    @classmethod
    def from_cases(cls, cases: Iterable[CaseSpec], version: int) -> "VariantSnapshot":
        return cls.create(
            version,
            {case.case_id: SnapshotCaseParams.from_case(case) for case in cases},
        )

    @classmethod
    def with_overrides(
        cls,
        base: "VariantSnapshot",
        version: int,
        overrides: Mapping[str, Mapping[str, Any]],
    ) -> "VariantSnapshot":
        cases = dict(base.cases)
        for case_id, changes in overrides.items():
            if case_id not in cases:
                raise DomainError(
                    E_SNAPSHOT_MISSING_CASE,
                    "override references an unknown snapshot case",
                    {"case_id": case_id, "variant": "*"},
                )
            cases[case_id] = cases[case_id].replace(**changes)
        return cls.create(version, cases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "content_checksum": self.content_checksum,
            "case_checksums": dict(self.case_checksums),
            "variant_checksums": {
                case_id: dict(checksums)
                for case_id, checksums in self.variant_checksums.items()
            },
            "cases": {
                case_id: params.to_dict() for case_id, params in self.cases.items()
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "VariantSnapshot":
        if value.get("schema") != SNAPSHOT_SCHEMA:
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                "unsupported snapshot schema",
                {"case_id": "*", "variant": "*", "schema": value.get("schema")},
            )
        version = value.get("version")
        raw_cases = value.get("cases")
        declared_checksum = value.get("content_checksum")
        declared_case_checksums = value.get("case_checksums", {})
        declared_variant_checksums = value.get("variant_checksums", {})
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
            or not isinstance(raw_cases, Mapping)
            or not raw_cases
            or not isinstance(declared_checksum, str)
        ):
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                "snapshot envelope is malformed",
                {"case_id": "*", "variant": "*"},
            )
        parsed: dict[str, SnapshotCaseParams] = {}
        for case_id in sorted(raw_cases):
            parsed[case_id] = SnapshotCaseParams.from_dict(raw_cases[case_id], case_id)
        snapshot = cls.create(version, parsed)
        declared_variant_checksums = (
            declared_variant_checksums
            if isinstance(declared_variant_checksums, Mapping)
            else {}
        )
        for case_id in sorted(snapshot.cases):
            declared_for_case = declared_variant_checksums.get(case_id)
            if not isinstance(declared_for_case, Mapping):
                continue
            for variant in VARIANT_NAMES:
                declared_variant = declared_for_case.get(variant)
                if (
                    declared_variant is not None
                    and declared_variant
                    != snapshot.variant_checksums[case_id][variant]
                ):
                    raise DomainError(
                        E_SNAPSHOT_CORRUPT,
                        "snapshot variant checksum does not match its content",
                        {
                            "case_id": case_id,
                            "variant": variant,
                            "affected_variants": [
                                candidate
                                for candidate in VARIANT_NAMES
                                if declared_for_case.get(candidate)
                                != snapshot.variant_checksums[case_id][candidate]
                            ],
                        },
                    )
        declared_case_checksums = (
            declared_case_checksums if isinstance(declared_case_checksums, Mapping) else {}
        )
        for case_id, checksum in snapshot.case_checksums.items():
            if declared_case_checksums.get(case_id) != checksum:
                raise DomainError(
                    E_SNAPSHOT_CORRUPT,
                    "snapshot case checksum does not match its content",
                    {
                        "case_id": case_id,
                        "variant": "*",
                        "affected_variants": list(VARIANT_NAMES),
                    },
                )
        if snapshot.content_checksum != declared_checksum:
            first_case = next(iter(sorted(snapshot.cases)))
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                "snapshot content checksum does not match",
                {
                    "case_id": first_case,
                    "variant": "*",
                    "affected_variants": list(VARIANT_NAMES),
                    "expected_checksum": declared_checksum,
                    "actual_checksum": snapshot.content_checksum,
                },
            )
        return snapshot

    def verify(self) -> "VariantSnapshot":
        for case_id, params in self.cases.items():
            for variant in VARIANT_NAMES:
                actual_variant = _variant_checksum(variant, params)
                if (
                    self.variant_checksums.get(case_id, {}).get(variant)
                    != actual_variant
                ):
                    affected = [
                        candidate
                        for candidate in VARIANT_NAMES
                        if self.variant_checksums.get(case_id, {}).get(candidate)
                        != _variant_checksum(candidate, params)
                    ]
                    raise DomainError(
                        E_SNAPSHOT_CORRUPT,
                        "snapshot variant content has changed",
                        {
                            "case_id": case_id,
                            "variant": variant,
                            "affected_variants": affected,
                        },
                    )
        for case_id, params in self.cases.items():
            actual = _case_checksum(params)
            if self.case_checksums.get(case_id) != actual:
                raise DomainError(
                    E_SNAPSHOT_CORRUPT,
                    "snapshot case content has changed",
                    {
                        "case_id": case_id,
                        "variant": "*",
                        "affected_variants": list(VARIANT_NAMES),
                    },
                )
        payload = {
            "schema": self.schema,
            "cases": {case_id: params.to_dict() for case_id, params in self.cases.items()},
            "case_checksums": dict(self.case_checksums),
        }
        actual_checksum = sha256_json(payload)
        if actual_checksum != self.content_checksum:
            first_case = next(iter(sorted(self.cases)))
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                "snapshot content checksum has changed",
                {
                    "case_id": first_case,
                    "variant": "*",
                    "affected_variants": list(VARIANT_NAMES),
                    "expected_checksum": self.content_checksum,
                    "actual_checksum": actual_checksum,
                },
            )
        return self


class SnapshotStore:
    """Durable, atomically replaced snapshot slot."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "snapshot.json"
        self.lock_path = self.root / "snapshot.lock"

    def load(self) -> VariantSnapshot | None:
        if not self.path.exists():
            return None
        try:
            import json

            value = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                f"snapshot file cannot be read: {exc}",
                {"case_id": "*", "variant": "*"},
            ) from exc
        return VariantSnapshot.from_dict(value)

    def save(self, snapshot: VariantSnapshot) -> None:
        atomic_write_json(self.path, snapshot.to_dict())

    @contextmanager
    def try_commit_lock(self):
        """Cross-process non-blocking commit lock when fcntl is available."""

        self.root.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        locked = False
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno in {errno.EACCES, errno.EAGAIN}:
                        raise DomainError(
                            E_STAGE_BUSY,
                            "another process is committing a variant snapshot",
                            {"case_id": "*", "variant": "*"},
                        ) from exc
                    raise
                locked = True
            yield
        finally:
            if locked and fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


@dataclass(frozen=True)
class CommitReceipt:
    status: str
    version: int
    content_checksum: str
    replaced_version: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "version": self.version,
            "content_checksum": self.content_checksum,
            "replaced_version": self.replaced_version,
        }


class SnapshotStage:
    """A non-blocking, atomically published variant snapshot stage."""

    def __init__(
        self,
        store: SnapshotStore | None = None,
        initial: VariantSnapshot | None = None,
    ):
        self._store = store
        self._commit_guard = threading.Lock()
        self._active = initial
        if self._active is not None:
            self._active.verify()
        if self._active is None and self._store is not None:
            self._active = self._store.load()

    def snapshot(self) -> VariantSnapshot:
        active = self._active
        if active is None:
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                "no snapshot has been committed",
                {"case_id": "*", "variant": "*"},
            )
        return active

    def active_snapshot(self) -> VariantSnapshot | None:
        return self._active

    def commit(self, snapshot: VariantSnapshot) -> CommitReceipt:
        return self.try_commit(snapshot)

    def try_commit(self, snapshot: VariantSnapshot) -> CommitReceipt:
        if not self._commit_guard.acquire(blocking=False):
            raise DomainError(
                E_STAGE_BUSY,
                "another variant snapshot commit is in progress",
                {"case_id": "*", "variant": "*"},
            )
        try:
            snapshot.verify()
            store_lock = (
                self._store.try_commit_lock()
                if self._store is not None
                else nullcontext()
            )
            with store_lock:
                current = self._active
                if self._store is not None:
                    durable = self._store.load()
                    if durable is not None and (
                        current is None or durable.version > current.version
                    ):
                        current = durable
                        self._active = durable
                if current is not None:
                    if snapshot.version < current.version:
                        raise DomainError(
                            E_SNAPSHOT_ROLLBACK,
                            "snapshot version would move backwards",
                            {
                                "case_id": "*",
                                "variant": "*",
                                "current_version": current.version,
                                "requested_version": snapshot.version,
                            },
                        )
                    if snapshot.version == current.version:
                        if snapshot.content_checksum != current.content_checksum:
                            raise DomainError(
                                E_SNAPSHOT_CORRUPT,
                                "same version cannot identify different snapshot content",
                                {
                                    "case_id": "*",
                                    "variant": "*",
                                    "version": snapshot.version,
                                },
                            )
                        return CommitReceipt(
                            status="unchanged",
                            version=snapshot.version,
                            content_checksum=snapshot.content_checksum,
                            replaced_version=None,
                        )

                self._before_publish(snapshot)
                if self._store is not None:
                    self._store.save(snapshot)
                previous_version = current.version if current is not None else None
                self._active = snapshot
                return CommitReceipt(
                    status="committed",
                    version=snapshot.version,
                    content_checksum=snapshot.content_checksum,
                    replaced_version=previous_version,
                )
        finally:
            self._commit_guard.release()

    def _before_publish(self, snapshot: VariantSnapshot) -> None:
        """Test hook invoked while the commit guard is held."""

