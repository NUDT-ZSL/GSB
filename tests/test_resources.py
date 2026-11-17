from __future__ import annotations

from pathlib import Path

from slpguard import (
    BatchConfig,
    InjectionSpec,
    ResourceLimits,
    analyze_bytes,
    inject,
    run_batch,
)


def test_step_limit_reports_processed_count_and_reason(corpus) -> None:
    data = (corpus / "valid_slp.slp").read_bytes()
    result = analyze_bytes(
        data,
        limits=ResourceLimits(max_steps_per_session=2, max_file_seconds=None),
    )
    assert result.status == "limited"
    assert result.termination == "E_STEPS"
    assert result.processed_messages == 2
    assert result.declared_message_count == 10
    assert result.steps_by_session == {1: 2}
    assert result.sessions == []


def test_step_limit_is_per_session(corpus) -> None:
    data = (corpus / "valid_slp.slp").read_bytes()
    result = analyze_bytes(
        data,
        limits=ResourceLimits(max_steps_per_session=5, max_file_seconds=None),
    )
    assert result.status == "ok"
    assert result.steps_by_session == {1: 5, 2: 5}


def test_wall_clock_timeout_uses_measured_time(corpus) -> None:
    data = (corpus / "valid_slp.slp").read_bytes()
    result = analyze_bytes(
        data,
        limits=ResourceLimits(max_steps_per_session=1000, max_file_seconds=0.0),
    )
    assert result.status == "limited"
    assert result.termination == "E_TIMEOUT"
    assert result.elapsed_seconds > 0.0
    assert "wall-clock" in (result.termination_detail or "")


def test_batch_continues_after_one_file_hits_step_limit(corpus, tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "short.slp").write_bytes((corpus / "valid_v2.slp").read_bytes())

    long_path = sample_dir / "long.slp"
    base = (corpus / "valid_v2.slp").read_bytes()
    inject(
        base,
        InjectionSpec(
            target_session=19,
            renegotiation_count=20,
            max_renegotiations=3,
        ),
        long_path,
    )

    workdir = tmp_path / "work"
    workdir.mkdir()
    report = run_batch(
        sample_dir,
        config=BatchConfig(
            workers=2,
            limits=ResourceLimits(max_steps_per_session=10, max_file_seconds=None),
            checkpoint_enabled=False,
            report_path="reports/resource-report.json",
        ),
        workdir=workdir,
    )
    by_name = {Path(item["path"]).name: item for item in report["files"]}
    assert by_name["short.slp"]["status"] == "ok"
    assert by_name["long.slp"]["status"] == "limited"
    assert by_name["long.slp"]["termination"] == "E_STEPS"
    assert by_name["long.slp"]["processed_messages"] == 10
    assert by_name["long.slp"]["declared_message_count"] > 10
    assert report["batch"]["files"] == 2
    assert report["batch"]["success"] == 1
    assert report["batch"]["limited"] == 1
    assert report["batch"]["failed"] == 0
