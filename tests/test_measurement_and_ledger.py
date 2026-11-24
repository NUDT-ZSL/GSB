from __future__ import annotations

import json

from acoustic_alignment.engine import MeasurementEngine, compute_input_digest
from acoustic_alignment.ledger import LedgerStore
from acoustic_alignment.task_model import load_task


def _run(task_path, output_dir, **kwargs):
    task = load_task(task_path)
    ledger = LedgerStore(output_dir / "alignment_ledger.sqlite")
    digest = compute_input_digest(task)
    engine = MeasurementEngine(task=task, ledger=ledger, **kwargs)
    outcome = engine.run(digest)
    return task, ledger, digest, outcome


def test_sample_measurements_classify_delay_phase_peak_and_validation(sample_task_path, tmp_path):
    task, ledger, digest, outcome = _run(sample_task_path, tmp_path)
    assert outcome.first_error is None
    assert outcome.interrupted is None
    by_id = {result.track_id: result for result in outcome.results}

    assert by_id["ref_main"].alignment_pass is True
    assert by_id["cand_delay3"].delay_samples == 3
    assert by_id["cand_delay3"].alignment_pass is True
    assert by_id["cand_44100"].alignment_pass is True
    assert by_id["cand_16k"].alignment_pass is True

    fractional = by_id["cand_fractional"]
    assert fractional.business_codes == ["E_DELAY"]
    assert fractional.delay_samples == 4
    assert abs(fractional.delay_float_samples - 4.4) < 0.05
    assert fractional.delay_delta_q > fractional.delay_tolerance_q

    phase_bad = by_id["cand_phase_bad"]
    assert "E_PHASE" in phase_bad.business_codes
    high_band = next(band for band in phase_bad.bands if band.band_id == "high_2600_6400")
    assert high_band.point_count > 100
    assert high_band.passed is False
    assert high_band.code == "E_PHASE"
    assert high_band.phase_error_q > high_band.phase_tolerance_q

    peak = by_id["cand_peak_silent"]
    assert peak.business_codes == ["E_PEAK"]
    assert peak.noise_rms is not None and peak.excitation_peak is not None
    assert peak.error_details["noise_rms"] == peak.noise_rms

    for track_id in ("bad_rate", "bad_bits", "bad_channels", "missing_file", "bad_start"):
        assert by_id[track_id].business_codes

    queried = ledger.query(track_id="cand_phase_bad", band_id="high_2600_6400", input_digest=digest)
    assert len(queried["tracks"]) == 1
    assert len(queried["bands"]) == 1
    assert queried["bands"][0]["band_id"] == "high_2600_6400"


def test_fixed_point_and_float_diagnostic_values_are_both_stored(sample_task_path, tmp_path):
    _task, ledger, digest, _outcome = _run(sample_task_path, tmp_path / "first")
    row = ledger.query(track_id="cand_delay3", input_digest=digest)["tracks"][0]
    assert row["delay_samples"] == 3
    assert row["integer_delay_q"] == 3 * 1_000_000
    assert row["float_delay_q"] == round(row["delay_float_samples"] * 1_000_000)
    assert "delay_delta_q" in row
    assert abs(row["delay_float_samples"] - row["delay_samples"]) < 0.01


def test_repeated_run_has_identical_ledger_fields_and_report_bytes(sample_task_path, tmp_path):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    task, first_ledger, digest, _ = _run(sample_task_path, first_dir)
    first_records = first_ledger.canonical_bytes()

    second_ledger = LedgerStore(second_dir / "alignment_ledger.sqlite")
    second_engine = MeasurementEngine(task=task, ledger=second_ledger)
    second_engine.run(digest)
    assert second_ledger.canonical_bytes() == first_records

    from acoustic_alignment.reporting import commit_report

    first_report = commit_report(first_dir / "alignment_report.json", first_ledger, digest, task)
    second_report = commit_report(second_dir / "alignment_report.json", second_ledger, digest, task)
    assert json.dumps(first_report, sort_keys=True) == json.dumps(second_report, sort_keys=True)
    assert (first_dir / "alignment_report.json").read_bytes() == (second_dir / "alignment_report.json").read_bytes()


def test_no_temp_files_remain_after_commit(sample_task_path, tmp_path):
    _task, _ledger, _digest, _outcome = _run(sample_task_path, tmp_path)
    from acoustic_alignment.reporting import commit_report

    task = load_task(sample_task_path)
    ledger = LedgerStore(tmp_path / "alignment_ledger.sqlite")
    commit_report(tmp_path / "alignment_report.json", ledger, _digest, task)
    leftovers = [path for path in tmp_path.iterdir() if path.name.endswith(".tmp")]
    assert leftovers == []
