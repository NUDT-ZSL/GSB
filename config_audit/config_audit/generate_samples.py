"""Generate deterministic local JSON sample archives.

All numbers are hand-picked literals; nothing random or network based.  The
generator is idempotent: running it repeatedly rewrites byte-identical files
(json.dump with sorted keys and a fixed indent).
"""

from __future__ import annotations

import json
from pathlib import Path

# --- Baseline, mutually consistent nodes -----------------------------------
NODE_A = {
    "node_id": "node-a",
    "version": "v1",
    "items": [
        {"key": "http.timeout", "value": 1000, "unit": "ms", "source": "manual"},
        {"key": "retry.count", "value": 3, "unit": "count", "source": "manual"},
        {"key": "pool.max_conn", "value": 100, "unit": "connections",
         "source": "manual"},
        {"key": "sampling.rate", "value": 0.25, "unit": "ratio",
         "source": "manual"},
        {"key": "refresh.interval", "value": 500, "unit": "ms",
         "source": "manual"},
        # derived: total backoff budget = 3 * retry.count + 100
        {"key": "backoff.budget", "unit": "ms", "source": "derived",
         "formula": {"coefficients": {"retry.count": 3}, "constant": 100}},
        # multi-level: effective timeout = http.timeout + 2 * backoff.budget
        {"key": "effective.timeout", "unit": "ms", "source": "derived",
         "formula": {
             "coefficients": {"http.timeout": 1, "backoff.budget": 2},
             "constant": 0,
         }},
    ],
}

NODE_B = {
    "node_id": "node-b",
    "version": "v1",
    "items": [
        # Deliberately 12 ms above node-a to exercise tolerance / deviation.
        {"key": "http.timeout", "value": 1012, "unit": "ms", "source": "manual"},
        {"key": "retry.count", "value": 3, "unit": "count", "source": "manual"},
        {"key": "pool.max_conn", "value": 103, "unit": "connections",
         "source": "manual"},
        # 0.25 vs 0.249 -> only distinguishable at finer grids.
        {"key": "sampling.rate", "value": 0.249, "unit": "ratio",
         "source": "manual"},
        {"key": "refresh.interval", "value": 500, "unit": "ms",
         "source": "manual"},
        {"key": "backoff.budget", "unit": "ms", "source": "derived",
         "formula": {"coefficients": {"retry.count": 3}, "constant": 100}},
        {"key": "effective.timeout", "unit": "ms", "source": "derived",
         "formula": {
             "coefficients": {"http.timeout": 1, "backoff.budget": 2},
             "constant": 0,
         }},
    ],
}

# Same content as NODE_A but bumped archive version -> snapshot whole-replace.
NODE_A_V2 = {**NODE_A, "version": "v2"}

# --- E_RANGE injection: a value above its unit bound -----------------------
NODE_RANGE_BAD = {
    "node_id": "node-range",
    "version": "v1",
    "items": [
        # ratio must be within [0, 1].
        {"key": "sampling.rate", "value": 1.5, "unit": "ratio",
         "source": "manual"},
        {"key": "http.timeout", "value": 1000, "unit": "ms",
         "source": "manual"},
    ],
}

# --- E_CYCLE injection: a -> b -> c -> a -----------------------------------
NODE_CYCLE = {
    "node_id": "node-cycle",
    "version": "v1",
    "items": [
        {"key": "chain.a", "value": 10, "unit": "count", "source": "manual"},
        {"key": "chain.b", "unit": "count", "source": "derived",
         "formula": {"coefficients": {"chain.a": 1, "chain.c": 1},
                     "constant": 0}},
        {"key": "chain.c", "unit": "count", "source": "derived",
         "formula": {"coefficients": {"chain.b": 1}, "constant": 0}},
    ],
}

# --- E_CONFIG injection: malformed archive (bad key, bad unit, duplicate) --
NODE_BAD = {
    "node_id": "node-bad",
    "version": "v1",
    "items": [
        {"key": "BAD-KEY", "value": 1, "unit": "ms", "source": "manual"},
        {"key": "http.timeout", "value": 1, "unit": "furlong",
         "source": "manual"},
        {"key": "http.timeout", "value": 2, "unit": "ms", "source": "manual"},
    ],
}

# --- E_PROPAGATE fixture: derived references an upstream that exists but has
# no determinable bound.  The loader rejects truly missing references, so this
# fixture is assembled through the API in tests; here we keep a missing-ref
# variant to document the E_CONFIG path, and tests construct E_PROPAGATE. -----
NODE_MISSING_REF = {
    "node_id": "node-missing",
    "version": "v1",
    "items": [
        {"key": "derived.x", "unit": "ms", "source": "derived",
         "formula": {"coefficients": {"ghost.key": 2}, "constant": 0}},
    ],
}

FILES = {
    "node_a.json": NODE_A,
    "node_b.json": NODE_B,
    "node_a_v2.json": NODE_A_V2,
    "node_range_bad.json": NODE_RANGE_BAD,
    "node_cycle.json": NODE_CYCLE,
    "node_bad.json": NODE_BAD,
    "node_missing_ref.json": NODE_MISSING_REF,
}


def write_samples(target_dir: str | Path) -> list[Path]:
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in sorted(FILES):
        path = target_dir / name
        path.write_text(
            json.dumps(FILES[name], sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return written


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "samples"
    paths = write_samples(out)
    for path in paths:
        print(f"wrote {path}")
