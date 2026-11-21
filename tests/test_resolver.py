"""Requirement 5: multi-level cache query, CNAME chains, upstream refetch,
negative caching - all against the loopback DNS test server."""
import pytest

from conftest import T0
from dnscache.errors import BizError, E_UPSTREAM_MALFORMED
from dnscache.resolver import UpstreamClient
from dnscache.service import op_resolve


def _resolver(loaded_store, dns_server):
    return UpstreamClient("127.0.0.1", dns_server)


def test_hit(loaded_store, dns_server):
    result = op_resolve(loaded_store, "alpha.test", "A", T0, 1000,
                        _resolver(loaded_store, dns_server))
    assert result["kind"] == "HIT"
    assert result["records"] == [{"rdata": "192.0.2.10"}]
    assert result["level"] == "local"


def test_expired_triggers_refetch_and_caches(loaded_store, dns_server):
    result = op_resolve(loaded_store, "gamma.test", "A", T0, 1000,
                        _resolver(loaded_store, dns_server))
    assert result["kind"] == "REFETCH"
    assert result["records"] == [{"rdata": "10.0.0.1", "ttl_s": 20}]
    # the upstream TTL was converted into the cache under the logical clock
    entries = loaded_store.load_entries(
        loaded_store.current_snapshot_version(), "gamma.test", "A")
    upstream = next(e for e in entries if e.source_version == "upstream")
    assert upstream.expiry_ms == T0 // 1000 + 20_000
    assert upstream.error_bound_ms == 1000.0
    assert upstream.original_repr["repr"] == "upstream_ttl_seconds"


def test_nxdomain_and_nodata_are_distinguished(loaded_store, dns_server):
    up = _resolver(loaded_store, dns_server)
    nx = op_resolve(loaded_store, "nx.test", "A", T0, 1000, up)
    assert nx["kind"] == "NXDOMAIN"
    nd = op_resolve(loaded_store, "nodata.test", "A", T0 + 1_000_000, 1000, up)
    assert nd["kind"] == "NODATA"
    # negative answers are cached and hit later with distinct kinds
    nx2 = op_resolve(loaded_store, "nx.test", "A", T0 + 2_000_000, 1000, up)
    assert nx2["kind"] == "NEGATIVE_HIT_NXDOMAIN"
    nd2 = op_resolve(loaded_store, "nodata.test", "A", T0 + 3_000_000, 1000,
                     up)
    assert nd2["kind"] == "NEGATIVE_HIT_NODATA"


def test_v2_negative_cache_hit(loaded_store, dns_server):
    result = op_resolve(loaded_store, "delta.test", "A", T0, 1000,
                        _resolver(loaded_store, dns_server))
    assert result["kind"] == "NEGATIVE_HIT_NXDOMAIN"
    result = op_resolve(loaded_store, "epsilon.test", "A", T0, 1000,
                        _resolver(loaded_store, dns_server))
    assert result["kind"] == "NEGATIVE_HIT_NODATA"


def test_cname_chain_resolved_hop_by_hop(loaded_store, dns_server):
    up = _resolver(loaded_store, dns_server)
    result = op_resolve(loaded_store, "alias.test", "A", T0, 1000, up)
    assert result["kind"] == "MISS_FETCH"
    # alias -> www -> cdn, no intermediate hop skipped
    assert [(h["name"], h["target"]) for h in result["hops"]] == [
        ("alias.test", "www.test"), ("www.test", "cdn.test")]
    assert result["records"] == [{"rdata": "203.0.113.7", "ttl_s": 45}]
    # every hop cached individually
    entries = loaded_store.load_entries(
        loaded_store.current_snapshot_version())
    keys = {(e.domain, e.rtype) for e in entries}
    assert ("alias.test", "CNAME") in keys
    assert ("www.test", "CNAME") in keys
    assert ("cdn.test", "A") in keys
    # a second resolve uses the cached chain (no upstream fetch)
    again = op_resolve(loaded_store, "alias.test", "A", T0 + 500_000, 1000,
                       up)
    assert again["kind"] == "HIT"
    assert again["hops"][0]["via"] == "cache"


def test_cached_cname_followed_before_upstream(loaded_store, dns_server):
    up = _resolver(loaded_store, dns_server)
    result = op_resolve(loaded_store, "www.test", "A", T0, 1000, up)
    # www.test CNAME is in the v1 snapshot; only cdn.test needs upstream
    assert result["hops"] == [
        {"name": "www.test", "type": "CNAME", "target": "cdn.test",
         "via": "cache"}]
    assert result["records"] == [{"rdata": "203.0.113.7", "ttl_s": 45}]


def test_malformed_upstream_reply(loaded_store, dns_server):
    with pytest.raises(BizError) as exc:
        op_resolve(loaded_store, "broken.test", "A", T0, 1000,
                   _resolver(loaded_store, dns_server))
    assert exc.value.code == E_UPSTREAM_MALFORMED


def test_delayed_upstream_reply(loaded_store, dns_server):
    result = op_resolve(loaded_store, "slow.test", "A", T0, 1000,
                        _resolver(loaded_store, dns_server))
    assert result["kind"] == "MISS_FETCH"
    assert result["records"] == [{"rdata": "203.0.113.9", "ttl_s": 10}]


def test_resolve_is_idempotent(loaded_store, dns_server):
    up = _resolver(loaded_store, dns_server)
    first = op_resolve(loaded_store, "gamma.test", "A", T0, 1000, up)
    ledger_before = len(loaded_store.ledger_dump())
    second = op_resolve(loaded_store, "gamma.test", "A", T0, 1000, up)
    assert second["idempotent_replay"] is True
    assert second["kind"] == first["kind"]
    assert len(loaded_store.ledger_dump()) == ledger_before
