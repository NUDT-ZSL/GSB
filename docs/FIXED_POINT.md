# Fixed-Point Error Model

## Notation and truncation

All fixed-point values are non-negative integers. For grid `S > 0` and rational
value `n/d`:

```text
q = floor(S * n / d)
r = (S * n) mod d
e_fixed = S*n/d - q = r/d
0 <= e_fixed < 1
```

The normalized absolute error is `< 1/S`. No `float` participates in any decision.

## Mismatch ratio and threshold comparison

```text
q_r = floor(S * mismatch_count / replica_count)
q_t = floor(S * threshold_num / threshold_den)
G   = q_r - q_t
```

For true fixed difference `D`:

```text
D - G = e_r - e_t,  e_r,e_t in [0,1)
=> -1 < D - G < 1
```

Strict threshold rule is `D > 0` (`ratio > threshold`). Therefore:

- `G >= 1` proves `above`;
- `G <= -1` proves `not_above`;
- `G == 0` is `indeterminate` and is never silently collapsed to a certain answer.
  The sole exact special case is `mismatch_count == 0`, where the strict relation
  to a positive threshold is known directly and is reported as `not_above`.

`exact_above` is retained in diagnostics for auditing, but the quantized certainty
state is what may flip when `S` changes.

## Transfer and workload

Transfer includes rational overhead `o_num/o_den`:

```text
transfer_exact = block_bytes * (o_den + o_num) / o_den
q_transfer = floor(S * transfer_exact)
```

Workload is:

```text
q_work = floor(S * block_bytes * work_num / work_den)
```

Both have a single-term absolute error `< 1` fixed unit (or exactly zero when the
division has no remainder).

## Candidate source cost

For source `s` with priority `p_s`, maximum priority `P`, repair load `l_s`, and
maximum load `L`:

```text
cost_fixed =
    w_transfer * q_transfer
  + w_work     * q_work
  + w_priority * floor(S * (P - p_s) / P)
  + w_load     * floor(S * l_s / L)
```

Each non-divisible floor term contributes `< w_i` weighted fixed units. Candidate
bound:

```text
B_c = sum(w_i for each term whose division has a non-zero remainder)
```

The provided implementation conservatively emits this full candidate bound even
when equal source-side terms would cancel in a pair comparison.

For two candidates `a,b`, the pair bound is `B_a + B_b`. If:

```text
abs(cost_a - cost_b) <= B_a + B_b
```

they enter the tie channel. The tie rule is:

```text
priority descending, then node_id ascending
```

The use of `<=` is deliberately conservative relative to the task wording's
“strictly less than” threshold; if a difference is exactly the bound, it cannot be
proven that cost ordering is stable, so it remains a tie.

## Synthesized upper bounds

- Single fixed estimate: `< 1` fixed unit.
- Ratio comparison: `< 1` fixed unit for the difference.
- Candidate cost: sum of weighted per-term bounds.
- Pair ordering: candidate bound A + candidate bound B.
- Task total: the task stores transfer/work errors, candidate cost bound, and
  aggregate summary sums the per-task bounds conservatively.

## Flip reporting

For each grid-induced flip the diagnosis records:

- before/after scale;
- block and target node;
- before/after decision;
- weighted cost term rows for the old/new sources;
- fixed cost gap and derived bound before and after;
- `error_bound_share_ppm = ceil(B * 1_000_000 / max(1, abs(gap)))`.

A share greater than `1,000,000` means the bound exceeds the observed gap. A
perfectly tied gap uses denominator one and is therefore finite and integer.

Exact truth digest and exact mismatch set are order-independent and grid-invariant.
Only ratio-alert certainty and error-aware cost ordering are grid-dependent.
