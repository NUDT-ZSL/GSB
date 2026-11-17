from __future__ import annotations

import json
from pathlib import Path

from authdiag.engine import EventDocument, Workbench


def _run(sample_dir: Path, *, workdir: Path, policy: str, events: list[str]):
    return Workbench(
        json_load(sample_dir / policy),
        source=str(sample_dir / policy),
        workdir=workdir,
        output_dir=workdir,
    ).run(
        [
            EventDocument(
                str(sample_dir / name),
                (sample_dir / name).read_text(encoding="utf-8"),
            )
            for name in events
        ]
    )


def json_load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_transitive_propagation_and_truth_markers(sample_dir: Path, tmp_path: Path):
    result = _run(
        sample_dir,
        workdir=tmp_path / "main",
        policy="policy.json",
        events=["events_a.json", "events_b.json"],
    )
    assert result.exit_code == 0
    report = result.report
    assert set(token["token_id"] for token in report["affected_tokens"]) == {
        "tok-alice1",
        "tok-alice2",
        "tok-dave1",
    }
    assert set(session["session_id"] for session in report["affected_sessions"]) == {
        "sess-alice1",
        "sess-carol1",
        "sess-dave1",
    }
    assert all(item["consistent"] for item in report["truth_comparison"])
    assert len(report["truth_comparison"]) == 3
    update_sets = {
        item["update_id"]: (item["affected_tokens"], item["affected_sessions"])
        for item in report["updates"]
    }
    assert update_sets["upd-001"] == (["tok-dave1"], ["sess-carol1", "sess-dave1"])
    assert update_sets["upd-003"] == (
        ["tok-alice1", "tok-alice2"],
        ["sess-alice1"],
    )
    assert report["checkpoint"]["watermark"] == (
        '["2026-01-02T00:00:00.000000Z","1.2.1","upd-003"]'
    )


def test_cycle_reports_error_with_entry_indices(sample_dir: Path, tmp_path: Path):
    result = _run(
        sample_dir,
        workdir=tmp_path / "cycle",
        policy="cycle_policy.json",
        events=["cycle_events.json"],
    )
    assert result.exit_code == 1
    cycle_errors = [
        item for item in result.diagnostics if item.code == "E_CYCLE"
    ]
    assert cycle_errors
    assert cycle_errors[0].details["entry_indices"] == [0, 1]
    assert result.report["cycles"]
    assert result.report["cycles"][0]["entry_ids"] == ["role:a", "role:b"]
    assert result.graph_data["cycles"][0]["entry_indices"] == [0, 1]
    assert result.report["affected_tokens"]
    assert any(chain["cycle"] for chain in result.report["propagation_chains"])


def test_truth_comparison_detects_explicit_marker_mismatch(
    sample_dir: Path,
    tmp_path: Path,
):
    policy = json_load(sample_dir / "policy.json")
    policy["sync_markers"][0]["expected_tokens"] = ["tok-dave1"]
    result = Workbench(
        policy,
        source=str(sample_dir / "policy.json"),
        workdir=tmp_path / "mismatch",
        output_dir=tmp_path / "mismatch",
    ).run(
        [
            EventDocument(
                str(sample_dir / name),
                (sample_dir / name).read_text(encoding="utf-8"),
            )
            for name in ("events_a.json", "events_b.json")
        ]
    )
    assert result.exit_code == 1
    comparison = next(
        item
        for item in result.report["truth_comparison"]
        if item["marker_id"] == "sync-003"
    )
    assert not comparison["consistent"]
    assert comparison["unexpected_tokens"] == ["tok-alice1", "tok-alice2"]
    assert comparison["missing_tokens"] == ["tok-dave1"]
    assert any(item.code == "E_SYNC" for item in result.diagnostics)


def test_chain_contains_policy_path_and_terminal(sample_dir: Path, tmp_path: Path):
    result = _run(
        sample_dir,
        workdir=tmp_path / "chains",
        policy="policy.json",
        events=["events_a.json", "events_b.json"],
    )
    member_update = next(
        item for item in result.report["updates"] if item["update_id"] == "upd-002"
    )
    token_chain = next(
        chain
        for chain in member_update["propagation_chains"]
        if chain["nodes"][-1]["type"] == "token"
        and chain["nodes"][-1]["id"] == "tok-alice2"
    )
    policy_ids = [
        node["id"] for node in token_chain["nodes"] if node["type"] == "policy"
    ]
    assert "role:member" in policy_ids
    assert "role:admin" in policy_ids
    assert token_chain["nodes"][-1]["session_id"] == "sess-alice1"


def test_graph_file_marks_changed_and_affected_nodes(
    sample_dir: Path,
    tmp_path: Path,
):
    result = _run(
        sample_dir,
        workdir=tmp_path / "graph",
        policy="policy.json",
        events=["events_a.json", "events_b.json"],
    )
    nodes = {node["id"]: node for node in result.graph_data["nodes"]}
    assert nodes["role:member"]["changed"] is True
    assert nodes["role:admin"]["affected"] is True
    assert nodes["session:base"]["affected"] is False
    assert result.graph_data["affected_entries"]
