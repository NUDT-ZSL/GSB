from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from quota_ledger.errors import fail
from quota_ledger.models import (
    Application,
    Config,
    Dataset,
    Observation,
    PolicyParams,
    Release,
    ResourceDependency,
    Snapshot,
    Tombstone,
)
from quota_ledger.util import (
    MONEY_SCALE,
    digest_json,
    load_json,
    parse_decimal,
    quota_micros,
    ratio_fraction,
    require_dict,
    require_int,
    require_list,
    require_text,
)


def _read_list_file(data_dir: Path, names: tuple[str, ...], label: str) -> list[Any]:
    for name in names:
        path = data_dir / name
        if path.exists():
            payload = load_json(path)
            if isinstance(payload, dict) and label in payload:
                payload = payload[label]
            return require_list(payload, str(path), label)
    fail("E_MISSING_FILE", f"missing required data file: {' or '.join(names)}", str(data_dir))


def _parse_configs(data_dir: Path) -> tuple[Config, ...]:
    payload = _read_list_file(
        data_dir, ("quota_configs.json", "configs.json"), "configs"
    )
    configs: list[Config] = []
    seen_tenants: set[str] = set()
    for index, raw in enumerate(payload):
        path = f"configs[{index}]"
        item = require_dict(raw, path, "config")
        tenant = require_text(item.get("tenant"), f"{path}.tenant")
        if tenant in seen_tenants:
            fail("E_DUPLICATE_TENANT", f"duplicate tenant: {tenant}", f"{path}.tenant")
        seen_tenants.add(tenant)
        resource_key = require_text(item.get("resource_key"), f"{path}.resource_key")
        baseline_micros, baseline_error, baseline_rounding = quota_micros(
            item.get("baseline_quota"),
            f"{path}.baseline_quota",
            positive=True,
            direction="up",
        )
        elasticity, elasticity_text = ratio_fraction(
            item.get("elasticity_ratio"), f"{path}.elasticity_ratio"
        )
        lease_duration_ms = require_int(
            item.get("lease_duration_ms"), f"{path}.lease_duration_ms", minimum=1
        )
        quota_generation = require_int(
            item.get("quota_generation"), f"{path}.quota_generation", minimum=0
        )
        dependencies_raw = item.get("depends_on_resource_keys", [])
        depends_on = tuple(
            require_text(value, f"{path}.depends_on_resource_keys[{dep_index}]")
            for dep_index, value in enumerate(
                require_list(dependencies_raw, f"{path}.depends_on_resource_keys")
            )
        )
        configs.append(
            Config(
                tenant=tenant,
                resource_key=resource_key,
                baseline_micros=baseline_micros,
                baseline_error_micros=baseline_error,
                baseline_rounding=baseline_rounding,
                baseline_input=str(item.get("baseline_quota")),
                elasticity=elasticity,
                elasticity_text=elasticity_text,
                lease_duration_ms=lease_duration_ms,
                quota_generation=quota_generation,
                depends_on_resource_keys=depends_on,
            )
        )
    return tuple(configs)


def _parse_applications(
    data_dir: Path, config_by_tenant: dict[str, Config], resource_keys: frozenset[str]
) -> tuple[Application, ...]:
    payload = _read_list_file(
        data_dir, ("applications.json", "application_trace.json"), "applications"
    )
    applications: list[Application] = []
    seen_seq: set[int] = set()
    for index, raw in enumerate(payload):
        path = f"applications[{index}]"
        item = require_dict(raw, path, "application")
        application_seq = require_int(
            item.get("application_seq"), f"{path}.application_seq", minimum=1
        )
        if application_seq in seen_seq:
            fail(
                "E_DUPLICATE_APPLICATION_SEQ",
                f"duplicate application sequence: {application_seq}",
                f"{path}.application_seq",
            )
        seen_seq.add(application_seq)
        tenant = require_text(item.get("tenant"), f"{path}.tenant")
        config = config_by_tenant.get(tenant)
        if config is None:
            fail("E_UNKNOWN_TENANT", f"unknown tenant: {tenant}", f"{path}.tenant")
        resource_key = require_text(item.get("resource_key"), f"{path}.resource_key")
        if resource_key not in resource_keys:
            fail(
                "E_RESOURCE_KEY_NOT_CLOSED",
                f"unknown resource key: {resource_key}",
                f"{path}.resource_key",
            )
        if resource_key != config.resource_key:
            fail(
                "E_RESOURCE_TENANT_MISMATCH",
                f"tenant {tenant} is bound to {config.resource_key}, not {resource_key}",
                f"{path}.resource_key",
            )
        requested_micros, requested_error, requested_rounding = quota_micros(
            item.get("requested_quota"),
            f"{path}.requested_quota",
            positive=True,
            direction="up",
        )
        logical_clock_ms = require_int(
            item.get("logical_clock_ms"), f"{path}.logical_clock_ms", minimum=0
        )
        priority = require_int(item.get("priority"), f"{path}.priority", minimum=0)
        applications.append(
            Application(
                application_seq=application_seq,
                tenant=tenant,
                resource_key=resource_key,
                requested_micros=requested_micros,
                requested_error_micros=requested_error,
                requested_rounding=requested_rounding,
                requested_input=str(item.get("requested_quota")),
                logical_clock_ms=logical_clock_ms,
                priority=priority,
            )
        )
    return tuple(applications)


def _parse_releases(
    data_dir: Path,
    applications_by_seq: dict[int, Application],
    config_by_tenant: dict[str, Config],
) -> tuple[Release, ...]:
    path = data_dir / "releases.json"
    if not path.exists():
        return ()
    payload = load_json(path)
    if isinstance(payload, dict) and "releases" in payload:
        payload = payload["releases"]
    payload = require_list(payload, str(path), "releases")
    releases: list[Release] = []
    seen_seq: set[int] = set()
    seen_application: set[int] = set()
    for index, raw in enumerate(payload):
        item_path = f"releases[{index}]"
        item = require_dict(raw, item_path, "release")
        release_seq = require_int(
            item.get("release_seq"), f"{item_path}.release_seq", minimum=1
        )
        if release_seq in seen_seq:
            fail(
                "E_DUPLICATE_RELEASE_SEQ",
                f"duplicate release sequence: {release_seq}",
                f"{item_path}.release_seq",
            )
        seen_seq.add(release_seq)
        application_seq = require_int(
            item.get("application_seq"), f"{item_path}.application_seq", minimum=1
        )
        if application_seq in seen_application:
            fail(
                "E_DUPLICATE_RELEASE_APPLICATION",
                f"application {application_seq} has more than one release",
                f"{item_path}.application_seq",
            )
        seen_application.add(application_seq)
        application = applications_by_seq.get(application_seq)
        if application is None:
            fail(
                "E_UNKNOWN_APPLICATION_SEQ",
                f"release references unknown application: {application_seq}",
                f"{item_path}.application_seq",
            )
        tenant = require_text(item.get("tenant"), f"{item_path}.tenant")
        resource_key = require_text(item.get("resource_key"), f"{item_path}.resource_key")
        config = config_by_tenant.get(tenant)
        if config is None:
            fail("E_UNKNOWN_TENANT", f"unknown tenant: {tenant}", f"{item_path}.tenant")
        if tenant != application.tenant or resource_key != application.resource_key:
            fail(
                "E_RELEASE_APPLICATION_MISMATCH",
                "release tenant/resource does not match application",
                item_path,
            )
        actual_micros, actual_error, actual_rounding = quota_micros(
            item.get("actual_quota"),
            f"{item_path}.actual_quota",
            nonnegative=True,
            direction="down",
        )
        logical_clock_ms = require_int(
            item.get("logical_clock_ms"), f"{item_path}.logical_clock_ms", minimum=0
        )
        if logical_clock_ms < application.logical_clock_ms:
            fail(
                "E_RELEASE_BEFORE_APPLICATION",
                "release clock precedes application clock",
                f"{item_path}.logical_clock_ms",
            )
        releases.append(
            Release(
                release_seq=release_seq,
                application_seq=application_seq,
                tenant=tenant,
                resource_key=resource_key,
                actual_micros=actual_micros,
                actual_error_micros=actual_error,
                actual_rounding=actual_rounding,
                actual_input=str(item.get("actual_quota")),
                logical_clock_ms=logical_clock_ms,
            )
        )
    return tuple(releases)


def _parse_observations(
    data_dir: Path,
    config_by_tenant: dict[str, Config],
    resource_keys: frozenset[str],
) -> tuple[Observation, ...]:
    payload = _read_list_file(
        data_dir, ("observations.json", "drift_observations.json"), "observations"
    )
    observations: list[Observation] = []
    seen_seq: set[int] = set()
    for index, raw in enumerate(payload):
        path = f"observations[{index}]"
        item = require_dict(raw, path, "observation")
        observation_seq = require_int(
            item.get("observation_seq"), f"{path}.observation_seq", minimum=1
        )
        if observation_seq in seen_seq:
            fail(
                "E_DUPLICATE_OBSERVATION_SEQ",
                f"duplicate observation sequence: {observation_seq}",
                f"{path}.observation_seq",
            )
        seen_seq.add(observation_seq)
        tenant = require_text(item.get("tenant"), f"{path}.tenant")
        config = config_by_tenant.get(tenant)
        if config is None:
            fail("E_UNKNOWN_TENANT", f"unknown tenant: {tenant}", f"{path}.tenant")
        resource_key = config.resource_key
        if resource_key not in resource_keys:
            fail(
                "E_RESOURCE_KEY_NOT_CLOSED",
                f"tenant resource key is not closed: {resource_key}",
                f"{path}.tenant",
            )
        actual_micros, actual_error, actual_rounding = quota_micros(
            item.get("actual_occupancy"),
            f"{path}.actual_occupancy",
            nonnegative=True,
            direction="down",
        )
        logical_clock_ms = require_int(
            item.get("logical_clock_ms"), f"{path}.logical_clock_ms", minimum=0
        )
        observations.append(
            Observation(
                observation_seq=observation_seq,
                tenant=tenant,
                resource_key=resource_key,
                actual_occupancy_micros=actual_micros,
                actual_error_micros=actual_error,
                actual_rounding=actual_rounding,
                actual_input=str(item.get("actual_occupancy")),
                logical_clock_ms=logical_clock_ms,
            )
        )
    return tuple(observations)


def _parse_params(raw: dict[str, Any], path: str) -> PolicyParams:
    global_budget, global_error, global_rounding = quota_micros(
        raw.get("global_quota_budget"),
        f"{path}.global_quota_budget",
        positive=True,
        direction="up",
    )
    if global_error or global_rounding != "none":
        fail(
            "E_BUDGET_PRECISION",
            "global_quota_budget must be representable at scale 1e6",
            f"{path}.global_quota_budget",
        )
    round_budget, round_error, round_rounding = quota_micros(
        raw.get("round_threshold_budget"),
        f"{path}.round_threshold_budget",
        positive=True,
        direction="up",
    )
    if round_error or round_rounding != "none":
        fail(
            "E_BUDGET_PRECISION",
            "round_threshold_budget must be representable at scale 1e6",
            f"{path}.round_threshold_budget",
        )
    per_cap, cap_error, cap_rounding = quota_micros(
        raw.get("per_tenant_adjustment_cap"),
        f"{path}.per_tenant_adjustment_cap",
        positive=True,
        direction="up",
    )
    if cap_error or cap_rounding != "none":
        fail(
            "E_BUDGET_PRECISION",
            "per_tenant_adjustment_cap must be representable at scale 1e6",
            f"{path}.per_tenant_adjustment_cap",
        )
    safety, safety_error, safety_rounding = quota_micros(
        raw.get("safety_watermark"),
        f"{path}.safety_watermark",
        nonnegative=True,
        direction="down",
    )
    if safety_error or safety_rounding != "none":
        fail(
            "E_BUDGET_PRECISION",
            "safety_watermark must be representable at scale 1e6",
            f"{path}.safety_watermark",
        )
    drift, drift_text = ratio_fraction(
        raw.get("drift_threshold_ratio"), f"{path}.drift_threshold_ratio"
    )
    if drift > 1:
        fail(
            "E_RATIO_OUT_OF_RANGE",
            "drift_threshold_ratio must be <= 1",
            f"{path}.drift_threshold_ratio",
        )
    if "danger_threshold_ratio" in raw:
        danger, danger_text = ratio_fraction(
            raw.get("danger_threshold_ratio"), f"{path}.danger_threshold_ratio"
        )
    else:
        danger = drift * 2
        danger_text = str(Decimal(danger.numerator) / Decimal(danger.denominator))
    if danger > 1:
        fail(
            "E_RATIO_OUT_OF_RANGE",
            "danger_threshold_ratio must be <= 1",
            f"{path}.danger_threshold_ratio",
        )
    if round_budget > global_budget:
        fail(
            "E_BUDGET_RELATION",
            "round_threshold_budget must not exceed global_quota_budget",
            path,
        )
    if per_cap > global_budget:
        fail(
            "E_BUDGET_RELATION",
            "per_tenant_adjustment_cap must not exceed global_quota_budget",
            path,
        )
    return PolicyParams(
        global_quota_budget_micros=global_budget,
        round_threshold_budget_micros=round_budget,
        per_tenant_adjustment_cap_micros=per_cap,
        safety_watermark_micros=safety,
        drift_threshold_ratio=drift,
        drift_threshold_text=drift_text,
        danger_threshold_ratio=danger,
        danger_threshold_text=danger_text,
    )


def _parse_threshold_table(
    raw: Any, *, path: str, config_by_tenant: dict[str, Config]
) -> dict[str, int]:
    if raw is None:
        return {}
    table = require_dict(raw, path, "threshold table")
    thresholds: dict[str, int] = {}
    for tenant, value in table.items():
        tenant = require_text(tenant, f"{path}.<tenant>")
        config = config_by_tenant.get(tenant)
        if config is None:
            fail("E_UNKNOWN_TENANT", f"threshold references unknown tenant: {tenant}", path)
        threshold_micros, threshold_error, threshold_rounding = quota_micros(
            value,
            f"{path}.{tenant}",
            nonnegative=True,
            direction="up",
        )
        if threshold_error or threshold_rounding != "none":
            fail(
                "E_THRESHOLD_PRECISION",
                "threshold must be representable at scale 1e6",
                f"{path}.{tenant}",
            )
        thresholds[tenant] = threshold_micros
    return thresholds


def _parse_v1(path: Path, config_by_tenant: dict[str, Config]) -> Snapshot:
    raw = require_dict(load_json(path), str(path), "v1 snapshot")
    version = require_text(raw.get("version"), f"{path}.version")
    if version != "v1":
        fail("E_SNAPSHOT_VERSION", "v1 file must declare version=v1", f"{path}.version")
    snapshot_id = require_text(raw.get("snapshot_id"), f"{path}.snapshot_id")
    generation = require_int(raw.get("generation"), f"{path}.generation", minimum=1)
    created_clock_ms = require_int(
        raw.get("created_clock_ms"), f"{path}.created_clock_ms", minimum=0
    )
    relative_seconds = parse_decimal(
        raw.get("effective_after_seconds"),
        f"{path}.effective_after_seconds",
        nonnegative=True,
    )
    relative_ms = relative_seconds * 1000
    if relative_ms != relative_ms.to_integral_value():
        fail(
            "E_V1_TIME_PRECISION",
            "effective_after_seconds must resolve to whole milliseconds",
            f"{path}.effective_after_seconds",
        )
    effective_clock_ms = created_clock_ms + int(relative_ms)
    params = _parse_params(raw, str(path))
    thresholds = _parse_threshold_table(
        raw.get("thresholds"), path=f"{path}.thresholds", config_by_tenant=config_by_tenant
    )
    policy_id = f"v1:{snapshot_id}:{generation}"
    snapshot = Snapshot(
        policy_id=policy_id,
        snapshot_id=snapshot_id,
        version="v1",
        generation=generation,
        created_clock_ms=created_clock_ms,
        effective_clock_ms=effective_clock_ms,
        params=params,
        thresholds=thresholds,
        tombstones=(),
        dependencies=(),
        shards=(),
        source_path=str(path),
        fingerprint="",
    )
    return _with_fingerprint(snapshot)


def _read_v2_sqlite(
    metadata_path: Path,
    raw: dict[str, Any],
    config_by_tenant: dict[str, Config],
) -> tuple[dict[str, int], tuple[Tombstone, ...], tuple[ResourceDependency, ...]]:
    if not metadata_path.exists():
        fail("E_V2_METADATA", "v2 metadata SQLite database is missing", str(metadata_path))
    try:
        connection = sqlite3.connect(f"file:{metadata_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        fail("E_V2_METADATA", f"cannot open v2 metadata: {exc}", str(metadata_path))
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
        }
        for required in ("thresholds", "tombstones"):
            if required not in tables:
                fail(
                    "E_V2_METADATA",
                    f"v2 metadata is missing table: {required}",
                    str(metadata_path),
                )
        if "policy_meta" in tables:
            rows = connection.execute("SELECT * FROM policy_meta").fetchall()
            if len(rows) != 1:
                fail(
                    "E_V2_METADATA",
                    "policy_meta must contain exactly one row",
                    str(metadata_path),
                )
            meta = rows[0]
            expected = {
                "snapshot_id": raw.get("snapshot_id"),
                "version": raw.get("version"),
                "generation": raw.get("generation"),
                "effective_clock_ms": raw.get("effective_clock_ms"),
            }
            for key, value in expected.items():
                if key in meta.keys() and meta[key] != value:
                    fail(
                        "E_V2_METADATA_MISMATCH",
                        f"policy_meta.{key} does not match policy JSON",
                        str(metadata_path),
                        expected=value,
                        actual=meta[key],
                    )
        thresholds: dict[str, int] = {}
        for row in connection.execute(
            "SELECT tenant, resource_key, threshold_quota FROM thresholds"
        ).fetchall():
            tenant = require_text(
                row["tenant"], f"{metadata_path}.thresholds.tenant"
            )
            resource_key = require_text(
                row["resource_key"], f"{metadata_path}.thresholds.resource_key"
            )
            config = config_by_tenant.get(tenant)
            if config is None:
                fail(
                    "E_UNKNOWN_TENANT",
                    f"v2 threshold references unknown tenant: {tenant}",
                    str(metadata_path),
                )
            if resource_key != config.resource_key:
                fail(
                    "E_RESOURCE_TENANT_MISMATCH",
                    f"v2 threshold resource does not match tenant {tenant}",
                    str(metadata_path),
                )
            if tenant in thresholds:
                fail(
                    "E_DUPLICATE_THRESHOLD",
                    f"duplicate v2 threshold for tenant: {tenant}",
                    str(metadata_path),
                )
            threshold, threshold_error, threshold_rounding = quota_micros(
                row["threshold_quota"],
                f"{metadata_path}.thresholds.{tenant}",
                nonnegative=True,
                direction="up",
            )
            if threshold_error or threshold_rounding != "none":
                fail(
                    "E_THRESHOLD_PRECISION",
                    "v2 threshold must be representable at scale 1e6",
                    str(metadata_path),
                )
            thresholds[tenant] = threshold
        tombstones: list[Tombstone] = []
        tombstone_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tombstones)")
        }
        required_columns = {
            "target_snapshot_id",
            "target_version",
            "tenant",
            "resource_key",
            "invalidated_generation",
            "effective_clock_ms",
            "reason",
        }
        if not required_columns.issubset(tombstone_columns):
            fail(
                "E_V2_METADATA",
                "tombstones table is missing required columns",
                str(metadata_path),
                missing=sorted(required_columns - tombstone_columns),
            )
        for row in connection.execute(
            "SELECT target_snapshot_id, target_version, tenant, resource_key, "
            "invalidated_generation, effective_clock_ms, reason FROM tombstones"
        ).fetchall():
            tenant = require_text(row["tenant"], f"{metadata_path}.tombstones.tenant")
            config = config_by_tenant.get(tenant)
            if config is None:
                fail(
                    "E_UNKNOWN_TENANT",
                    f"tombstone references unknown tenant: {tenant}",
                    str(metadata_path),
                )
            resource_key = require_text(
                row["resource_key"], f"{metadata_path}.tombstones.resource_key"
            )
            if resource_key != config.resource_key:
                fail(
                    "E_RESOURCE_TENANT_MISMATCH",
                    f"tombstone resource does not match tenant {tenant}",
                    str(metadata_path),
                )
            target_snapshot_id = require_text(
                row["target_snapshot_id"],
                f"{metadata_path}.tombstones.target_snapshot_id",
            )
            target_version = require_text(
                row["target_version"], f"{metadata_path}.tombstones.target_version"
            )
            if target_version not in {"v1", "v2"}:
                fail(
                    "E_SNAPSHOT_VERSION",
                    "tombstone target_version must be v1 or v2",
                    str(metadata_path),
                )
            invalidated_generation = require_int(
                row["invalidated_generation"],
                f"{metadata_path}.tombstones.invalidated_generation",
                minimum=1,
            )
            effective_clock_ms = require_int(
                row["effective_clock_ms"],
                f"{metadata_path}.tombstones.effective_clock_ms",
                minimum=0,
            )
            reason = require_text(row["reason"], f"{metadata_path}.tombstones.reason")
            tombstones.append(
                Tombstone(
                    target_snapshot_id=target_snapshot_id,
                    target_version=target_version,
                    tenant=tenant,
                    resource_key=resource_key,
                    invalidated_generation=invalidated_generation,
                    effective_clock_ms=effective_clock_ms,
                    reason=reason,
                )
            )
        dependencies: list[ResourceDependency] = []
        if "resource_dependencies" in tables:
            for row in connection.execute(
                "SELECT resource_key, depends_on FROM resource_dependencies"
            ).fetchall():
                resource_key = require_text(
                    row["resource_key"],
                    f"{metadata_path}.resource_dependencies.resource_key",
                )
                depends_on = require_text(
                    row["depends_on"],
                    f"{metadata_path}.resource_dependencies.depends_on",
                )
                dependencies.append(
                    ResourceDependency(resource_key=resource_key, depends_on=depends_on)
                )
    except sqlite3.Error as exc:
        fail("E_V2_METADATA", f"cannot read v2 metadata: {exc}", str(metadata_path))
    finally:
        connection.close()
    return thresholds, tuple(tombstones), tuple(dependencies)


def _parse_v2(
    path: Path, config_by_tenant: dict[str, Config], resource_keys: frozenset[str]
) -> Snapshot:
    raw = require_dict(load_json(path), str(path), "v2 snapshot")
    version = require_text(raw.get("version"), f"{path}.version")
    if version != "v2":
        fail("E_SNAPSHOT_VERSION", "v2 file must declare version=v2", f"{path}.version")
    snapshot_id = require_text(raw.get("snapshot_id"), f"{path}.snapshot_id")
    generation = require_int(raw.get("generation"), f"{path}.generation", minimum=1)
    created_clock_ms = require_int(
        raw.get("created_clock_ms"), f"{path}.created_clock_ms", minimum=0
    )
    effective_clock_ms = require_int(
        raw.get("effective_clock_ms"), f"{path}.effective_clock_ms", minimum=0
    )
    if effective_clock_ms < created_clock_ms:
        fail(
            "E_V2_TIME_ORDER",
            "effective_clock_ms must be >= created_clock_ms",
            f"{path}.effective_clock_ms",
        )
    metadata_name = require_text(raw.get("metadata_db"), f"{path}.metadata_db")
    metadata_path = path.parent / metadata_name
    thresholds, tombstones, dependencies = _read_v2_sqlite(
        metadata_path, raw, config_by_tenant
    )
    params = _parse_params(raw, str(path))
    shard_entries: list[dict[str, Any]] = []
    shard_paths = require_list(raw.get("shards", []), f"{path}.shards")
    for index, shard_name in enumerate(shard_paths):
        shard_name = require_text(shard_name, f"{path}.shards[{index}]")
        shard_path = path.parent / shard_name
        shard_raw = require_dict(
            load_json(shard_path), str(shard_path), "v2 shard"
        )
        resource_key = require_text(
            shard_raw.get("resource_key"), f"{shard_path}.resource_key"
        )
        if resource_key not in resource_keys:
            fail(
                "E_RESOURCE_KEY_NOT_CLOSED",
                f"v2 shard references unknown resource key: {resource_key}",
                str(shard_path),
            )
        shard_entries.append(
            {
                "file": shard_name,
                "resource_key": resource_key,
                "payload": shard_raw,
            }
        )
    for dependency in dependencies:
        if dependency.resource_key not in resource_keys:
            fail(
                "E_RESOURCE_KEY_NOT_CLOSED",
                f"dependency references unknown resource key: {dependency.resource_key}",
                str(metadata_path),
            )
        if dependency.depends_on not in resource_keys:
            fail(
                "E_RESOURCE_KEY_NOT_CLOSED",
                f"dependency references unknown resource key: {dependency.depends_on}",
                str(metadata_path),
            )
    policy_id = f"v2:{snapshot_id}:{generation}"
    snapshot = Snapshot(
        policy_id=policy_id,
        snapshot_id=snapshot_id,
        version="v2",
        generation=generation,
        created_clock_ms=created_clock_ms,
        effective_clock_ms=effective_clock_ms,
        params=params,
        thresholds=thresholds,
        tombstones=tombstones,
        dependencies=dependencies,
        shards=tuple(shard_entries),
        source_path=str(path),
        fingerprint="",
    )
    return _with_fingerprint(snapshot)


def _with_fingerprint(snapshot: Snapshot) -> Snapshot:
    payload = snapshot.as_canonical()
    payload.pop("fingerprint", None)
    return Snapshot(
        policy_id=snapshot.policy_id,
        snapshot_id=snapshot.snapshot_id,
        version=snapshot.version,
        generation=snapshot.generation,
        created_clock_ms=snapshot.created_clock_ms,
        effective_clock_ms=snapshot.effective_clock_ms,
        params=snapshot.params,
        thresholds=snapshot.thresholds,
        tombstones=snapshot.tombstones,
        dependencies=snapshot.dependencies,
        shards=snapshot.shards,
        source_path=snapshot.source_path,
        fingerprint=digest_json(payload),
    )


def _discover_snapshot_files(data_dir: Path) -> tuple[list[Path], list[Path]]:
    v1_dir = data_dir / "snapshots" / "v1"
    v1_files = sorted(v1_dir.glob("*.json")) if v1_dir.is_dir() else []
    v2_files = sorted((data_dir / "snapshots" / "v2").glob("*/policy.json"))
    direct_v2 = data_dir / "snapshots" / "v2" / "policy.json"
    if direct_v2.exists():
        v2_files.append(direct_v2)
    return v1_files, sorted(v2_files)


def _validate_resource_closures(
    configs: tuple[Config, ...],
    snapshots: tuple[Snapshot, ...],
    resource_keys: frozenset[str],
) -> None:
    graph: dict[str, set[str]] = {key: set() for key in resource_keys}
    for config in configs:
        for dependency in config.depends_on_resource_keys:
            if dependency not in resource_keys:
                fail(
                    "E_RESOURCE_KEY_NOT_CLOSED",
                    f"config dependency references unknown resource key: {dependency}",
                    config.tenant,
                )
            graph[config.resource_key].add(dependency)
    for snapshot in snapshots:
        for dependency in snapshot.dependencies:
            graph[dependency.resource_key].add(dependency.depends_on)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, stack: list[str]) -> None:
        if node in visiting:
            fail(
                "E_RESOURCE_DEPENDENCY_CYCLE",
                "resource dependency cycle: " + " -> ".join(stack + [node]),
                "resource_dependencies",
            )
        if node in visited:
            return
        visiting.add(node)
        for adjacent in sorted(graph.get(node, ())):
            visit(adjacent, stack + [node])
        visiting.remove(node)
        visited.add(node)

    for key in sorted(graph):
        visit(key, [])


def _validate_snapshots(
    snapshots: tuple[Snapshot, ...],
    config_by_tenant: dict[str, Config],
    resource_keys: frozenset[str],
) -> None:
    seen_ids: set[str] = set()
    for snapshot in snapshots:
        if snapshot.policy_id in seen_ids:
            fail(
                "E_DUPLICATE_POLICY",
                f"duplicate policy id: {snapshot.policy_id}",
                snapshot.source_path,
            )
        seen_ids.add(snapshot.policy_id)
        for tenant in snapshot.thresholds:
            config = config_by_tenant.get(tenant)
            if config is None:
                fail("E_UNKNOWN_TENANT", f"unknown tenant: {tenant}", snapshot.source_path)
            if config.resource_key not in resource_keys:
                fail(
                    "E_RESOURCE_KEY_NOT_CLOSED",
                    f"resource key is not closed: {config.resource_key}",
                    snapshot.source_path,
                )
        if sum(snapshot.thresholds.values()) > snapshot.params.global_quota_budget_micros:
            fail(
                "E_THRESHOLD_SUM_EXCEEDS_BUDGET",
                "snapshot thresholds exceed global_quota_budget",
                snapshot.source_path,
                threshold_micros=sum(snapshot.thresholds.values()),
                budget_micros=snapshot.params.global_quota_budget_micros,
            )
    snapshot_ids = {item.snapshot_id: item for item in snapshots}
    for snapshot in snapshots:
        for tombstone in snapshot.tombstones:
            target = snapshot_ids.get(tombstone.target_snapshot_id)
            if target is None:
                fail(
                    "E_TOMBSTONE_TARGET",
                    f"tombstone targets unknown snapshot: {tombstone.target_snapshot_id}",
                    snapshot.source_path,
                )
            if target.version != tombstone.target_version:
                fail(
                    "E_TOMBSTONE_TARGET",
                    "tombstone target version mismatch",
                    snapshot.source_path,
                )
            if tombstone.invalidated_generation != target.generation:
                fail(
                    "E_TOMBSTONE_GENERATION",
                    "tombstone invalidated_generation does not match target generation",
                    snapshot.source_path,
                )
            if tombstone.tenant not in target.thresholds:
                fail(
                    "E_TOMBSTONE_THRESHOLD",
                    "tombstone targets a tenant absent from target snapshot",
                    snapshot.source_path,
                )
    ordered = sorted(
        snapshots,
        key=lambda item: (
            item.created_clock_ms,
            item.effective_clock_ms,
            item.generation,
            item.version_rank,
            item.snapshot_id,
        ),
    )
    previous_generation = 0
    previous_created = -1
    previous_effective = -1
    for snapshot in ordered:
        if snapshot.generation <= previous_generation:
            fail(
                "E_SNAPSHOT_GENERATION_ORDER",
                "snapshot generations must strictly increase in logical order",
                snapshot.source_path,
                previous=previous_generation,
                actual=snapshot.generation,
            )
        if snapshot.created_clock_ms < previous_created:
            fail(
                "E_SNAPSHOT_CLOCK_ORDER",
                "snapshot created clocks must be monotonic",
                snapshot.source_path,
            )
        if snapshot.effective_clock_ms < previous_effective:
            fail(
                "E_SNAPSHOT_CLOCK_ORDER",
                "snapshot effective clocks must be monotonic",
                snapshot.source_path,
            )
        previous_generation = snapshot.generation
        previous_created = snapshot.created_clock_ms
        previous_effective = snapshot.effective_clock_ms


def load_dataset(data_dir: str | Path) -> Dataset:
    data_dir = Path(data_dir).resolve()
    if not data_dir.is_dir():
        fail("E_DATA_DIR", f"data directory does not exist: {data_dir}", str(data_dir))
    configs = _parse_configs(data_dir)
    if not configs:
        fail("E_EMPTY_DATA", "config set must not be empty", str(data_dir))
    config_by_tenant = {item.tenant: item for item in configs}
    resource_keys = frozenset(item.resource_key for item in configs)
    applications = _parse_applications(data_dir, config_by_tenant, resource_keys)
    applications_by_seq = {item.application_seq: item for item in applications}
    releases = _parse_releases(data_dir, applications_by_seq, config_by_tenant)
    observations = _parse_observations(data_dir, config_by_tenant, resource_keys)
    v1_files, v2_files = _discover_snapshot_files(data_dir)
    snapshots: list[Snapshot] = []
    for path in v1_files:
        snapshots.append(_parse_v1(path, config_by_tenant))
    for path in v2_files:
        snapshots.append(_parse_v2(path, config_by_tenant, resource_keys))
    if snapshots:
        _validate_snapshots(tuple(snapshots), config_by_tenant, resource_keys)
        _validate_resource_closures(configs, tuple(snapshots), resource_keys)
    else:
        _validate_resource_closures(configs, (), resource_keys)
    round_span_ms = 1000
    dataset_meta_path = data_dir / "dataset.json"
    if dataset_meta_path.exists():
        raw_meta = require_dict(
            load_json(dataset_meta_path), str(dataset_meta_path), "dataset metadata"
        )
        round_span_ms = require_int(
            raw_meta.get("round_span_ms", 1000),
            f"{dataset_meta_path}.round_span_ms",
            minimum=1,
        )
    dataset = Dataset(
        data_dir=data_dir,
        configs=configs,
        applications=applications,
        releases=releases,
        observations=observations,
        snapshots=tuple(snapshots),
        round_span_ms=round_span_ms,
        digest="",
    )
    dataset_digest = digest_json(dataset.as_canonical())
    dataset = Dataset(
        data_dir=dataset.data_dir,
        configs=dataset.configs,
        applications=dataset.applications,
        releases=dataset.releases,
        observations=dataset.observations,
        snapshots=dataset.snapshots,
        round_span_ms=dataset.round_span_ms,
        digest=dataset_digest,
    )
    return dataset

