from __future__ import annotations

from pathlib import Path

from gbdt_workbench.pipeline import WorkbenchEngine
from gbdt_workbench.utils import to_jsonable

from .helpers import read_json, rewrite_config, write_config


def _assert_model_equal(left: dict, right: dict) -> None:
    for key in (
        "status",
        "trees",
        "ensemble",
        "contributions",
        "error_attribution",
        "threshold_sensitive_samples",
        "failures",
        "binning",
        "data",
        "initial_state",
    ):
        assert to_jsonable(left[key]) == to_jsonable(right[key]), key


def test_incremental_edit_sequence_matches_full_rebuild(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        experiment={"n_trees": 4, "max_depth": 2, "learning_rate": 0.1, "n_bins": 8},
        runtime={"n_jobs": 1, "snapshot_interval": 1},
    )
    output = tmp_path / "incremental"

    first = WorkbenchEngine(config_path, output).run()
    assert first.exit_code == 0

    rewrite_config(config_path, experiment={"n_trees": 6})
    second = WorkbenchEngine(config_path, output).run()
    audit = second.run_report["operational_audit"]
    assert sorted(audit["reused"]["tree_indices"]) == [0, 1, 2, 3]
    assert sorted(audit["computed"]["tree_indices"]) == [4, 5]
    assert sorted(audit["recompute_set"]["tree_indices"]) == [4, 5]

    rewrite_config(config_path, experiment={"max_depth": 3})
    third = WorkbenchEngine(config_path, output).run()
    audit = third.run_report["operational_audit"]
    assert "binning" in audit["reused"]["stages"]
    assert sorted(audit["computed"]["tree_indices"]) == [0, 1, 2, 3, 4, 5]
    assert sorted(audit["recompute_set"]["tree_indices"]) == [0, 1, 2, 3, 4, 5]

    rewrite_config(config_path, experiment={"learning_rate": 0.15})
    fourth = WorkbenchEngine(config_path, output).run()
    audit = fourth.run_report["operational_audit"]
    assert sorted(audit["reused"]["tree_indices"]) == [0, 1, 2, 3, 4, 5]
    assert audit["computed"]["tree_indices"] == []
    assert "ensemble" in audit["recompute_set"]["stages"]
    assert "contributions" in audit["recompute_set"]["stages"]

    rewrite_config(config_path, experiment={"n_bins": 10})
    fifth = WorkbenchEngine(config_path, output).run()
    audit = fifth.run_report["operational_audit"]
    assert "binning" in audit["recompute_set"]["stages"]
    assert sorted(audit["computed"]["tree_indices"]) == [0, 1, 2, 3, 4, 5]

    rewrite_config(config_path, runtime={"n_jobs": 3})
    sixth = WorkbenchEngine(config_path, output).run()
    audit = sixth.run_report["operational_audit"]
    assert sorted(audit["reused"]["tree_indices"]) == [0, 1, 2, 3, 4, 5]
    assert audit["computed"]["tree_indices"] == []
    assert audit["recompute_set"]["tree_indices"] == []
    assert set(audit["reused"]["stages"]) >= {"binning", "ensemble", "contributions"}

    full_output = tmp_path / "full"
    WorkbenchEngine(config_path, full_output).run()
    incremental_report = read_json(output / "diagnostic_report.json")
    full_report = read_json(full_output / "diagnostic_report.json")
    assert incremental_report == full_report
    assert incremental_report["equivalence_digest"] == full_report["equivalence_digest"]


def test_thread_count_does_not_change_model_artifacts(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, experiment={"n_trees": 6})
    one = WorkbenchEngine(config_path, tmp_path / "one").run()
    rewrite_config(config_path, runtime={"n_jobs": 4})
    four = WorkbenchEngine(config_path, tmp_path / "four").run()

    assert one.exit_code == four.exit_code == 0
    _assert_model_equal(one.diagnostic_report, four.diagnostic_report)
    assert to_jsonable(one.diagnostic_report["trees"]) == to_jsonable(
        four.diagnostic_report["trees"]
    )
    assert to_jsonable(
        one.diagnostic_report["ensemble"]["probabilities"]
    ) == to_jsonable(four.diagnostic_report["ensemble"]["probabilities"])
    assert to_jsonable(
        one.diagnostic_report["contributions"]["feature_contributions"]
    ) == to_jsonable(
        four.diagnostic_report["contributions"]["feature_contributions"]
    )
