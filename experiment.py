"""Repeatable end-to-end experiment for the local SQL plan laboratory.

Run from Windows cmd.exe:
    python experiment.py --out reports

The script uses no network or external database service.  It writes a fresh
SQLite file and deterministic JSON/Markdown reports.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sqlplan_lab.anomalies import ANOMALY_CODES, run_anomaly
from sqlplan_lab.engine import Engine
from sqlplan_lab.errors import E_PARSE, LabError


ROOT = Path(__file__).resolve().parent
DEFAULT_SAMPLE = ROOT / "samples" / "demo.sql"
DEFAULT_QUERY = ROOT / "samples" / "query.sql"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _remove_db_files(db_path: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()


def _reformatted_query() -> str:
    return (
        "  select   O.ID , o.amount,c.region\n"
        "from ORDERS as O\n"
        "join CUSTOMERS as C on (O.customer_id=C.id)\n"
        "where ((O.status = 'paid') and (O.amount >= 100)) ;\n"
    )


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# SQL Plan Attribution Experiment Report",
        "",
        "This report is deterministic and generated from local SQLite samples only.",
        "",
        "## Versions",
        "",
        f"- Data version: `{report['data']['version']}`",
        f"- Statistics v1: `{report['statistics']['v1']['stats_version']}`",
        f"- Statistics v2: `{report['statistics']['v2']['stats_version']}`",
        f"- Plan id: `{report['plan']['plan_id']}`",
        "",
        "## Normalized Plan",
        "",
        "```sql",
        report["plan"]["normalized_sql"],
        "```",
        "",
        "```text",
        report["plan"]["tree_text"],
        "```",
        "",
        "## Operator Attribution (current statistics v2)",
        "",
        "| Operator | Type | Estimated | Actual | Est/Actual | Deviation | Heat |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for row in report["analysis"]["operators"]:
        ratio = row["estimated_to_actual_ratio"]
        lines.append(
            "| {label} | {kind} | {estimated_rows} | {actual_rows} | {ratio} | "
            "{deviation_score} | {heat_band} |".format(
                **row, ratio=ratio
            )
        )
    lines.extend(
        [
            "",
            f"Primary attribution operator: `{report['analysis']['primary_operator_id']}`.",
            "",
            "## Statistics Version Comparison",
            "",
            f"Changed operators: **{report['comparison']['changed_operator_count']}**",
            "",
            "| Operator | Type | Old estimate | New estimate | Old score | New score | Score delta |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in report["comparison"]["changed_operators"]:
        lines.append(
            f"| {row['label']} | {row['kind']} | {row['old_estimated_rows']} | "
            f"{row['new_estimated_rows']} | {row['old_deviation_score']} | "
            f"{row['new_deviation_score']} | {row['deviation_delta']} |"
        )
    lines.extend(
        [
            "",
            "## Snapshot Restoration",
            "",
            f"Snapshot `{report['snapshot']['name']}` restored with whole-state replacement: "
            f"`{report['snapshot']['replaced']}`.",
            "",
            f"Replayed plans after opening a new engine: **{len(report['snapshot']['history_verification'])}**",
            "",
            "## Anomaly Injection",
            "",
            "| Anomaly | Expected code | Observed code | First event id | Repeat event id | Stable |",
            "|---|---|---|---|---|---|",
        ]
    )
    for row in report["anomalies"]:
        lines.append(
            f"| {row['anomaly']} | {row['expected_code']} | {row['business_code']} | "
            f"`{row['first_event_id']}` | `{row['repeat_event_id']}` | {row['stable']} |"
        )
    lines.extend(
        [
            "",
            "## Validation Checks",
            "",
            "| Check | Result |",
            "|---|---|",
        ]
    )
    for check in report["checks"]:
        lines.append(f"| {check['name']} | {'PASS' if check['passed'] else 'FAIL'} |")
    lines.extend(
        [
            "",
            f"Overall: **{'PASS' if report['all_passed'] else 'FAIL'}**",
            "",
        ]
    )
    return "\n".join(lines)


def run_experiment(
    out_dir: Path,
    sample_path: Path,
    query_path: Path,
    anomaly_mode: str = "all",
    reset: bool = True,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "experiment.sqlite3"
    if reset:
        _remove_db_files(db_path)
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, details: Any = None) -> None:
        item: dict[str, Any] = {"name": name, "passed": bool(passed)}
        if details is not None:
            item["details"] = details
        checks.append(item)

    sample_text = _read(sample_path)
    query_text = _read(query_path)
    engine = Engine(db_path)
    try:
        ingest_1 = engine.ingest_sql_file(sample_path)
        ingest_2 = engine.ingest_sql_file(sample_path)
        check("same sample re-ingest yields same data version", ingest_1["data_version"] == ingest_2["data_version"])
        check("same sample re-ingest yields same content digest", ingest_1["digest"] == ingest_2["digest"])

        plan = engine.build_plan(query_text)
        plan_variant = engine.build_plan(_reformatted_query())
        check("whitespace/case/parenthesis variant yields same plan id", plan["plan_id"] == plan_variant["plan_id"])
        check("variant yields same normalized SQL", plan["normalized_sql"] == plan_variant["normalized_sql"])
        check(
            "stable topological serialization",
            plan["topological_order"] == plan_variant["topological_order"]
            and plan["tree"] == plan_variant["tree"],
        )

        parse_rejections = []
        for bad_query in (
            "SELECT o.id FROM orders AS o WHERE o.id IN (SELECT id FROM customers)",
            "SELECT COUNT(*) FROM orders",
        ):
            try:
                engine.build_plan(bad_query)
                parse_rejections.append(None)
            except LabError as exc:
                parse_rejections.append(exc.code)
        check("subquery and aggregate rejected with E_PARSE", parse_rejections == [E_PARSE, E_PARSE], parse_rejections)

        stats_v1 = engine.create_stats_version(4, 0.5, {})
        estimate_a = engine.estimate(query_text, stats_v1["stats_version"])
        estimate_b = engine.estimate(query_text, stats_v1["stats_version"])
        check("same statistics version yields identical estimates", estimate_a["estimate"] == estimate_b["estimate"])
        check(
            "statistics version is emitted by estimator",
            estimate_a["estimate"]["stats_version"] == stats_v1["stats_version"],
        )
        check(
            "estimator and actual collection use one data version",
            estimate_a["estimate"]["data_version"] == engine.current_data_version,
        )

        analysis_v1 = engine.analyze(query_text, stats_v1["stats_version"])
        heat_v1 = engine.load_heatmap()
        check("v1 heatmap contains all operators", len(heat_v1) == len(analysis_v1["operators"]))

        zero_query = (
            "SELECT o.id FROM orders AS o JOIN customers AS c "
            "ON o.customer_id = c.id WHERE o.status = 'never';"
        )
        zero_analysis = engine.analyze(zero_query, stats_v1["stats_version"])
        zero_filter = zero_analysis["operators"][-1]
        check(
            "zero actual rows use explicit ratio sentinel",
            zero_filter["zero_actual_sentinel"] == "ZERO_ACTUAL"
            and zero_filter["estimated_to_actual_ratio"] == "ZERO_ACTUAL"
            and zero_filter["ratio_ppm"] is None,
        )

        stats_v2 = engine.create_stats_version(
            12,
            1.0,
            {"orders.status": 0.12, "orders.amount": 0.05},
        )
        check("statistics change creates a new version", stats_v1["stats_version"] != stats_v2["stats_version"])
        check(
            "old statistics version remains available",
            any(item["stats_version"] == stats_v1["stats_version"] for item in engine.list_stats_versions()),
        )

        analysis_v2 = engine.analyze(query_text, stats_v2["stats_version"])
        heat_v2 = engine.load_heatmap()
        check("v2 heatmap is whole-set replacement", len(heat_v2) == len(analysis_v2["operators"]))
        check(
            "v2 heatmap contains no stale v1 operator rows",
            all(row["statistics"]["estimated_rows"] == row["estimated_rows"] for row in heat_v2)
            and all(row["op_id"] in {item["op_id"] for item in analysis_v2["operators"]} for row in heat_v2),
        )
        comparison = engine.compare_stats_versions(
            query_text, stats_v1["stats_version"], stats_v2["stats_version"]
        )
        check("statistics comparison identifies changed operators", comparison["changed_operator_count"] > 0)
        check("statistics comparison marks version difference", comparison["versions_differ"])

        snapshot = engine.save_snapshot("baseline")
        check("snapshot stores plan history", snapshot["plan_count"] == len(engine.session_state()["plan_history"]))
    finally:
        engine.close()

    reopened = Engine(db_path)
    try:
        restored = reopened.restore_snapshot("baseline")
        check("snapshot restore replaces whole session", restored["replaced"] is True)
        check(
            "restored history replays to identical deviation scores",
            all(item["same"] for item in restored["history_verification"]),
        )
        check(
            "restored heatmap is present",
            len(reopened.load_heatmap()) == len(analysis_v2["operators"]),
        )

        anomaly_types = list(ANOMALY_CODES) if anomaly_mode == "all" else [anomaly_mode]
        anomaly_reports = []
        for anomaly in anomaly_types:
            first = run_anomaly(
                reopened,
                anomaly,
                query_text=query_text,
                stats_version=stats_v2["stats_version"],
            )
            second = run_anomaly(
                reopened,
                anomaly,
                query_text=query_text,
                stats_version=stats_v2["stats_version"],
            )
            stable = (
                first["business_code"] == second["business_code"]
                and first["log_event_id"] == second["log_event_id"]
            )
            anomaly_reports.append(
                {
                    "anomaly": anomaly,
                    "expected_code": ANOMALY_CODES[anomaly],
                    "business_code": first["business_code"],
                    "message": first["message"],
                    "details": first["details"],
                    "first_event_id": first["log_event_id"],
                    "repeat_event_id": second["log_event_id"],
                    "stable": stable,
                }
            )
            check(
                f"{anomaly} anomaly reports {ANOMALY_CODES[anomaly]}",
                first["business_code"] == ANOMALY_CODES[anomaly],
            )
            check(f"{anomaly} anomaly is reproducible", stable)

        logs = reopened.list_logs()
        sorted_logs = sorted(logs, key=lambda item: (item["event"], item["event_id"], item["event_key"]))
        check("structured anomaly logs use stable ordering", logs == sorted_logs)
        check(
            "repeated injection deduplicates stable log records",
            len(logs) == len(anomaly_types),
            {"log_count": len(logs)},
        )
        check(
            "anomaly injection leaves data version unchanged",
            reopened.current_data_version == analysis_v2["data_version"],
        )
    finally:
        reopened.close()

    report: dict[str, Any] = {
        "format": 1,
        "experiment": "sql-plan-attribution-heat-lab",
        "data": {
            "version": ingest_1["data_version"],
            "content_digest": ingest_1["digest"],
            "sql_sha256": ingest_1["sql_sha256"],
            "tables": ingest_1["tables"],
        },
        "plan": {
            "plan_id": plan["plan_id"],
            "normalized_sql": plan["normalized_sql"],
            "tree": plan["tree"],
            "tree_text": "\n".join(plan["tree"]),
            "topological_order": plan["topological_order"],
        },
        "statistics": {
            "v1": {
                "stats_version": stats_v1["stats_version"],
                "parameters": stats_v1["parameters"],
            },
            "v2": {
                "stats_version": stats_v2["stats_version"],
                "parameters": stats_v2["parameters"],
            },
        },
        "analysis": {
            "stats_version": analysis_v2["stats_version"],
            "data_version": analysis_v2["data_version"],
            "root_estimated_rows": analysis_v2["root_estimated_rows"],
            "root_actual_rows": analysis_v2["root_actual_rows"],
            "operators": analysis_v2["operators"],
            "attribution_chain": analysis_v2["attribution_chain"],
            "primary_operator_id": analysis_v2["primary_operator_id"],
        },
        "comparison": comparison,
        "heatmap": {"replacement": "whole_set", "rows": heat_v2},
        "snapshot": {
            "name": snapshot["name"],
            "checksum": snapshot["checksum"],
            "plan_count": snapshot["plan_count"],
            "replaced": restored["replaced"],
            "history_verification": restored["history_verification"],
        },
        "anomalies": anomaly_reports,
        "logs": logs,
        "checks": checks,
        "all_passed": all(item["passed"] for item in checks),
    }
    report_json = _stable_json(report)
    report_md = _markdown(report)
    (out_dir / "experiment_report.json").write_text(report_json + "\n", encoding="utf-8")
    (out_dir / "experiment_report.md").write_text(report_md, encoding="utf-8")
    (out_dir / "anomaly_logs.json").write_text(_stable_json(logs) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic local SQL plan experiment")
    parser.add_argument("--out", default="reports", help="output directory")
    parser.add_argument("--sample", default=str(DEFAULT_SAMPLE), help="local sample SQL file")
    parser.add_argument("--query", default=str(DEFAULT_QUERY), help="local SELECT query file")
    parser.add_argument(
        "--anomaly",
        choices=["all", "skew", "stats", "replay"],
        default="all",
        help="inject one or all supported reproducible anomalies",
    )
    parser.add_argument(
        "--keep-db",
        action="store_true",
        help="reuse the existing experiment database instead of starting fresh",
    )
    args = parser.parse_args(argv)
    try:
        report = run_experiment(
            Path(args.out),
            Path(args.sample),
            Path(args.query),
            anomaly_mode=args.anomaly,
            reset=not args.keep_db,
        )
    except LabError as exc:
        print(_stable_json(exc.to_dict()))
        return 2
    except Exception as exc:
        print(_stable_json({"ok": False, "code": "E_INTERNAL", "message": str(exc)}))
        return 3
    print(
        _stable_json(
            {
                "ok": report["all_passed"],
                "report_json": str(Path(args.out) / "experiment_report.json"),
                "report_markdown": str(Path(args.out) / "experiment_report.md"),
                "checks_passed": sum(1 for item in report["checks"] if item["passed"]),
                "checks_total": len(report["checks"]),
                "anomaly_codes": {
                    item["anomaly"]: item["business_code"] for item in report["anomalies"]
                },
            }
        )
    )
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
