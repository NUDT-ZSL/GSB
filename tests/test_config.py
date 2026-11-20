from __future__ import annotations

import copy
import json

import pytest

from tunnel_kernel.config import load_config, validate_documents
from tunnel_kernel.errors import (
    E_BAD_CONFIG,
    E_BAD_ENDPOINT,
    E_BAD_LINK,
    E_BAD_MTU,
    E_BAD_PAYLOAD,
    E_BAD_POLICY,
    E_DUPLICATE_PEER,
    BusinessError,
)

from conftest import ROOT


def _documents():
    return (
        json.loads((ROOT / "samples" / "peers.json").read_text(encoding="utf-8")),
        json.loads((ROOT / "samples" / "links.json").read_text(encoding="utf-8")),
        json.loads((ROOT / "samples" / "policy.json").read_text(encoding="utf-8")),
    )


def test_sample_documents_validate() -> None:
    bundle = load_config(
        ROOT / "samples" / "peers.json",
        ROOT / "samples" / "links.json",
        ROOT / "samples" / "policy.json",
    )
    assert sorted(bundle.peers) == ["tcp-beta", "udp-alpha"]
    assert bundle.policy.max_concurrency == 2
    assert bundle.policy.sample_points == (96, 384, 512, 640, 1024)


def test_duplicate_peer_name_is_rejected() -> None:
    peers, links, policy = _documents()
    peers["peers"].append(copy.deepcopy(peers["peers"][0]))
    with pytest.raises(BusinessError) as caught:
        validate_documents(peers, links, policy)
    assert caught.value.code == E_DUPLICATE_PEER


def test_non_loopback_endpoint_is_rejected() -> None:
    peers, links, policy = _documents()
    peers["peers"][0]["listen"]["host"] = "192.0.2.10"
    with pytest.raises(BusinessError) as caught:
        validate_documents(peers, links, policy)
    assert caught.value.code == E_BAD_ENDPOINT


def test_non_literal_loopback_hostname_is_rejected() -> None:
    peers, links, policy = _documents()
    peers["peers"][0]["listen"]["host"] = "localhost"
    with pytest.raises(BusinessError) as caught:
        validate_documents(peers, links, policy)
    assert caught.value.code == E_BAD_ENDPOINT


@pytest.mark.parametrize("mtu", [0, -1, 80, 70_000])
def test_bad_mtu_is_rejected(mtu: int) -> None:
    peers, links, policy = _documents()
    peers["peers"][0]["outer_mtu"] = mtu
    with pytest.raises(BusinessError) as caught:
        validate_documents(peers, links, policy)
    assert caught.value.code == E_BAD_MTU


def test_payload_larger_than_encapsulation_limit_is_rejected() -> None:
    peers, links, policy = _documents()
    peers["peers"][0]["payload_lengths"] = [1200]
    with pytest.raises(BusinessError) as caught:
        validate_documents(peers, links, policy)
    assert caught.value.code == E_BAD_PAYLOAD


def test_zero_payload_length_is_rejected() -> None:
    peers, links, policy = _documents()
    peers["peers"][0]["payload_lengths"] = [0]
    with pytest.raises(BusinessError) as caught:
        validate_documents(peers, links, policy)
    assert caught.value.code == E_BAD_PAYLOAD


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("loss_rate", 1.1),
        ("loss_rate", -0.1),
        ("jitter_ms", -1.0),
        ("rtt_ms", -1.0),
        ("bandwidth_mbps", 0.0),
    ],
)
def test_bad_link_metrics_are_rejected(field: str, value: float) -> None:
    peers, links, policy = _documents()
    links["profiles"]["udp-alpha"][field] = value
    with pytest.raises(BusinessError) as caught:
        validate_documents(peers, links, policy)
    assert caught.value.code == E_BAD_LINK


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_concurrency", 0),
        ("round_budget_ms", 0),
        ("logical_clock_step_ms", 0),
        ("io_timeout_ms", 0),
    ],
)
def test_bad_policy_positive_counts_are_rejected(field: str, value: int) -> None:
    peers, links, policy = _documents()
    policy[field] = value
    with pytest.raises(BusinessError) as caught:
        validate_documents(peers, links, policy)
    assert caught.value.code == E_BAD_POLICY


def test_sample_points_must_be_strictly_increasing() -> None:
    peers, links, policy = _documents()
    policy["sample_points"] = [100, 100, 200]
    with pytest.raises(BusinessError) as caught:
        validate_documents(peers, links, policy)
    assert caught.value.code == E_BAD_POLICY


def test_missing_link_profile_is_rejected() -> None:
    peers, links, policy = _documents()
    del links["profiles"]["udp-alpha"]
    with pytest.raises(BusinessError) as caught:
        validate_documents(peers, links, policy)
    assert caught.value.code == "E_NO_PROFILE"
