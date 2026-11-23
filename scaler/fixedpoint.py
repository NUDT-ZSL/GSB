"""Deterministic fixed point ledger with rounding and error propagation.

All business quantities (requested replicas, effective resources, cost deltas,
budget headroom) are quantized against the declared scale ``S`` and stored as
integers.  Every conversion, addition and multiplication is registered in a
monotonic ledger; each entry records the rounding direction, the discarded
remainder and the accumulated upper error bound (in ticks) of its result.

The ledger is pure integer/``decimal`` arithmetic - no binary floats participate
in decisions - so two runs over identical input produce byte identical entries.
"""

from dataclasses import dataclass, field, asdict
from decimal import Decimal, localcontext, ROUND_HALF_UP, ROUND_CEILING, ROUND_FLOOR

from .errors import bad_sample


ROUND_UP = "up"
ROUND_DOWN = "down"
ROUND_EXACT = "exact"


@dataclass(frozen=True)
class Q:
    """A quantized value: integer ticks plus an accumulated error bound."""

    ticks: int
    err: int
    sources: tuple = field(default_factory=tuple)

    def to_dict(self):
        return {"ticks": self.ticks, "err": self.err, "sources": list(self.sources)}


@dataclass
class Entry:
    seq: int
    service: str
    path: str
    op: str
    raw: str
    ticks: int
    round_dir: str
    discarded: str
    err_bound: int
    sources: list

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, row):
        return cls(**row)


class FixedLedger:
    """Append only conversion ledger for one reconcile context.

    A ledger can be snapshotted to plain dicts and rebuilt (for crash recovery);
    rebuilt ledgers keep the original sequence numbers so error source chains
    remain stable across process restarts.
    """

    def __init__(self, scale, tolerance, entries=None, next_seq=None):
        if not isinstance(scale, int) or scale <= 0:
            raise bad_sample("fixed point scale must be a positive integer", scale=scale)
        self.scale = scale
        self.tolerance = int(tolerance)
        self.entries = list(entries or [])
        self.next_seq = int(next_seq) if next_seq is not None else (
            (self.entries[-1].seq + 1) if self.entries else 1
        )

    # ------------------------------------------------------------------ core
    def _register(self, service, path, op, raw, ticks, round_dir, discarded,
                  err_bound, sources):
        entry = Entry(
            seq=self.next_seq,
            service=service,
            path=path,
            op=op,
            raw=str(raw),
            ticks=int(ticks),
            round_dir=round_dir,
            discarded=str(discarded),
            err_bound=int(err_bound),
            sources=list(sources),
        )
        self.next_seq += 1
        self.entries.append(entry)
        return Q(entry.ticks, entry.err_bound, tuple(sources + [entry.seq]))

    def convert(self, service, value, path, rounding=ROUND_HALF_UP):
        """Quantize a raw int/float/str/Decimal value against the scale."""
        with localcontext() as ctx:
            ctx.prec = 50
            dec = Decimal(str(value))
            scaled = dec * Decimal(self.scale)
            if rounding == ROUND_HALF_UP:
                q = scaled.to_integral_value(rounding=ROUND_HALF_UP)
            elif rounding == ROUND_UP:
                q = scaled.to_integral_value(rounding=ROUND_CEILING)
            else:
                q = scaled.to_integral_value(rounding=ROUND_FLOOR)
            remainder = scaled - q
            if remainder == 0:
                round_dir = ROUND_EXACT
            elif (rounding == ROUND_UP) or (
                rounding == ROUND_HALF_UP and remainder > 0
            ):
                round_dir = ROUND_UP
            else:
                round_dir = ROUND_DOWN
            ticks = int(q)
            err_bound = 0 if round_dir == ROUND_EXACT else 1
            return self._register(
                service, path, "convert", value, ticks, round_dir,
                remainder, err_bound, [],
            )

    def exact_int(self, service, value, path):
        """Register an already integral quantity (zero conversion error)."""
        if int(value) != value:
            raise bad_sample("exact_int requires an integral value", value=value)
        return self._register(
            service, path, "convert", value, int(value) * self.scale,
            ROUND_EXACT, 0, 0, [],
        )

    def add(self, service, left, right, path):
        """Integer addition; error bounds add linearly."""
        ticks = left.ticks + right.ticks
        err = left.err + right.err
        return self._register(
            service, path, "add",
            f"{left.ticks}+{right.ticks}", ticks, ROUND_EXACT, 0, err,
            list(left.sources + right.sources),
        )

    def sub(self, service, left, right, path):
        ticks = left.ticks - right.ticks
        err = left.err + right.err
        return self._register(
            service, path, "sub",
            f"{left.ticks}-{right.ticks}", ticks, ROUND_EXACT, 0, err,
            list(left.sources + right.sources),
        )

    def mul(self, service, left, right, path, rounding=ROUND_HALF_UP):
        """Multiply two quantized values, keeping the result at scale S."""
        with localcontext() as ctx:
            ctx.prec = 50
            raw = Decimal(left.ticks) * Decimal(right.ticks) / Decimal(self.scale)
            if rounding == ROUND_UP:
                q = raw.to_integral_value(rounding=ROUND_CEILING)
            elif rounding == ROUND_DOWN:
                q = raw.to_integral_value(rounding=ROUND_FLOOR)
            else:
                q = raw.to_integral_value(rounding=ROUND_HALF_UP)
            remainder = raw - q
            round_dir = ROUND_EXACT if remainder == 0 else (
                ROUND_UP if remainder > 0 else ROUND_DOWN
            )
            propagated = (
                abs(left.ticks) * right.err
                + abs(right.ticks) * left.err
                + left.err * right.err
            )
            err_bound = (
                propagated // self.scale
                + (1 if propagated % self.scale else 0)
                + (0 if round_dir == ROUND_EXACT else 1)
            )
            return self._register(
                service, path, "mul",
                f"{left.ticks}*{right.ticks}/{self.scale}", int(q),
                round_dir, remainder, err_bound,
                list(left.sources + right.sources),
            )

    # ------------------------------------------------------------ comparison
    def diff(self, left, right):
        return abs(left.ticks - right.ticks)

    def is_sensitive(self, left, right):
        """True when the difference is below the declared tolerance.

    Error intervals are considered: sensitivity holds whenever the nominal
    difference minus both error bounds stays inside the tolerance window.
    """
        gap = abs(left.ticks - right.ticks)
        return gap - left.err - right.err < self.tolerance

    def chain_for(self, *quantums):
        """Collect ordered ledger entry sequences backing one or more Q values."""
        seq = []
        for quantum in quantums:
            for source in quantum.sources:
                if source not in seq:
                    seq.append(source)
        return seq

    def entry(self, seq):
        return self.entries[seq - 1] if 1 <= seq <= len(self.entries) else None

    # ----------------------------------------------------------- persistence
    def snapshot(self):
        return {
            "scale": self.scale,
            "tolerance": self.tolerance,
            "next_seq": self.next_seq,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def restore(cls, snapshot):
        if not snapshot:
            return None
        return cls(
            scale=snapshot["scale"],
            tolerance=snapshot["tolerance"],
            entries=[Entry.from_dict(row) for row in snapshot["entries"]],
            next_seq=snapshot["next_seq"],
        )
