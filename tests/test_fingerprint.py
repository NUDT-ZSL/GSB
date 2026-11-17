from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ml_gateway.fingerprint import (
    derive_fingerprint,
    materialize_backend_states,
)


def test_initial_fingerprints_are_independent_of_order_and_concurrency(sample_bundle):
    forward = materialize_backend_states(sample_bundle.backends, {})
    reverse = materialize_backend_states(tuple(reversed(sample_bundle.backends)), {})
    assert {key: value.fingerprint for key, value in forward.items()} == {
        key: value.fingerprint for key, value in reverse.items()
    }

    with ThreadPoolExecutor(max_workers=8) as executor:
        concurrent = list(
            executor.map(
                lambda _: materialize_backend_states(sample_bundle.backends, {}),
                range(32),
            )
        )
    expected = {key: value.fingerprint for key, value in forward.items()}
    assert all(
        {key: value.fingerprint for key, value in states.items()} == expected
        for states in concurrent
    )


def test_initial_load_sequence_is_zero_and_reload_preserves_or_increments(sample_bundle):
    first = materialize_backend_states(sample_bundle.backends, {})
    assert all(state.load_sequence == 0 for state in first.values())
    unchanged = materialize_backend_states(sample_bundle.backends, first)
    assert {key: value.fingerprint for key, value in unchanged.items()} == {
        key: value.fingerprint for key, value in first.items()
    }


def test_parameter_last_bit_changes_fingerprint(sample_bundle, evolved_bundle):
    alpha_v1 = sample_bundle.backend_by_id["be-alpha"]
    alpha_v2 = evolved_bundle.backend_by_id["be-alpha"]
    assert alpha_v1.canonical_vector() != alpha_v2.canonical_vector()
    fp1, _, _ = derive_fingerprint(alpha_v1, 0)
    fp2, _, _ = derive_fingerprint(alpha_v2, 0)
    assert fp1 != fp2
    assert fp1.startswith("fp1:") and len(fp1) == 68


def test_load_sequence_is_part_of_fingerprint(sample_bundle):
    alpha = sample_bundle.backend_by_id["be-alpha"]
    fp0 = derive_fingerprint(alpha, 0)[0]
    fp1 = derive_fingerprint(alpha, 1)[0]
    assert fp0 != fp1


def test_configuration_changes_fingerprint_without_vector_change(sample_bundle):
    alpha = sample_bundle.backend_by_id["be-alpha"]
    from dataclasses import replace

    changed = replace(alpha, max_concurrency=alpha.max_concurrency + 1)
    assert derive_fingerprint(alpha, 0)[0] != derive_fingerprint(changed, 0)[0]
