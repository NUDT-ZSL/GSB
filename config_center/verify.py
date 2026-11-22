from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import BusinessError
from .fixed import convert_decl, stable_decimal
from .models import ConfigDecl, normalize_patch, normalize_snapshot, snapshot_digest
from .service import ConfigCenter
from .store import now_utc
from .util import read_json, sha256_json


def _decimal(text: Any) -> Decimal:
    return Decimal(str(text))


def _check(name: str, status: str, detail: Any, *, code: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "code": code,
        "detail": detail,
    }


def verify_center(
    center: ConfigCenter,
    *,
    namespace: str | None = None,
    tolerance: Decimal = Decimal("0.001"),
    scale_factor: int = 100,
    compute_input_path: str | Path | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    pointers = center.store.list_pointers()
    selected_namespaces = (
        [namespace]
        if namespace is not None
        else sorted({pointer["namespace"] for pointer in pointers})
    )
    if not selected_namespaces and namespace is not None:
        selected_namespaces = [namespace]

    for ns in selected_namespaces:
        checks.append(_verify_version_chain(center, ns))
        checks.append(_verify_conversion_replay(center, ns))
        checks.append(_verify_results(center, ns))
        checks.append(_verify_pointer_isolation(center, ns))
    if not selected_namespaces:
        checks.append(
            _check(
                "version_chain",
                "fail",
                "配置中心尚未初始化，不存在当前版本指针",
                code="E_NOT_INITIALIZED",
            )
        )

    error_contract = _verify_error_contract(center)
    checks.append(error_contract)

    numeric: dict[str, Any] | None = None
    sensitivity: dict[str, Any] | None = None
    if selected_namespaces:
        ns = selected_namespaces[0]
        input_path = (
            Path(compute_input_path)
            if compute_input_path is not None
            else center.data_dir / "compute_input.json"
        )
        if input_path.exists():
            raw_input = read_json(input_path)
            versions = center.store.list_versions(ns)
            if versions:
                latest = int(versions[-1]["version"])
                bundle = center.store.read_version_bundle(ns, latest)
                if bundle is not None:
                    from .compute import compute_bundle, normalize_events

                    events = normalize_events(raw_input["events"])
                    result = compute_bundle(
                        bundle,
                        events,
                        tolerance=tolerance,
                        logical_clock_ms=max(
                            bundle.logical_clock_ms,
                            int(raw_input.get("logical_clock_ms", 0)),
                        ),
                        input_digest=sha256_json(
                            {
                                "namespace": ns,
                                "version": latest,
                                "events": raw_input["events"],
                            }
                        ),
                    )
                    failed_metrics = [
                        name
                        for name, metric in result["metrics"].items()
                        if not metric["bound_ok"]
                    ]
                    numeric = {
                        "namespace": ns,
                        "version": latest,
                        "result_digest": result["result_digest"],
                        "metrics": result["metrics"],
                        "precision_sensitive": result["sensitivity"][
                            "precision_sensitive"
                        ],
                    }
                    checks.append(
                        _check(
                            "numeric_truth_bounds",
                            "fail" if failed_metrics else "pass",
                            {
                                "checked_metrics": len(result["metrics"]),
                                "failed_metrics": failed_metrics,
                                "result_digest": result["result_digest"],
                            },
                            code="E_BOUND_VIOLATION" if failed_metrics else None,
                        )
                    )
                    if len(versions) >= 2:
                        comparison = center.compare_versions(
                            ns,
                            int(versions[0]["version"]),
                            latest,
                            raw_input,
                            tolerance=tolerance,
                            scale_factor=scale_factor,
                        )
                        sensitivity = {
                            "namespace": ns,
                            "from_version": int(versions[0]["version"]),
                            "to_version": latest,
                            "comparison": comparison["comparison"],
                            "higher_scale": comparison["higher_scale"],
                        }
                        checks.append(
                            _check(
                                "version_sensitivity",
                                "pass",
                                {
                                    "precision_sensitive": comparison["comparison"][
                                        "precision_sensitive"
                                    ],
                                    "higher_scale_flip": comparison["higher_scale"][
                                        "flips_conclusion"
                                    ],
                                },
                            )
                        )
                else:
                    checks.append(
                        _check(
                            "numeric_truth_bounds",
                            "fail",
                            "当前版本快照缺失",
                            code="E_NOT_FOUND",
                        )
                    )
        else:
            checks.append(
                _check(
                    "numeric_truth_bounds",
                    "skip",
                    f"未找到计算输入 {input_path}",
                )
            )

    passed_count = sum(1 for check in checks if check["status"] == "pass")
    failed_count = sum(1 for check in checks if check["status"] == "fail")
    warning_count = sum(1 for check in checks if check["status"] == "warn")
    return {
        "ok": failed_count == 0,
        "passed": failed_count == 0,
        "generated_at": now_utc(),
        "summary": {
            "checks": len(checks),
            "passed": passed_count,
            "failed": failed_count,
            "warnings": warning_count,
        },
        "checks": checks,
        "numeric_comparison": numeric,
        "sensitivity": sensitivity,
    }


def _verify_version_chain(center: ConfigCenter, namespace: str) -> dict[str, Any]:
    errors: list[str] = []
    with center.store.read_session() as conn:
        pointer = center.store.get_pointer(conn, namespace)
        versions = conn.execute(
            "SELECT * FROM versions WHERE namespace = ? ORDER BY version",
            (namespace,),
        ).fetchall()
        if pointer is None:
            errors.append("当前指针不存在")
        if not versions:
            errors.append("版本链为空")
            return _check("version_chain", "fail", errors, code="E_NOT_INITIALIZED")
        max_version = max(int(row["version"]) for row in versions)
        if pointer is not None and int(pointer["version"]) != max_version:
            errors.append(
                f"指针版本 {pointer['version']} 不是最大版本 {max_version}"
            )
        previous_version: int | None = None
        previous_clock: int | None = None
        for row in versions:
            version = int(row["version"])
            if previous_version is not None:
                if version <= previous_version:
                    errors.append(f"版本号未严格递增: {previous_version} -> {version}")
                expected_parent = previous_version
                if row["parent_version"] != expected_parent:
                    errors.append(
                        f"v{version} parent_version={row['parent_version']}，"
                        f"期望 {expected_parent}"
                    )
                if previous_clock is not None and int(row["logical_clock_ms"]) < previous_clock:
                    errors.append(f"v{version} 逻辑时钟发生回退")
            elif row["parent_version"] is not None:
                errors.append("首版本 parent_version 必须为空")
            previous_version = version
            previous_clock = int(row["logical_clock_ms"])
            config_rows = conn.execute(
                """
                SELECT * FROM version_configs
                WHERE namespace = ? AND version = ?
                ORDER BY config_key
                """,
                (namespace, version),
            ).fetchall()
            if int(row["config_count"]) != len(config_rows):
                errors.append(
                    f"v{version} config_count={row['config_count']}，实际 {len(config_rows)}"
                )
            decls: dict[str, ConfigDecl] = {}
            for cfg_row in config_rows:
                value_decimal = (
                    Decimal(cfg_row["value_text"])
                    if cfg_row["value_type"] in {"integer", "decimal"}
                    else None
                )
                decls[cfg_row["config_key"]] = ConfigDecl(
                    key=cfg_row["config_key"],
                    value_type=cfg_row["value_type"],
                    value_text=cfg_row["value_text"],
                    value_decimal=value_decimal,
                    scale=int(cfg_row["unit_scale"]),
                    role=cfg_row["role"],
                )
            recomputed = snapshot_digest(
                namespace,
                version,
                int(row["logical_clock_ms"]),
                decls,
            )
            if recomputed != row["digest"]:
                errors.append(f"v{version} digest 复算不一致")
            if row["content_digest"] != row["digest"]:
                errors.append(f"v{version} content_digest 与 digest 不一致")
        if pointer is not None:
            target = conn.execute(
                """
                SELECT digest, logical_clock_ms, config_count
                FROM versions WHERE namespace = ? AND version = ?
                """,
                (namespace, int(pointer["version"])),
            ).fetchone()
            if target is None:
                errors.append("指针指向不存在的版本")
            else:
                if pointer["digest"] != target["digest"]:
                    errors.append("指针摘要与版本摘要不一致")
                if int(pointer["logical_clock_ms"]) != int(target["logical_clock_ms"]):
                    errors.append("指针逻辑时钟与版本不一致")
                if int(target["config_count"]) == 0:
                    errors.append("指针版本没有配置项")
    return _check(
        "version_chain",
        "fail" if errors else "pass",
        {
            "namespace": namespace,
            "version_count": len(versions),
            "current_version": int(pointer["version"]) if pointer else None,
            "errors": errors,
        },
        code="E_VERSION_CHAIN" if errors else None,
    )


def _verify_conversion_replay(center: ConfigCenter, namespace: str) -> dict[str, Any]:
    errors: list[str] = []
    replay_count = 0
    inherited_count = 0
    with center.store.read_session() as conn:
        versions = conn.execute(
            "SELECT version FROM versions WHERE namespace = ? ORDER BY version",
            (namespace,),
        ).fetchall()
        previous: dict[str, Any] | None = None
        for version_row in versions:
            version = int(version_row["version"])
            current_rows = conn.execute(
                """
                SELECT * FROM version_configs
                WHERE namespace = ? AND version = ?
                ORDER BY config_key
                """,
                (namespace, version),
            ).fetchall()
            current: dict[str, Any] = {row["config_key"]: row for row in current_rows}
            for key, row in current.items():
                if row["value_type"] not in {"integer", "decimal"}:
                    if row["fixed_int_text"] is not None:
                        errors.append(f"v{version}/{key} 非数值配置不应有定点值")
                    continue
                conversion_id = row["conversion_id"]
                if conversion_id is None:
                    errors.append(f"v{version}/{key} 数值配置缺少换算登记")
                    continue
                ledger = conn.execute(
                    "SELECT * FROM conversion_ledger WHERE conversion_id = ?",
                    (conversion_id,),
                ).fetchone()
                if ledger is None:
                    errors.append(f"v{version}/{key} conversion_id 无账本")
                    continue
                if ledger["namespace"] != namespace:
                    errors.append(f"v{version}/{key} 账本命名空间不一致")
                ledger_version = int(ledger["version"])
                if ledger_version == version:
                    replay_count += 1
                    prior = previous.get(key) if previous is not None else None
                    expected_carry = (
                        _decimal(prior["residual_units_text"])
                        if prior is not None
                        and prior["value_type"] in {"integer", "decimal"}
                        else Decimal(0)
                    )
                    actual_carry = _decimal(ledger["carried_in_text"])
                    if actual_carry != expected_carry:
                        errors.append(
                            f"v{version}/{key} carried_in={actual_carry}，"
                            f"期望 {expected_carry}"
                        )
                    decl = ConfigDecl(
                        key=key,
                        value_type=row["value_type"],
                        value_text=row["value_text"],
                        value_decimal=Decimal(row["value_text"]),
                        scale=int(row["unit_scale"]),
                        role=row["role"],
                    )
                    rec = convert_decl(decl, expected_carry)
                    comparisons = {
                        "raw_scaled": (ledger["raw_scaled_text"], rec.raw_scaled),
                        "adjusted_scaled": (
                            ledger["adjusted_scaled_text"],
                            rec.adjusted_scaled,
                        ),
                        "fixed_int": (ledger["fixed_int_text"], rec.fixed_int),
                        "discarded": (ledger["discarded_text"], rec.discarded),
                        "carried_out": (
                            ledger["carried_out_text"],
                            rec.carried_out,
                        ),
                        "conversion_error": (
                            ledger["conversion_error_text"],
                            rec.conversion_error,
                        ),
                    }
                    for field, (stored, recomputed) in comparisons.items():
                        if _decimal(stored) != recomputed:
                            errors.append(
                                f"v{version}/{key} {field} 复算不一致: "
                                f"{stored} != {stable_decimal(recomputed)}"
                            )
                    if ledger["rounding_direction"] != rec.rounding_direction:
                        errors.append(f"v{version}/{key} 舍入方向登记不一致")
                    if str(row["fixed_int_text"]) != str(rec.fixed_int):
                        errors.append(f"v{version}/{key} 版本定点整数与账本不一致")
                else:
                    inherited_count += 1
                    prior = previous.get(key) if previous is not None else None
                    if prior is None:
                        errors.append(f"v{version}/{key} 继承换算但前一版本不存在")
                    else:
                        if prior["conversion_id"] != conversion_id:
                            errors.append(f"v{version}/{key} 继承换算来源链断裂")
                        if prior["fixed_int_text"] != row["fixed_int_text"]:
                            errors.append(f"v{version}/{key} 继承定点值发生变化")
                        if prior["residual_units_text"] != row["residual_units_text"]:
                            errors.append(f"v{version}/{key} 继承余量发生变化")
            previous = current
    return _check(
        "conversion_replay",
        "fail" if errors else "pass",
        {
            "namespace": namespace,
            "recomputed_conversions": replay_count,
            "inherited_versions": inherited_count,
            "errors": errors,
        },
        code="E_CONVERSION_REPLAY" if errors else None,
    )


def _verify_results(center: ConfigCenter, namespace: str) -> dict[str, Any]:
    errors: list[str] = []
    result_count = 0
    pending_count = 0
    with center.store.read_session() as conn:
        results = conn.execute(
            """
            SELECT * FROM client_results
            WHERE namespace = ?
            ORDER BY client_id, round_key
            """,
            (namespace,),
        ).fetchall()
        for row in results:
            result_count += 1
            payload = json.loads(row["result_json"])
            declared_digest = payload.pop("result_digest", None)
            recomputed = sha256_json(payload)
            if declared_digest != row["result_digest"] or declared_digest != recomputed:
                errors.append(
                    f"{row['client_id']}/{row['round_key']} result_digest 复算不一致"
                )
            job = conn.execute(
                """
                SELECT * FROM compute_jobs
                WHERE namespace = ? AND client_id = ? AND round_key = ?
                """,
                (namespace, row["client_id"], row["round_key"]),
            ).fetchone()
            if job is None:
                errors.append(
                    f"{row['client_id']}/{row['round_key']} 缺少计算作业"
                )
                continue
            if job["status"] != "committed":
                errors.append(
                    f"{row['client_id']}/{row['round_key']} 结果已写但作业未提交"
                )
            if job["input_digest"] != row["input_digest"]:
                errors.append(
                    f"{row['client_id']}/{row['round_key']} 输入摘要不一致"
                )
            if int(job["bound_version"]) != int(row["bound_version"]):
                errors.append(
                    f"{row['client_id']}/{row['round_key']} 绑定版本不一致"
                )
            ledger_count = conn.execute(
                "SELECT COUNT(*) FROM error_ledger WHERE result_id = ?",
                (row["result_id"],),
            ).fetchone()[0]
            expected_ledger = len(json.loads(row["error_ledger_json"]))
            if ledger_count != expected_ledger:
                errors.append(
                    f"{row['client_id']}/{row['round_key']} 误差账本行数不一致"
                )
        duplicates = conn.execute(
            """
            SELECT client_id, round_key, COUNT(*) AS n
            FROM client_results WHERE namespace = ?
            GROUP BY client_id, round_key HAVING n > 1
            """,
            (namespace,),
        ).fetchall()
        if duplicates:
            errors.append("存在重复客户端轮次结果")
        pending_count = conn.execute(
            """
            SELECT COUNT(*) FROM compute_jobs
            WHERE namespace = ? AND status = 'pending'
            """,
            (namespace,),
        ).fetchone()[0]
    status = "fail" if errors else ("warn" if pending_count else "pass")
    return _check(
        "result_and_job_atomicity",
        status,
        {
            "namespace": namespace,
            "results": result_count,
            "pending_jobs": pending_count,
            "errors": errors,
            "note": (
                "存在未提交作业，可执行 resume 只重放该作业"
                if pending_count
                else "结果、作业、误差账本与水位摘要一致"
            ),
        },
        code="E_RESULT_LEDGER" if errors else None,
    )


def _verify_pointer_isolation(center: ConfigCenter, namespace: str) -> dict[str, Any]:
    errors: list[str] = []
    with center.store.read_session() as conn:
        pointer = center.store.get_pointer(conn, namespace)
        if pointer is None:
            return _check(
                "pointer_isolation",
                "fail",
                "当前指针不存在",
                code="E_NOT_INITIALIZED",
            )
        version = int(pointer["version"])
        config_rows = conn.execute(
            """
            SELECT COUNT(*) AS n FROM version_configs
            WHERE namespace = ? AND version = ?
            """,
            (namespace, version),
        ).fetchone()["n"]
        pointer_config_count = conn.execute(
            """
            SELECT config_count FROM versions
            WHERE namespace = ? AND version = ?
            """,
            (namespace, version),
        ).fetchone()
        if pointer_config_count is None:
            errors.append("指针版本元数据缺失")
        elif int(pointer_config_count["config_count"]) != int(config_rows):
            errors.append("指针版本配置集合不完整")
        for row in conn.execute(
            """
            SELECT version, config_count FROM versions
            WHERE namespace = ? ORDER BY version
            """,
            (namespace,),
        ):
            actual = conn.execute(
                """
                SELECT COUNT(*) AS n FROM version_configs
                WHERE namespace = ? AND version = ?
                """,
                (namespace, int(row["version"])),
            ).fetchone()["n"]
            if int(row["config_count"]) != int(actual):
                errors.append(f"v{row['version']} 配置集合不完整")
    return _check(
        "pointer_isolation",
        "fail" if errors else "pass",
        {
            "namespace": namespace,
            "pointer_version": int(pointer["version"]),
            "addressable_snapshot_complete": not errors,
            "errors": errors,
        },
        code="E_POINTER_ISOLATION" if errors else None,
    )


def _verify_error_contract(center: ConfigCenter) -> dict[str, Any]:
    cases = [
        (
            "bad_clock",
            center.data_dir / "error_cases" / "bad_clock.json",
            "E_BAD_CLOCK",
            normalize_snapshot,
        ),
        (
            "bad_type",
            center.data_dir / "error_cases" / "bad_type.json",
            "E_BAD_CONFIG",
            normalize_snapshot,
        ),
        (
            "bad_digest",
            center.data_dir / "error_cases" / "bad_digest_patch.json",
            "E_DIGEST_MISMATCH",
            normalize_patch,
        ),
    ]
    results: list[dict[str, Any]] = []
    okay = True
    for name, path, expected_code, validator in cases:
        if not path.exists():
            results.append(
                {"case": name, "status": "skip", "reason": "文件不存在"}
            )
            continue
        try:
            validator(read_json(path))
        except BusinessError as exc:
            passed = exc.code == expected_code
            okay = okay and passed
            results.append(
                {
                    "case": name,
                    "status": "pass" if passed else "fail",
                    "expected_code": expected_code,
                    "actual_code": exc.code,
                    "message": exc.message,
                }
            )
        except Exception as exc:  # pragma: no cover - defensive contract check
            okay = False
            results.append(
                {
                    "case": name,
                    "status": "fail",
                    "expected_code": expected_code,
                    "actual_code": type(exc).__name__,
                    "message": str(exc),
                }
            )
    return _check(
        "business_error_contract",
        "pass" if okay else "fail",
        results,
        code=None if okay else "E_ERROR_CONTRACT",
    )

