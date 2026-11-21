"""TTL conversion with precision accounting.

Every conversion of a TTL into an absolute expiry moment produces a
ConversionRecord capturing the scale, rounding direction, the discarded
sub-millisecond remainder and the resulting error bound. These records are
serialized into the precision ledger.
"""
from dataclasses import dataclass, asdict

ROUND_EXACT = "exact"
ROUND_DOWN = "down"  # truncation toward the past (floor)

# Upper bound of truncation uncertainty per source representation.
GRANULARITY_SECOND_MS = 1000  # v1 remaining-seconds / upstream TTL seconds
GRANULARITY_MS = 0            # v2 absolute millisecond timestamps


@dataclass
class ConversionRecord:
    kind: str = "convert"
    domain: str = ""
    rtype: str = ""
    level: str = ""
    source_version: str = ""       # v1 | v2 | upstream
    input_repr: str = ""           # remaining_seconds | abs_ms | upstream_ttl_seconds
    input_value: int = 0
    now_us: int = 0
    scale: int = 1                 # multiplier applied to reach ms
    rounding: str = ROUND_EXACT
    discarded_sub_ms_us: int = 0   # sub-ms remainder dropped by ms truncation
    output_expiry_ms: int = 0
    error_bound_ms: float = 0.0

    def to_json(self):
        return asdict(self)


def convert_remaining_seconds(ttl_s, now_us, domain, rtype, level,
                              source_version, input_repr="remaining_seconds"):
    """remaining seconds -> absolute expiry ms under the injected logical clock.

    The source value is an integer number of seconds, so the true remaining
    lifetime lies in [ttl_s, ttl_s+1)s: the error bound is 1000ms. The ms
    truncation itself is exact for whole-second inputs; the discarded sub-ms
    remainder is still recorded for the ledger.
    """
    expiry_us = now_us + int(ttl_s) * 1_000_000
    expiry_ms = expiry_us // 1000
    rem_us = expiry_us % 1000
    rec = ConversionRecord(
        domain=domain, rtype=rtype, level=level, source_version=source_version,
        input_repr=input_repr, input_value=int(ttl_s), now_us=now_us,
        scale=1000, rounding=ROUND_DOWN, discarded_sub_ms_us=rem_us,
        output_expiry_ms=expiry_ms,
        error_bound_ms=float(GRANULARITY_SECOND_MS),
    )
    return expiry_ms, rec


def passthrough_abs_ms(expiry_ms, now_us, domain, rtype, level, source_version):
    """v2 absolute ms timestamps need no rescaling; recorded for traceability."""
    rec = ConversionRecord(
        domain=domain, rtype=rtype, level=level, source_version=source_version,
        input_repr="abs_ms", input_value=int(expiry_ms), now_us=now_us,
        scale=1, rounding=ROUND_EXACT, discarded_sub_ms_us=0,
        output_expiry_ms=int(expiry_ms),
        error_bound_ms=float(GRANULARITY_MS),
    )
    return int(expiry_ms), rec
