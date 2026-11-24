"""Deterministic sample generator (fixed seed, offline, no network).

Generates into --out:
  declarations.json      - generation-1 region declarations
  declarations_v2.json   - generation-2 evolution (interval change + tombstone)
  traces_batch1.jsonl    - first arrival batch (shuffled, gaps, duplicates,
                           plus a few intentionally invalid lines)
  traces_batch2.jsonl    - second arrival batch (fills some gaps, more dups)
  truth.json             - ground-truth sync marker: expected per-region
                           verdicts computed by an independent reference
                           implementation, plus a canonical trace digest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os

import numpy as np

ARENA = 4096

DECLS_V1 = [
    {"region_id": "R_ARENA", "generation": 1, "arena_size": ARENA,
     "start": 0, "end": 4096, "life_start_ms": 0, "life_end_ms": None,
     "parent_id": None},
    {"region_id": "R_HEAP", "generation": 1, "arena_size": ARENA,
     "start": 256, "end": 2048, "life_start_ms": 0, "life_end_ms": None,
     "parent_id": "R_ARENA"},
    {"region_id": "R_BUF", "generation": 1, "arena_size": ARENA,
     "start": 512, "end": 768, "life_start_ms": 0, "life_end_ms": 1000,
     "parent_id": "R_HEAP"},
    {"region_id": "R_TMP", "generation": 1, "arena_size": ARENA,
     "start": 1024, "end": 1280, "life_start_ms": 0, "life_end_ms": None,
     "parent_id": "R_HEAP"},
    {"region_id": "R_IO", "generation": 1, "arena_size": ARENA,
     "start": 2048, "end": 2560, "life_start_ms": 0, "life_end_ms": None,
     "parent_id": "R_ARENA"},
    {"region_id": "R_OLD", "generation": 1, "arena_size": ARENA,
     "start": 3000, "end": 3200, "life_start_ms": 0, "life_end_ms": None,
     "parent_id": "R_ARENA"},
]

# v2: R_IO interval shrinks (2048-2560 -> 2048-2400), R_OLD tombstoned.
DECLS_V2 = [
    {"region_id": "R_IO", "generation": 2, "arena_size": ARENA,
     "start": 2048, "end": 2400, "life_start_ms": 0, "life_end_ms": None,
     "parent_id": "R_ARENA"},
    {"region_id": "R_OLD", "generation": 2, "tombstone": True, "arena_size": ARENA,
     "start": 3000, "end": 3200, "life_start_ms": 0, "life_end_ms": None,
     "parent_id": "R_ARENA"},
]


def _access(trace_id, seq, region, base, size, kind, ts):
    return {
        "trace_id": trace_id, "access_seq": seq, "region_id": region,
        "base_offset": base, "access_size": size, "access_kind": kind,
        "arena_size": ARENA, "ts_ms": ts,
    }


def build_traces(rng):
    """Build the canonical trace set plus invalid lines."""
    recs = []
    tid = 0

    def add(region, seq, base, size, kind, ts):
        nonlocal tid
        tid += 1
        recs.append(_access(f"t{tid:04d}", seq, region, base, size, kind, ts))

    # R_BUF (512-768, parent R_HEAP 256-2048, life ends at 1000ms)
    for seq in range(0, 40):
        if seq in (7, 23):            # permanent gaps
            continue
        base = int(rng.randint(512, 720))
        add("R_BUF", seq, base, 16, "read", seq * 20)
    add("R_BUF", 40, 760, 32, "read", 800)      # 760-792: beyond own end, inside R_HEAP -> E_BOUND_OVER
    add("R_BUF", 41, 600, 16, "read", 1500)     # ts 1500 > life_end 1000 -> E_AFTER_LIFE
    add("R_BUF", 40, 760, 32, "read", 800)      # duplicate of seq 40 (new trace_id)
    add("R_BUF", 0, 2000, 64, "write", 820)     # 2000-2064: beyond R_HEAP end -> E_ANCESTOR_OVER

    # R_TMP (1024-1280): clean, but with a gap filled by batch 2
    for seq in range(0, 20):
        if seq == 11:                 # gap filled later by batch 2
            continue
        add("R_TMP", seq, int(rng.randint(1024, 1240)), 16, "read", seq * 30)

    # R_IO (2048-2560 in v1; 2048-2400 in v2): one access inside the v1
    # range but outside v2 -> verdict flips after declaration evolution.
    for seq in range(0, 10):
        add("R_IO", seq, int(rng.randint(2048, 2360)), 16, "write", seq * 10)
    add("R_IO", 10, 2440, 32, "write", 100)     # clean in v1, E_BOUND_OVER in v2

    # R_OLD (3000-3200, tombstoned in v2)
    for seq in range(0, 5):
        add("R_OLD", seq, int(rng.randint(3000, 3160)), 16, "read", seq * 50)

    invalid = [
        '{"trace_id": "badjson", "access_seq": ',                       # E_JSON_LINE
        json.dumps({"trace_id": "tf1", "access_seq": 0, "region_id": "R_TMP",
                    "base_offset": 1.5, "access_size": 4, "access_kind": "read",
                    "arena_size": ARENA, "ts_ms": 0}),                  # E_SCHEMA_FIELD (float)
        json.dumps({"trace_id": "tf2", "access_seq": 0, "region_id": "R_TMP",
                    "base_offset": 1024, "access_size": 4, "access_kind": "exec",
                    "arena_size": ARENA, "ts_ms": 0}),                  # E_KIND_UNKNOWN
        json.dumps({"trace_id": "tf3", "access_seq": 0, "region_id": "R_TMP",
                    "base_offset": 8192, "access_size": 4, "access_kind": "read",
                    "arena_size": ARENA, "ts_ms": 0}),                  # base_offset > arena_size
    ]
    return recs, invalid


# --- independent reference implementation (truth, not importing memguard) ---

def reference_truth(decls, records):
    regions = {d["region_id"]: d for d in decls if not d.get("tombstone")}

    def judge(rec):
        decl = regions.get(rec["region_id"])
        if decl is None:
            return None
        if decl["life_end_ms"] is not None and rec["ts_ms"] > decl["life_end_ms"]:
            return "E_AFTER_LIFE"
        lo = rec["base_offset"]
        hi = lo + rec["access_size"]
        anc_lo, anc_hi = 0, decl["arena_size"]
        node = decl["parent_id"]
        while node is not None:
            anc = regions[node]
            anc_lo = max(anc_lo, anc["start"])
            anc_hi = min(anc_hi, anc["end"])
            node = anc["parent_id"]
        if lo < anc_lo or hi > anc_hi:
            return "E_ANCESTOR_OVER"
        if lo < decl["start"] or hi > decl["end"]:
            return "E_BOUND_OVER"
        return None

    truth = {}
    for rec in records:
        key = (rec["region_id"], rec["access_kind"])
        truth.setdefault(key, []).append(rec)
    out = {}
    for (rid, kind), recs in sorted(truth.items()):
        if rid not in regions:
            continue
        recs = sorted(recs, key=lambda r: (r["access_seq"], r["trace_id"]))
        seen, total, dup, oob = set(), 0, 0, 0
        watermark, oob_max_end = -1, 0
        evidence = []
        for rec in recs:
            watermark = max(watermark, rec["access_seq"])
            if rec["access_seq"] in seen:
                dup += 1
                continue
            seen.add(rec["access_seq"])
            total += 1
            verdict = judge(rec)
            if verdict:
                oob += 1
                oob_max_end = max(oob_max_end, rec["base_offset"] + rec["access_size"])
                evidence.append({"access_seq": rec["access_seq"], "trace_id": rec["trace_id"],
                                 "code": verdict, "base_offset": rec["base_offset"],
                                 "access_size": rec["access_size"]})
        seqs = sorted(seen)
        gaps = [[a + 1, b - 1] for a, b in zip(seqs, seqs[1:]) if b > a + 1]
        out[f"{rid}|{kind}"] = {
            "total_count": total, "oob_count": oob, "oob_max_end": oob_max_end,
            "watermark": watermark, "dup_count": dup, "gaps": gaps,
            "evidence": evidence,
        }
    return out


def generate(out_dir, seed=20261125):
    rng = np.random.RandomState(seed)
    os.makedirs(out_dir, exist_ok=True)

    records, invalid = build_traces(rng)

    # Split into two shuffled batches; batch 2 also fills the R_TMP seq-11 gap.
    order = list(range(len(records)))
    rng.shuffle(order)
    half = len(order) // 2
    batch1 = [records[i] for i in order[:half]]
    batch2 = [records[i] for i in order[half:]]
    filler = _access("t_fill_11", 11, "R_TMP", 1100, 16, "read", 330)
    batch2.append(filler)
    records_all = records + [filler]

    def write_jsonl(path, recs, extra_lines=()):
        with open(path, "w", encoding="utf-8") as fh:
            for rec in recs:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
            for line in extra_lines:
                fh.write(line + "\n")

    with open(os.path.join(out_dir, "declarations.json"), "w", encoding="utf-8") as fh:
        json.dump({"regions": DECLS_V1}, fh, indent=2)
    with open(os.path.join(out_dir, "declarations_v2.json"), "w", encoding="utf-8") as fh:
        json.dump({"regions": DECLS_V2}, fh, indent=2)
    write_jsonl(os.path.join(out_dir, "traces_batch1.jsonl"), batch1, invalid)
    write_jsonl(os.path.join(out_dir, "traces_batch2.jsonl"), batch2)

    canonical = sorted(records_all, key=lambda r: r["trace_id"])
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True).encode()).hexdigest()
    truth = {
        "seed": seed,
        "sync_marker": digest,
        "trace_count": len(records_all),
        "expected_v1": reference_truth(DECLS_V1, records_all),
        "expected_v2": reference_truth(
            [d for d in DECLS_V1 if d["region_id"] not in
             {x["region_id"] for x in DECLS_V2}] + DECLS_V2, records_all),
    }
    with open(os.path.join(out_dir, "truth.json"), "w", encoding="utf-8") as fh:
        json.dump(truth, fh, indent=2, sort_keys=True)
    return truth


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", type=int, default=20261125)
    args = parser.parse_args()
    truth = generate(args.out, seed=args.seed)
    print(f"samples written to {args.out} "
          f"(seed={args.seed}, traces={truth['trace_count']})")


if __name__ == "__main__":
    main()
