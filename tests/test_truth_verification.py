from perm_audit.pipeline import run_audit
from perm_audit.verifier import verify_against_truth


def test_truth_sync_verification_passes(sample_dir, tmp_path):
    workdir = tmp_path / "run"
    result = run_audit(sample_dir, workdir)
    verification = verify_against_truth(
        sample_dir / "truth.jsonl",
        workdir / "report.json",
        output_path=workdir / "verification.json",
    )
    assert verification["ok"] is True
    assert verification["summary"]["failed"] == 0
    assert verification["summary"]["total"] == 17
    assert verification["summary"]["conservative_uncertain"] == 3
    assert (workdir / "verification.json").exists()
