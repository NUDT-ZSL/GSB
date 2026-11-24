from __future__ import annotations

from typing import Any, Dict

from tlsdiag import codes
from tlsdiag.chain import evaluate_messages
from tlsdiag.generate import SampleBuilder
from tlsdiag.models import ReassembledMessage, canonical_json
from tlsdiag.trust import load_trust_view


def _message(kind: str, payload: Dict[str, Any], message_id: int, seq: int) -> ReassembledMessage:
    data = canonical_json(payload).encode()
    return ReassembledMessage(
        message_id=message_id,
        kind=kind,
        data=data,
        declared_length=len(data),
        first_seq=seq,
        last_seq=seq,
        direction="server_to_client",
        record_seqs=[seq],
        payload=payload,
        length_ok=True,
    )


def _trust(sample_dir):
    return load_trust_view(
        v1_bytes=(sample_dir / "input/trust_v1.json").read_bytes(),
        v1_source="v1",
        v2_shards_dir=sample_dir / "input/trust_v2",
        v2_db_path=sample_dir / "input/trust_v2.sqlite3",
    )


def test_multiple_candidates_are_ranked_by_trust(sample_dir) -> None:
    builder = SampleBuilder(20261125)
    messages = [
        _message(
            "certificate",
            builder.certificate_message(["leaf_multi", "int_multi", "root_unknown"]),
            1,
            0,
        ),
        _message(
            "certificate",
            builder.certificate_message(["leaf_multi", "int_multi", "root_alpha"]),
            2,
            1,
        ),
        _message("handshake", builder.handshake_message("multi"), 3, 2),
    ]
    observed, source, candidates, selected, issues, conclusion, primary = evaluate_messages(
        messages, [], _trust(sample_dir), "multi"
    )
    assert observed == builder.observed_ms
    assert source.startswith("handshake_message")
    assert len(candidates) == 2
    assert selected is not None
    assert selected.root_fingerprint == builder.fps["root_alpha"]
    assert selected.trusted
    assert conclusion == "trusted"
    assert primary is None
    assert candidates[0].rank == 0
    assert candidates[1].rank == 1
    assert any(i.code == codes.E_ROOT_UNTRUSTED for i in candidates[1].issues)


def test_expired_broken_and_untrusted_root_codes(sample_dir) -> None:
    builder = SampleBuilder(20261125)
    trust = _trust(sample_dir)
    cases = {
        "expired": (
            ["leaf_expired", "root_alpha"],
            codes.E_CERT_EXPIRED,
            "expired",
        ),
        "broken": (
            ["leaf_broken", "int_broken", "root_alpha"],
            codes.E_CHAIN_BROKEN,
            "broken_chain",
        ),
        "untrusted": (
            ["leaf_untrusted", "root_unknown"],
            codes.E_ROOT_UNTRUSTED,
            "untrusted_root",
        ),
    }
    for name, (chain, expected_code, expected_conclusion) in cases.items():
        messages = [
            _message("certificate", builder.certificate_message(chain), 1, 0),
            _message("handshake", builder.handshake_message(name), 2, 1),
        ]
        *_, selected, issues, conclusion, primary = evaluate_messages(
            messages, [], trust, name
        )
        assert selected is not None
        assert primary == expected_code
        assert conclusion == expected_conclusion
        assert expected_code in {i.code for i in issues}


def test_leaf_and_intermediate_revocation_roles(sample_dir) -> None:
    builder = SampleBuilder(20261125)
    trust = _trust(sample_dir)
    leaf_messages = [
        _message(
            "certificate",
            builder.certificate_message(["leaf_revoked", "int_ok", "root_alpha"]),
            1,
            0,
        ),
        _message("handshake", builder.handshake_message("leaf"), 2, 1),
    ]
    *_, leaf_selected, leaf_issues, leaf_conclusion, leaf_primary = evaluate_messages(
        leaf_messages, [], trust, "leaf"
    )
    assert leaf_primary == codes.E_REVOKED_LEAF
    assert leaf_conclusion == "revoked_leaf"
    assert leaf_selected.revoked_roles[0]["role"] == "leaf"

    intermediate_messages = [
        _message(
            "certificate",
            builder.certificate_message(["leaf_child", "int_revoked", "root_alpha"]),
            1,
            0,
        ),
        _message("handshake", builder.handshake_message("int"), 2, 1),
    ]
    *_, int_selected, int_issues, int_conclusion, int_primary = evaluate_messages(
        intermediate_messages, [], trust, "int"
    )
    assert int_primary == codes.E_REVOKED_INTERMEDIATE
    assert int_conclusion == "revoked_intermediate"
    assert int_selected.revoked_roles[0]["role"] == "intermediate"


def test_empty_chain(sample_dir) -> None:
    builder = SampleBuilder(20261125)
    messages = [
        _message("certificate", builder.certificate_message([]), 1, 0),
        _message("handshake", builder.handshake_message("empty"), 2, 1),
    ]
    *_, selected, issues, conclusion, primary = evaluate_messages(
        messages, [], _trust(sample_dir), "empty"
    )
    assert selected is None
    assert conclusion == "empty_chain"
    assert primary == codes.E_CHAIN_EMPTY
