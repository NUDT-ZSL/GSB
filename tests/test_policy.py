from __future__ import annotations

from pathlib import Path

from mfa_guard.errors import Codes
from mfa_guard.models import PolicyMeta, PolicyRecord, TrustedFactor
from mfa_guard.policy import build_policy_view, load_policy, parse_v1_policy


def _record(
    factor_id: str,
    generation: int,
    effective: int,
    window: int,
    *,
    tombstone: bool = False,
    source: str = "v2",
    factor_type: str | None = "TOTP",
) -> PolicyRecord:
    return PolicyRecord(
        policy_key="mfa-policy",
        factor_id=factor_id,
        policy_generation=generation,
        effective_at_ms=effective,
        verification_window_ms=window,
        ticket_generation=generation,
        ticket_generation_max=generation + 1,
        tombstone=tombstone,
        factor_type=factor_type,
        source=source,
        source_index=1,
        source_ref="test",
    )


def test_sample_policy_merges_v1_v2_and_tombstone(sample_root: Path):
    policy = load_policy(
        policy_key="mfa-policy",
        observation_clock_ms=1_767_225_600_000,
        v1_path=sample_root / "samples" / "policy_v1.json",
        v2_shard_paths=[
            sample_root / "samples" / "policy_v2" / "shard_00.json",
            sample_root / "samples" / "policy_v2" / "shard_01.json",
        ],
        v2_db_path=sample_root / "samples" / "policy_v2.sqlite",
        as_of_ms=1_767_225_700_000,
        factor_types={
            **{
                f"factor-{name}": "TOTP"
                for name in (
                    "ok",
                    "otp",
                    "fpamb",
                    "mismatch",
                    "otpamb",
                    "replay",
                    "legacy",
                    "retired",
                )
            },
            "factor-push": "PUSH",
        },
    )
    assert policy.policy_generation == 3
    assert "factor-retired" not in policy.trusted_factors
    assert "factor-legacy" in policy.trusted_factors
    assert policy.trusted_factors["factor-legacy"].window_basis.startswith("v1_")
    assert policy.trusted_factors["factor-ok"].window_basis == "v2_absolute_ms"
    assert policy.trusted_factors["factor-ok"].ticket_generation == 10
    assert policy.source_counts == {"v1": 9, "v2_shard": 7, "v2_db": 1}
    assert len(policy.policy_version) == 64
    assert not policy.issues


def test_v1_relative_window_uses_declared_observation_clock(tmp_path: Path):
    policy_path = tmp_path / "v1.json"
    policy_path.write_text(
        """
        {
          "policy_key": "mfa-policy",
          "observed_at_ms": 1000000,
          "verification_window_seconds": 1.5,
          "factor_allowlist": ["f1"],
          "stateless_ticket": true
        }
        """,
        encoding="utf-8",
    )
    parsed = parse_v1_policy(policy_path, observation_clock_ms=1_000_000)
    assert not parsed.issues
    assert parsed.records[0].verification_window_ms == 1_500
    assert parsed.records[0].effective_at_ms == 1_000_000
    assert parsed.records[0].stateless_ticket is True

    without_clock = tmp_path / "no-clock.json"
    without_clock.write_text(
        """
        {
          "policy_key": "mfa-policy",
          "verification_window_seconds": 30,
          "factor_allowlist": ["f1"],
          "stateless_ticket": true
        }
        """,
        encoding="utf-8",
    )
    parsed = parse_v1_policy(without_clock, observation_clock_ms=None)
    assert any(item.code == Codes.POLICY_CLOCK for item in parsed.issues)

    mismatched = parse_v1_policy(policy_path, observation_clock_ms=2_000_000)
    assert any(item.code == Codes.POLICY_CLOCK for item in mismatched.issues)


def test_tombstone_covers_older_record_and_readd_wins():
    records = [
        _record("factor-x", 1, 10, 30_000),
        _record("factor-x", 2, 20, 30_000, tombstone=True),
    ]
    view = build_policy_view(
        records=records,
        issues=[],
        policy_key="mfa-policy",
        as_of_ms=100,
        meta=PolicyMeta(),
        source_counts={},
        factor_types={"factor-x": "TOTP"},
    )
    assert "factor-x" not in view.trusted_factors

    records.append(_record("factor-x", 3, 30, 30_000))
    view2 = build_policy_view(
        records=records,
        issues=[],
        policy_key="mfa-policy",
        as_of_ms=100,
        meta=PolicyMeta(),
        source_counts={},
        factor_types={"factor-x": "TOTP"},
    )
    assert view2.trusted_factors["factor-x"].policy_generation == 3
    assert view2.policy_version != view.policy_version


def test_policy_version_changes_on_set_generation_or_window_change():
    base = _record("factor-x", 1, 10, 30_000)
    base_view = build_policy_view(
        records=[base],
        issues=[],
        policy_key="mfa-policy",
        as_of_ms=100,
        meta=PolicyMeta(),
        source_counts={},
        factor_types={"factor-x": "TOTP"},
    )
    variants = [
        [_record("factor-x", 2, 10, 30_000)],
        [_record("factor-x", 1, 10, 31_000)],
        [
            _record("factor-x", 1, 10, 30_000),
            _record("factor-y", 1, 10, 30_000),
        ],
    ]
    for records in variants:
        view = build_policy_view(
            records=records,
            issues=[],
            policy_key="mfa-policy",
            as_of_ms=100,
            meta=PolicyMeta(),
            source_counts={},
            factor_types={"factor-x": "TOTP", "factor-y": "PUSH"},
        )
        assert view.policy_version != base_view.policy_version


def test_same_authority_identity_conflict_rejected():
    records = [
        _record("factor-x", 2, 10, 30_000),
        _record("factor-x", 2, 10, 31_000),
    ]
    issues = []
    view = build_policy_view(
        records=records,
        issues=issues,
        policy_key="mfa-policy",
        as_of_ms=100,
        meta=PolicyMeta(),
        source_counts={},
        factor_types={"factor-x": "TOTP"},
    )
    assert any(item.code == Codes.POLICY_CONFLICT for item in view.issues)
    assert view.trusted_factors["factor-x"].verification_window_ms == 30_000


def test_v2_metadata_generation_and_canonical_scale_affect_version():
    record = _record("factor-x", 3, 10, 30_000)
    base = build_policy_view(
        records=[record],
        issues=[],
        policy_key="mfa-policy",
        as_of_ms=100,
        meta=PolicyMeta(policy_generation=3, effective_at_ms=10),
        source_counts={},
        factor_types={"factor-x": "TOTP"},
    )
    generation_changed = build_policy_view(
        records=[record],
        issues=[],
        policy_key="mfa-policy",
        as_of_ms=100,
        meta=PolicyMeta(policy_generation=4, effective_at_ms=10),
        source_counts={},
        factor_types={"factor-x": "TOTP"},
    )
    scale_changed = build_policy_view(
        records=[record],
        issues=[],
        policy_key="mfa-policy",
        as_of_ms=100,
        meta=PolicyMeta(
            policy_generation=3,
            effective_at_ms=10,
            canonical_scale=4,
            canonical_scale_defined=True,
        ),
        source_counts={},
        factor_types={"factor-x": "TOTP"},
    )
    assert generation_changed.policy_version != base.policy_version
    assert scale_changed.layout_version != base.layout_version

