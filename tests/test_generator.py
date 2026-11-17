from __future__ import annotations

from pathlib import Path

from sensitive_guard.engine import analyze_dataset
from sensitive_guard.generator import build_injected_dataset, generate


def test_generator_is_byte_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate(first, seed=12345, clean=True)
    generate(second, seed=12345, clean=True)

    first_files = sorted(
        path.relative_to(first) for path in first.rglob("*") if path.is_file()
    )
    second_files = sorted(
        path.relative_to(second) for path in second.rglob("*") if path.is_file()
    )
    assert first_files == second_files
    for relative in first_files:
        assert (first / relative).read_bytes() == (second / relative).read_bytes()


def test_every_generated_dataset_has_sync_marker_and_injection() -> None:
    import json

    from sensitive_guard.generator import build_samples

    for dataset in build_samples():
        assert dataset["sync_marker"].startswith("sha256:")
        assert isinstance(dataset["injection"], dict)
        json.dumps(dataset, ensure_ascii=False)


def test_explicit_injection_builds_all_requested_scenarios() -> None:
    injection = {
        "force_cycle_length": 3,
        "force_downgrade_at": "inj.down",
        "forge_version_mix_at": "inj.mix",
        "deep_chain": {
            "prefix": "inj.deep",
            "depth": 6,
            "initial_status": "plaintext",
            "rest_status": "masked",
        },
        "simulate_delay_ms": 0,
    }
    dataset = build_injected_dataset(
        injection,
        dataset_id="explicit",
        seed=123,
    )
    result = analyze_dataset(dataset)
    codes = set(result["codes"])

    assert {"E_CYCLE", "E_DOWNGRADE", "E_VERSION_CONFLICT"}.issubset(codes)
    assert result["observations"]["inconsistent"] == 0
    assert result["sync_marker_ok"] is True
    assert any(cycle["length"] == 3 for cycle in result["cycles"])
    last_path = "inj.deep[5].value"
    leaf = next(field for field in result["fields"] if field["path"] == last_path)
    assert leaf["effective_status"] == "plaintext"
    assert dataset["injection"]["deep_chain"]["paths"][-1] == last_path
