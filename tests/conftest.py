from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from tlsdiag import AnalysisConfig, analyze_run, generate_sample, verify_sample


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("deterministic-sample")
    generate_sample(out, seed=20261125)
    result = verify_sample(out)
    assert result["ok"], result
    return out


@pytest.fixture(scope="session")
def expected(sample_dir: Path) -> Dict[str, Any]:
    return json.loads((sample_dir / "expected/expected.json").read_text(encoding="utf-8"))


def run_profile(
    sample_dir: Path,
    expected: Dict[str, Any],
    profile_name: str,
    out_dir: Path,
    *,
    jobs: int = 1,
    resume: bool = False,
    write_outputs: bool = True,
) -> Any:
    profile = expected["profiles"][profile_name]
    cfg = profile["config"]
    return analyze_run(
        handshake_path=sample_dir / profile["handshake"],
        v1_path=sample_dir / profile["v1"],
        v2_shards=sample_dir / profile["v2_shards"],
        v2_db=sample_dir / profile["v2_db"],
        output_dir=out_dir,
        config=AnalysisConfig(
            jobs=jobs,
            timeout_ms=cfg["timeout_ms"],
            max_frags_per_connection=cfg["max_frags_per_connection"],
            max_expanded_bytes_per_connection=cfg["max_expanded_bytes_per_connection"],
            max_total_expanded_bytes=cfg["max_total_expanded_bytes"],
            resume=resume,
        ),
        resume=resume,
        write_outputs=write_outputs,
    )


def assert_connection_expectation(
    report: Dict[str, Any], connection_id: str, expectation: Dict[str, Any]
) -> None:
    by_id = {c["connection_id"]: c for c in report["connections"]}
    assert connection_id in by_id
    connection = by_id[connection_id]
    if "fragment_status" in expectation:
        assert (connection.get("fragment_reassembly") or {}).get("status") == expectation[
            "fragment_status"
        ]
    if "trust_conclusion" in expectation:
        assert connection["trust_conclusion"] == expectation["trust_conclusion"]
    if "primary_code" in expectation:
        assert connection["primary_code"] == expectation["primary_code"]
    if expectation.get("selected_root") is not None:
        assert connection["selected_chain"] is not None
        assert (
            connection["selected_chain"]["root_fingerprint"]
            == expectation["selected_root"]
        )
    if "candidate_count" in expectation:
        assert len(connection["candidate_chains"]) == expectation["candidate_count"]
    if "duplicate_count" in expectation:
        assert (
            (connection.get("fragment_reassembly") or {}).get("duplicate_count")
            == expectation["duplicate_count"]
        )
    if "missing_sequence_ranges" in expectation:
        assert (
            (connection.get("fragment_reassembly") or {}).get("missing_sequence_ranges")
            == expectation["missing_sequence_ranges"]
        )
    codes = {issue["code"] for issue in connection["issues"]}
    for code in expectation.get("codes_include", []):
        assert code in codes
