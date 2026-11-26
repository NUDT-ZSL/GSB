import json
from dataclasses import replace

import pytest

from seg_diag.codes import CODES
from seg_diag.config_io import read_v1_config, read_v2_config
from seg_diag.errors import DiagnosticError
from seg_diag.migration import migrate_v1_to_v2, migrate_v2_to_v1
from seg_diag.sample_generator import generate_sample_directory
from seg_diag.selfcheck import verify_v1_upgrade_contract, verify_v2_downgrade_contract
from seg_diag.sample_generator import load_sample_arrays


@pytest.fixture()
def sample_dir(tmp_path):
    return generate_sample_directory(tmp_path / "sample", seed=455, force=True)


def test_reads_v1_and_v2_configs(sample_dir):
    v1 = read_v1_config(sample_dir / "config_v1.json")
    v2 = read_v2_config(sample_dir / "config_v2")

    assert v1.source_version == 1
    assert v2.source_version == 2
    assert {item.id for item in v1.categories} == {0, 1, 2, 3}
    assert len(v2.mappings) == 4
    assert len(v2.checksum) == 64
    assert v2.effective_at_ms > 1_700_000_000_000


def test_unknown_version_returns_business_code(sample_dir):
    bad = sample_dir / "unknown.json"
    bad.write_text(json.dumps({"version": "v9"}), encoding="utf-8")
    with pytest.raises(DiagnosticError) as exc:
        read_v1_config(bad)
    assert exc.value.code == CODES.VERSION_UNSUPPORTED


def test_missing_fields_and_illegal_categories_are_invalid(sample_dir):
    missing = sample_dir / "missing.json"
    missing.write_text(json.dumps({"version": "v1"}), encoding="utf-8")
    with pytest.raises(DiagnosticError) as exc:
        read_v1_config(missing)
    assert exc.value.code == CODES.CONFIG_INVALID

    illegal = sample_dir / "illegal.json"
    illegal.write_text(
        json.dumps(
            {
                "version": "v1",
                "relative_effective_seconds": 1,
                "categories": [1, 1],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(DiagnosticError) as exc:
        read_v1_config(illegal)
    assert exc.value.code == CODES.CONFIG_INVALID


def test_migration_records_actions_and_preserves_pixels(sample_dir):
    arrays = load_sample_arrays(sample_dir)
    v1 = read_v1_config(sample_dir / "config_v1.json")
    v2 = read_v2_config(sample_dir / "config_v2")

    upgraded, up_report = migrate_v1_to_v2(v1)
    assert upgraded.source_version == 2
    assert any(a.action == "complete_mapping" for a in up_report.actions)
    assert any("default_confidence" in a.target for a in up_report.actions)
    contract = verify_v1_upgrade_contract(arrays["v1_instance"], v1, upgraded)
    assert contract["pixel_contract_consistent"]

    for policy in ["discard", "flatten"]:
        downgraded, down_report = migrate_v2_to_v1(v2, policy=policy)
        assert downgraded.source_version == 1
        assert down_report.policy == policy
        result = verify_v2_downgrade_contract(
            arrays, v2, downgraded, policy=policy
        )
        assert result["pixel_contract_consistent"]


def test_revised_v2_config_round_trips_sqlite(sample_dir, tmp_path):
    v2 = read_v2_config(sample_dir / "config_v2")
    revised = replace(v2, revision=9)
    from seg_diag.hashing import content_key
    from seg_diag.config_writer import write_v2_config

    revised.checksum = content_key(revised.canonical_payload())
    target = tmp_path / "rev9"
    write_v2_config(target, revised)
    loaded = read_v2_config(target)
    assert loaded.revision == 9
    assert loaded.checksum == revised.checksum
