"""Deterministically generate the local sample bundle.

Run with::

    python scripts/generate_samples.py [output_dir]

The generator uses a fixed-seed LCG (no network, no wall-clock randomness) so
every run rewrites byte-identical samples plus a SHA-256 manifest used by the
loader to detect tampering (``E_SAMPLE_MISMATCH``).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List

SEED = 20261123
MODULUS = 2**31
MULTIPLIER = 1103515245
INCREMENT = 12345


class Lcg:
    """glibc-style linear congruential generator, fully deterministic."""

    def __init__(self, seed: int = SEED) -> None:
        self.state = seed

    def next_u32(self) -> int:
        self.state = (MULTIPLIER * self.state + INCREMENT) % MODULUS
        return self.state

    def below(self, upper: int) -> int:
        return self.next_u32() % upper


def build_slos() -> List[dict]:
    return [
        {
            "name": "checkout-availability",
            "service": "checkout",
            "sli_type": "availability",
            "target": 99.0,
            "window_length_ms": 2000,
            "window_step_ms": 1000,
            "compliance_period_ms": 5000,
            "burn_alert_threshold": 2.0,
            "burn_consecutive_windows": 2,
            "release_consecutive_windows": 2,
        },
        {
            "name": "search-latency",
            "service": "search",
            "sli_type": "latency",
            "target": 95.0,
            "window_length_ms": 2000,
            "window_step_ms": 1000,
            "compliance_period_ms": 5000,
            "burn_alert_threshold": 1.5,
            "burn_consecutive_windows": 2,
            "release_consecutive_windows": 2,
            "latency_threshold_ms": 120,
            "latency_percentile": 95,
        },
    ]


def build_events(rng: Lcg) -> List[dict]:
    events: List[dict] = []
    seq = 1
    # 12 windows of 1000ms each; inject a late event and an empty window.
    for clock in range(0, 12000, 50):
        for service, base_failure, base_latency in (
            ("checkout", 0.08, 60),
            ("search", 0.12, 90),
        ):
            # Leave window 7 (7000-7999) of checkout empty on purpose.
            if service == "checkout" and 7000 <= clock < 8000:
                continue
            # Elevated failure rate in windows 3-4 to trigger burn streaks.
            failure_rate = base_failure
            if 3000 <= clock < 5000:
                failure_rate = 0.45
            valid = rng.below(100) >= 3  # ~3% invalid requests
            success = rng.below(100) >= int(failure_rate * 100)
            latency = base_latency + rng.below(80)
            if rng.below(20) == 0:
                latency += 120  # latency tail
            events.append(
                {
                    "seq": seq,
                    "service": service,
                    "clock_ms": clock,
                    "success": bool(success),
                    "latency_ms": latency,
                    "valid": bool(valid),
                }
            )
            seq += 1

    # Deterministic late events: high sequence number, old logical clock.  They
    # are appended *after* regular events and their home window (window 1,
    # 1000-1999ms) has already closed by the time their sequence is reached, so
    # the engine must record them as late instead of mutating window 1.
    events.append(
        {
            "seq": seq,
            "service": "checkout",
            "clock_ms": 1500,
            "success": False,
            "latency_ms": 70,
            "valid": True,
        }
    )
    seq += 1
    events.append(
        {
            "seq": seq,
            "service": "search",
            "clock_ms": 1750,
            "success": True,
            "latency_ms": 88,
            "valid": True,
        }
    )
    return events


def build_clock() -> dict:
    # Ticks close every window at length 2000ms; include one small backwards
    # step within tolerance and one out-of-tolerance rollback for diagnostics.
    ticks = list(range(1000, 13000, 1000))
    ticks.append(12900)  # within tolerance (100ms)
    ticks.append(9000)   # beyond tolerance -> E_CLOCK_ROLLBACK, isolated
    return {"ticks_ms": ticks, "rollback_tolerance_ms": 100}


def write_json(path: Path, payload) -> None:
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def generate(output_dir: Path) -> Dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rng = Lcg()
    files = {
        "slos.json": build_slos(),
        "events.json": build_events(rng),
        "clock.json": build_clock(),
    }
    digests: Dict[str, str] = {}
    for name, payload in sorted(files.items()):
        target = output_dir / name
        write_json(target, payload)
        digests[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {
        "generator": "scripts/generate_samples.py",
        "seed": SEED,
        "files": dict(sorted(digests.items())),
    }
    write_json(output_dir / "manifest.json", manifest)
    return digests


def main(argv: List[str]) -> int:
    output_dir = Path(argv[1]) if len(argv) > 1 else Path("samples/default")
    digests = generate(output_dir)
    print(json.dumps({"output_dir": str(output_dir), "digests": digests}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
