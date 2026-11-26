from seg_diag.config_io import read_v1_config, read_v2_config
from seg_diag.hashing import canonical_bytes
from seg_diag.ledger import build_ledger, ledger_canonical_bytes
from seg_diag.sample_generator import generate_sample_directory, load_sample_arrays


def test_same_seed_and_inputs_produce_byte_identical_ledger(tmp_path):
    sample_dir = generate_sample_directory(tmp_path / "sample", seed=777, force=True)
    arrays = load_sample_arrays(sample_dir)
    v1 = read_v1_config(sample_dir / "config_v1.json")
    v2 = read_v2_config(sample_dir / "config_v2")

    first = build_ledger(arrays, v1, v2, seed=777)
    second = build_ledger(arrays, v1, v2, seed=777)

    assert first["checksum"] == second["checksum"]
    assert ledger_canonical_bytes(first) == ledger_canonical_bytes(second)
    assert canonical_bytes(first) == canonical_bytes(second)


def test_truth_has_highest_priority_and_instance_order_is_deterministic(tmp_path):
    sample_dir = generate_sample_directory(tmp_path / "sample", seed=778, force=True)
    arrays = load_sample_arrays(sample_dir)
    v1 = read_v1_config(sample_dir / "config_v1.json")
    v2 = read_v2_config(sample_dir / "config_v2")
    ledger = build_ledger(arrays, v1, v2, seed=778)

    overlap_rows = []
    for y in range(arrays["templates"].shape[1]):
        for x in range(arrays["templates"].shape[2]):
            truth_count = sum(bool(mask[y, x]) for mask in arrays["templates"])
            if truth_count >= 2:
                row = ledger["rows"][y * arrays["image"].shape[1] + x]
                overlap_rows.append(row)
                assert row["winner"]["source"] == "truth"
                truth_instances = [
                    candidate["instance_id"]
                    for candidate in row["candidates"]
                    if candidate["source"] == "truth"
                ]
                assert row["winner"]["instance_id"] == min(truth_instances)

    assert overlap_rows


def test_ledger_contains_one_row_per_pixel(tmp_path):
    sample_dir = generate_sample_directory(tmp_path / "sample", seed=779, force=True)
    arrays = load_sample_arrays(sample_dir)
    v1 = read_v1_config(sample_dir / "config_v1.json")
    v2 = read_v2_config(sample_dir / "config_v2")
    ledger = build_ledger(arrays, v1, v2, seed=779)

    assert len(ledger["rows"]) == arrays["image"].shape[0] * arrays["image"].shape[1]
    assert ledger["ledger_version"] == 1
    assert len(ledger["checksum"]) == 64
