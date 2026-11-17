# Design and Verification Notes

## Execution State Machine

1. Parse `S=10^e` and partition count `P`; validate before touching business data.
2. Load and validate both tables atomically. Any table error prevents query execution.
3. Validate the query, key compatibility, predicates, projections, and aggregate source.
4. Quantize float join keys and partition all rows with the stable hash rule.
5. For each partition, apply fixed-point predicates, build an open-addressing multi-map on the build side, and probe the other side.
6. Sort matched pairs globally by `(join_key_fixed_lattice, left_row_index, right_row_index)`.
7. Produce dual-valued float projections, global attribution, partition aggregates, and the global aggregate.
8. Recompute the three error segments and apply the declared dominant-segment threshold.
9. Write the report and JSONL events. The verifier reloads source data and recomputes the result independently.

## Quantization Contract

For each finite IEEE-754 float `x`, let `x = n/d` be the exact integer ratio returned by `float.as_integer_ratio()`. Quantization is:

```text
q = (S * n) // d
```

Python integer division by a positive denominator is floor division. Therefore:

```text
q/S <= x < (q+1)/S
-1/S < q/S - x <= 0
```

The report uses a conservative integer error bound of one lattice unit. Comparisons are performed on `q`, not on the reference float. A predicate decision difference between fixed comparison and reference comparison is counted as a boundary mismatch.

## Aggregate Contract

For non-null float aggregate inputs `x_1 ... x_n`:

```text
Q = sum(q_i)
sum_hat = Q/S
sum_error_lattice < n
```

Mean uses truncation toward zero:

```text
m = trunc_toward_zero(Q/n)
mean_hat = m/S
mean_error_lattice < n + 1
```

The extra unit accounts for truncation of the fixed sum by the count. Null aggregate inputs do not change `Q` or `n`, but do increase `count_null`.

## Stable Hash and Partition Digest

The domain is `i64` for integer-to-integer keys and `f64q` when either side is a float. A cross-type key normalizes the integer side to `value * S`.

```text
payload = ASCII("fixed-join-v1|" + domain + "|" + decimal(lattice_value))
h64 = big_endian_uint64(SHA-256(payload)[0:8])
partition = h64 mod P
```

Rows in a partition are ordered by the SHA-256 of canonical row JSON before computing the partition digest. Original row order therefore does not affect the digest. The combined partition digest also includes table names and the partition index.

## Bucket Layout

For `B` non-null build rows in a partition:

```text
capacity = next_power_of_two(max(4, 2B))
load_factor = B / capacity
```

The linear-probe home bucket uses the lower bits of the same stable 64-bit hash. A probe limit of eight prevents unbounded primary-table scans. If no insertion slot is found, the key is appended to the overflow region. Lookups count both primary probes and overflow accesses.

## Row Attribution Invariant

Attribution is evaluated after the final global sort. For each output row:

```text
quantization_bound = left_float_key_bound + right_float_key_bound + non_null_float_projection_count
aggregation_prefix_bound = cumulative_non_null_float_aggregate_inputs_through_this_row
```

Classification:

```text
aggregation_prefix_bound == 0               -> quantization
quantization_bound == 0                     -> aggregation
quantization_bound > aggregation_prefix     -> quantization
aggregation_prefix > quantization_bound     -> aggregation
otherwise                                   -> both
```

None of these inputs depend on the partition assignment or within-partition execution order. A conflicting duplicate row attribution raises `E_ATTR`; the verifier uses `E_ATTR` for the first cross-report attribution mismatch.

## Error Segments

| Segment | Bound in integer lattice units | Affected rows |
| --- | --- | --- |
| Shuffle | output rows x float key sides | source rows with non-null float join keys |
| Bucket probe | reference collisions + predicate boundary mismatches | collision rows + predicate-affected rows |
| Aggregation | non-null float aggregate inputs | non-null aggregate output rows |

The dominant segment is the largest bound only when its share is at least 50% of the three-segment total. Otherwise the report says `mixed`. This rule is recorded in `error_propagation.threshold_rule` and independently checked.

## Error Codes

| Code | Meaning |
| --- | --- |
| `E_TABLE` | Table schema, row, range, or type validation failure |
| `E_NULLMAP` | Null bitmap length/content mismatch; carries parent `E_TABLE` |
| `E_SCALEF` | Scale factor is not a positive supported power of ten |
| `E_PART` | Partition count is not a positive integer or exceeds 64 |
| `E_QUERY` | Invalid query structure, column reference, or join type |
| `E_ATTR` | Inconsistent row attribution |
| `E_VERIFY` | Independent report verification failure |
| `E_IO` | Local file or output-directory failure |

## Actual Validation Record

Executed in the delivered workspace:

```text
python3 -m unittest discover -s tests -v
11 tests passed

python3 scripts/run_demo.py
3 successful reports verified
3 injected business codes verified

E_NULLMAP: measurements, row 12, column sensor_key
E_SCALEF: scale factor 25 rejected
E_PART: partition count 65 rejected
```

The generated reports are under `experiments/`; `experiments/verification.json` is the verifier output.
