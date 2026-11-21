"""Multi-level cache query + upstream refetch against the loopback test
server. CNAME chains are resolved hop by hop and every hop is cached."""
import socket

import dnslib

from . import ttl as ttlmod
from .errors import (
    BizError, E_UPSTREAM_MALFORMED, E_UPSTREAM_TIMEOUT,
    E_UPSTREAM_UNREACHABLE,
)
from .merge import Entry, fold_min_expiry
from .normalize import canon
from .precision import analyze_comparisons, analyze_expiry

DEFAULT_NEGATIVE_TTL_S = 30
MAX_CNAME_HOPS = 8

HIT = "HIT"
NEGATIVE_HIT_NXDOMAIN = "NEGATIVE_HIT_NXDOMAIN"
NEGATIVE_HIT_NODATA = "NEGATIVE_HIT_NODATA"
REFETCH = "REFETCH"        # was cached but expired -> went upstream
MISS_FETCH = "MISS_FETCH"  # not cached -> went upstream
NXDOMAIN = "NXDOMAIN"
NODATA = "NODATA"


class UpstreamClient:
    def __init__(self, host="127.0.0.1", port=15353, timeout=2.0):
        self.host = host
        self.port = int(port)
        self.timeout = timeout

    def query(self, name, rtype):
        question = dnslib.DNSRecord.question(name, rtype)
        try:
            data = question.send(self.host, self.port, tcp=False,
                                 timeout=self.timeout)
        except socket.timeout:
            raise BizError(E_UPSTREAM_TIMEOUT, "upstream timed out",
                           {"name": name, "type": rtype})
        except OSError as exc:
            raise BizError(E_UPSTREAM_UNREACHABLE, "upstream unreachable",
                           {"name": name, "type": rtype, "error": str(exc)})
        try:
            return dnslib.DNSRecord.parse(data)
        except Exception as exc:
            raise BizError(E_UPSTREAM_MALFORMED, "malformed upstream reply",
                           {"name": name, "type": rtype, "error": str(exc)})


def conversion_of(entry):
    """Reconstruct the exact conversion record for a stored entry."""
    orig = entry.original_repr
    repr_kind = orig.get("repr")
    if repr_kind in ("remaining_seconds", "upstream_ttl_seconds"):
        ttl_s = orig.get("ttl_remaining_s", orig.get("ttl_s"))
        _, conv = ttlmod.convert_remaining_seconds(
            ttl_s, orig["converted_at_us"], entry.domain, entry.rtype,
            entry.level, entry.source_version, input_repr=repr_kind)
        return conv
    _, conv = ttlmod.passthrough_abs_ms(
        orig["expiry_ms"], orig["converted_at_us"], entry.domain,
        entry.rtype, entry.level, entry.source_version)
    return conv


class ResolveSession:
    """Accumulates ledger records, cache writes and findings for a resolve."""

    def __init__(self, entries_index, now_us, tolerance_ms, upstream,
                 snapshot_version):
        self.index = entries_index  # {(domain, rtype, level): Entry}
        self.now_us = now_us
        self.now_ms = now_us // 1000
        self.tolerance_ms = tolerance_ms
        self.upstream = upstream
        self.snapshot_version = snapshot_version
        self.ledger = []      # ConversionRecord / ComparisonRecord
        self.writes = []      # Entry objects to upsert
        self.findings = []

    def _entries_for(self, domain, rtype):
        return [e for (d, t, _), e in self.index.items()
                if d == domain and t == rtype]

    def _effective(self, domain, rtype):
        entries = self._entries_for(domain, rtype)
        if not entries:
            return None
        winner, comparisons = fold_min_expiry(entries)
        self.ledger.extend(comparisons)
        conversions = [conversion_of(e) for e in entries]
        self.findings.extend(analyze_comparisons(
            domain, rtype, comparisons, self.tolerance_ms, conversions))
        self.findings.extend(analyze_expiry(
            winner, self.now_ms, self.tolerance_ms, conversions))
        return winner

    def _cache_upstream_entry(self, domain, rtype, ttl_s, records, negative,
                              negative_kind=None):
        expiry_ms, conv = ttlmod.convert_remaining_seconds(
            ttl_s, self.now_us, domain, rtype, "recursive", "upstream",
            input_repr="upstream_ttl_seconds")
        original = {"repr": "upstream_ttl_seconds", "ttl_s": int(ttl_s),
                    "converted_at_us": self.now_us}
        if negative_kind:
            original["negative_kind"] = negative_kind
        entry = Entry(
            domain=domain, rtype=rtype, level="recursive",
            source_version="upstream", expiry_ms=expiry_ms,
            error_bound_ms=conv.error_bound_ms, records=records,
            negative=negative, generation=0, original_repr=original,
            arbitration={"rule": "upstream_fetch"},
            snapshot_version=self.snapshot_version)
        self.ledger.append(conv)
        self.writes.append(entry)
        self.index[entry.key()] = entry
        return entry

    def resolve(self, name, rtype, hops=None, depth=0):
        hops = hops if hops is not None else []
        domain = canon(name)
        rtype = rtype.upper()
        if depth > MAX_CNAME_HOPS:
            raise BizError("E4010_CNAME_LOOP", "CNAME chain too long",
                           {"name": domain})
        effective = self._effective(domain, rtype)
        if effective is not None and effective.expiry_ms > self.now_ms:
            if effective.negative:
                kind = effective.original_repr.get("negative_kind", "NODATA")
                result_kind = (NEGATIVE_HIT_NXDOMAIN if kind == "NXDOMAIN"
                               else NEGATIVE_HIT_NODATA)
                return {"kind": result_kind, "name": domain, "type": rtype,
                        "records": [], "hops": hops,
                        "level": effective.level}
            return {"kind": HIT, "name": domain, "type": rtype,
                    "records": effective.records, "hops": hops,
                    "level": effective.level}
        was_expired = effective is not None
        if rtype != "CNAME":
            cname_entry = self._effective(domain, "CNAME")
            if (cname_entry is not None and not cname_entry.negative
                    and cname_entry.expiry_ms > self.now_ms
                    and cname_entry.records):
                target = canon(cname_entry.records[0]["target"])
                hops.append({"name": domain, "type": "CNAME",
                             "target": target, "via": "cache"})
                return self.resolve(target, rtype, hops, depth + 1)
        return self._fetch(domain, rtype, was_expired, hops, depth)

    def _fetch(self, domain, rtype, was_expired, hops, depth):
        if self.upstream is None:
            return {"kind": "MISS_NO_UPSTREAM", "name": domain,
                    "type": rtype, "records": [], "hops": hops}
        resp = self.upstream.query(domain, rtype)
        rcode = resp.header.rcode
        neg_ttl = DEFAULT_NEGATIVE_TTL_S
        for rr in resp.auth:
            if rr.rtype == dnslib.QTYPE.SOA:
                neg_ttl = int(rr.ttl)
        fetch_kind = REFETCH if was_expired else MISS_FETCH
        if rcode == dnslib.RCODE.NXDOMAIN:
            self._cache_upstream_entry(domain, rtype, neg_ttl, [], True,
                                       negative_kind="NXDOMAIN")
            return {"kind": NXDOMAIN, "name": domain, "type": rtype,
                    "records": [], "hops": hops, "fetch": fetch_kind}
        answers = resp.rr
        if not answers:
            self._cache_upstream_entry(domain, rtype, neg_ttl, [], True,
                                       negative_kind="NODATA")
            return {"kind": NODATA, "name": domain, "type": rtype,
                    "records": [], "hops": hops, "fetch": fetch_kind}
        final_records = []
        final_name = None
        for rr in answers:
            rr_name = canon(str(rr.rname))
            if rr.rtype == dnslib.QTYPE.CNAME:
                target = canon(str(rr.rdata))
                self._cache_upstream_entry(
                    rr_name, "CNAME", int(rr.ttl),
                    [{"target": target}], False)
                hops.append({"name": rr_name, "type": "CNAME",
                             "target": target, "via": "upstream"})
            elif rr.rtype == getattr(dnslib.QTYPE, rtype, -1):
                final_records.append({"rdata": str(rr.rdata),
                                      "ttl_s": int(rr.ttl)})
                final_name = rr_name
        if final_records:
            self._cache_upstream_entry(final_name, rtype,
                                       final_records[0]["ttl_s"],
                                       final_records, False)
            return {"kind": fetch_kind, "name": domain, "type": rtype,
                    "records": final_records, "hops": hops}
        if hops and depth < MAX_CNAME_HOPS:
            return self.resolve(hops[-1]["target"], rtype, hops, depth + 1)
        return {"kind": NODATA, "name": domain, "type": rtype,
                "records": [], "hops": hops, "fetch": fetch_kind}
