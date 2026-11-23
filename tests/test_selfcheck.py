from __future__ import annotations

from transformer_fusion import equivalence_report, execute


def test_public_entrypoints_and_selfcheck(dataset, stores):
    data_dir, manifest = dataset
    report = execute(
        manifest,
        data_dir=data_dir,
        cache_dir=stores["cache_dir"],
        snapshot_dir=stores["snapshot_dir"],
    )
    assert report.selfcheck["ok"]
    assert report.selfcheck["cache_checks"]
    assert report.selfcheck["snapshot_checks"]
    assert report.selfcheck["independent_equivalence"]
    assert all(
        check["exact"] and check["within_tolerance"]
        for check in report.selfcheck["independent_equivalence"].values()
    )

    public = equivalence_report(
        manifest,
        data_dir=data_dir,
        cache_dir=stores["cache_dir"],
        snapshot_dir=stores["snapshot_dir"],
    )
    assert public["ok"]
    assert public["selfcheck"]["ok"]
    assert public["case_count"] == 9
    assert all(case["tolerance_ok"] for case in public["cases"])
