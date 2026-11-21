"""Cross-version snapshot merge and the unified multi-level cache index.

Arbitration rule for the same (domain, type, level) present in several
sources: highest declared logical clock wins; ties break by cache generation,
then by source-format priority (v2 > v1 > upstream). Every decision keeps the
source version, the original time representation, the original TTL value and
the arbitration rationale for later error tracing.
"""
from dataclasses import dataclass, field, asdict

from . import ttl as ttlmod

LEVEL_ORDER = ("authoritative", "recursive", "local")
FORMAT_PRIORITY = {"v2": 3, "v1": 2, "upstream": 1}


@dataclass
class Entry:
    domain: str
    rtype: str
    level: str
    source_version: str            # v1 | v2 | upstream
    expiry_ms: int
    error_bound_ms: float
    records: list = field(default_factory=list)
    negative: bool = False
    generation: int = 0
    original_repr: dict = field(default_factory=dict)
    arbitration: dict = field(default_factory=dict)
    snapshot_version: int = 0

    def key(self):
        return (self.domain, self.rtype, self.level)

    def to_row(self):
        return {
            "domain": self.domain, "rtype": self.rtype, "level": self.level,
            "source_version": self.source_version, "expiry_ms": self.expiry_ms,
            "error_bound_ms": self.error_bound_ms, "records": self.records,
            "negative": self.negative, "generation": self.generation,
            "original_repr": self.original_repr, "arbitration": self.arbitration,
            "snapshot_version": self.snapshot_version,
        }

    @classmethod
    def from_row(cls, row):
        return cls(
            domain=row["domain"], rtype=row["rtype"], level=row["level"],
            source_version=row["source_version"], expiry_ms=row["expiry_ms"],
            error_bound_ms=row["error_bound_ms"], records=row["records"],
            negative=bool(row["negative"]), generation=row["generation"],
            original_repr=row["original_repr"], arbitration=row["arbitration"],
            snapshot_version=row["snapshot_version"],
        )


@dataclass
class ComparisonRecord:
    kind: str = "compare"
    domain: str = ""
    rtype: str = ""
    order_index: int = 0
    left: dict = field(default_factory=dict)
    right: dict = field(default_factory=dict)
    winner_level: str = ""
    diff_ms: int = 0
    combined_bound_ms: float = 0.0
    reliable: bool = True           # False when |diff| <= combined error bound

    def to_json(self):
        return asdict(self)


def _entry_view(entry):
    return {"level": entry.level, "expiry_ms": entry.expiry_ms,
            "error_bound_ms": entry.error_bound_ms,
            "source_version": entry.source_version}


def arbitrate(candidates):
    """Pick the newest entry among same-key candidates; record the rationale."""
    def rank(item):
        snap_clock, generation, entry = item
        return (snap_clock, generation, FORMAT_PRIORITY[entry.source_version])

    ordered = sorted(candidates, key=rank)
    snap_clock, generation, winner = ordered[-1]
    rationale = {
        "rule": "logical_clock,generation,format_priority",
        "winner_source": winner.source_version,
        "winner_clock_us": snap_clock,
        "winner_generation": generation,
        "contenders": [
            {"source": e.source_version, "clock_us": c, "generation": g}
            for c, g, e in ordered
        ],
    }
    return winner, rationale


def merge_snapshots(snapshots, now_us):
    """Merge validated v1/v2 snapshots into unified entries + ledger records.

    Returns (entries, ledger_records): one ConversionRecord per merged entry,
    in deterministic (domain, type, level) key order.
    """
    by_key = {}
    for snap in snapshots:
        for raw in snap.entries:
            if snap.format == "v1":
                expiry_ms, conv = ttlmod.convert_remaining_seconds(
                    raw["ttl_remaining_s"], now_us, raw["name"], raw["type"],
                    raw["level"], "v1")
                entry = Entry(
                    domain=raw["name"], rtype=raw["type"], level=raw["level"],
                    source_version="v1", expiry_ms=expiry_ms,
                    error_bound_ms=conv.error_bound_ms, records=raw["records"],
                    negative=False, generation=snap.generation,
                    original_repr={"repr": "remaining_seconds",
                                   "ttl_remaining_s": raw["ttl_remaining_s"],
                                   "snapshot_clock_us": snap.logical_clock_us,
                                   "converted_at_us": now_us},
                )
            else:
                expiry_ms, conv = ttlmod.passthrough_abs_ms(
                    raw["expiry_ms"], now_us, raw["name"], raw["type"],
                    raw["level"], "v2")
                entry = Entry(
                    domain=raw["name"], rtype=raw["type"], level=raw["level"],
                    source_version="v2", expiry_ms=expiry_ms,
                    error_bound_ms=conv.error_bound_ms, records=raw["records"],
                    negative=raw["negative"], generation=raw["generation"],
                    original_repr={"repr": "abs_ms",
                                   "expiry_ms": raw["expiry_ms"],
                                   "snapshot_clock_us": snap.logical_clock_us,
                                   "converted_at_us": now_us,
                                   "negative_kind": raw.get("negative_kind")},
                )
            by_key.setdefault(entry.key(), []).append(
                (snap.logical_clock_us, entry.generation, entry, conv))
    entries = []
    ledger = []
    for key in sorted(by_key):
        group = by_key[key]
        winner, rationale = arbitrate(
            [(c, g, e) for c, g, e, _ in group])
        conv = next(cv for _, _, e, cv in group if e is winner)
        winner.arbitration = rationale
        entries.append(winner)
        ledger.append(conv)
    return entries, ledger


def fold_min_expiry(entries):
    """Ordered min-expiry fold across levels for one (domain, type).

    Entries are folded in the fixed LEVEL_ORDER sequence; every comparison is
    recorded. Never an unordered reduce. Returns (winner, [ComparisonRecord]).
    """
    by_level = {}
    for entry in entries:
        by_level[entry.level] = entry
    present = [lv for lv in LEVEL_ORDER if lv in by_level]
    current = by_level[present[0]]
    records = []
    for order_index, level in enumerate(present[1:]):
        challenger = by_level[level]
        diff = current.expiry_ms - challenger.expiry_ms
        combined = current.error_bound_ms + challenger.error_bound_ms
        if challenger.expiry_ms < current.expiry_ms:
            winner = challenger
        else:
            winner = current
        records.append(ComparisonRecord(
            domain=current.domain, rtype=current.rtype, order_index=order_index,
            left=_entry_view(current), right=_entry_view(challenger),
            winner_level=winner.level, diff_ms=diff,
            combined_bound_ms=combined,
            reliable=abs(diff) > combined,
        ))
        current = winner
    return current, records
