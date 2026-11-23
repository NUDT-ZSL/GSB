from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lbworkbench.db import WorkbenchDB
from lbworkbench.engine import run_check
from lbworkbench.probes import ProbeHTTPServer
from lbworkbench.store import get_status, load_package
from lbworkbench.util import canonical_json


def test_parallel_instance_probes_commit_once(
    sample_copy: Path,
    check_plan: Path,
    probe_server: ProbeHTTPServer,
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    db = WorkbenchDB(work)
    load_package(db, sample_copy / "package-2026.11.1.json")
    groups = [
        ["api-01", "api-02"],
        ["cache-01", "db-01"],
        ["edge-01", "tie-01"],
        ["worker-01"],
    ]

    def process_group(group: list[str]) -> None:
        run_check(
            WorkbenchDB(work),
            plan_path=str(check_plan),
            probe_base_url=probe_server.base_url,
            round_index=1,
            instances=group,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(process_group, groups))

    result = run_check(
        WorkbenchDB(work),
        plan_path=str(check_plan),
        probe_base_url=probe_server.base_url,
    )
    assert result["ok"] is True
    concurrent_status = get_status(WorkbenchDB(work))

    reference = tmp_path / "reference"
    ref_db = WorkbenchDB(reference)
    load_package(ref_db, sample_copy / "package-2026.11.1.json")
    assert run_check(
        ref_db,
        plan_path=str(check_plan),
        probe_base_url=probe_server.base_url,
    )["ok"]
    assert canonical_json(concurrent_status) == canonical_json(get_status(ref_db))

    with db.connection() as conn:
        observation_count = conn.execute(
            "SELECT COUNT(*) AS count FROM probe_observations"
        ).fetchone()["count"]
    assert observation_count == 21
