# Fixed-point Shuffle Hash Join Demo

All values below were produced from local deterministic JSON files. No network or account access is used.

## Experiments

| Experiment | Partitions | Scale | Rows | Collisions | Sum lattice | Sum bound | Mean lattice | Mean bound | Dominant |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| exp1_single_partition | 1 | 100 | 13 | 2 | 19575 | 13 | 1505 | 14 | shuffle |
| exp2_multi_partition | 4 | 100 | 13 | 2 | 19575 | 13 | 1505 | 14 | shuffle |
| exp3_scale_perturbation | 4 | 10000 | 10 | 0 | 1542500 | 10 | 154250 | 11 | shuffle |

## Partition Profiles

### exp1_single_partition

| Partition | Left | Right | Capacity | Load | Probes | Overflow probes | Matches |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 12 | 14 | 32 | 0.3125 | 21 | 0 | 13 |

Segments:

- `shuffle`: bound `26` lattice units, affected rows `24/26` (92.307692%).
- `bucket_probe`: bound `3` lattice units, affected rows `3/14` (21.428571%).
- `aggregation`: bound `13` lattice units, affected rows `13/13` (100.0%).

Row attribution counts: quantization `5`, aggregation `7`, both `1`.

### exp2_multi_partition

| Partition | Left | Right | Capacity | Load | Probes | Overflow probes | Matches |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 2 | 2 | 4 | 0.5 | 4 | 0 | 4 |
| 1 | 2 | 2 | 4 | 0.25 | 4 | 0 | 1 |
| 2 | 7 | 8 | 16 | 0.3125 | 11 | 0 | 6 |
| 3 | 1 | 2 | 4 | 0.5 | 3 | 0 | 2 |

Segments:

- `shuffle`: bound `26` lattice units, affected rows `24/26` (92.307692%).
- `bucket_probe`: bound `3` lattice units, affected rows `3/14` (21.428571%).
- `aggregation`: bound `13` lattice units, affected rows `13/13` (100.0%).

Row attribution counts: quantization `5`, aggregation `7`, both `1`.

### exp3_scale_perturbation

| Partition | Left | Right | Capacity | Load | Probes | Overflow probes | Matches |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 3 | 4 | 8 | 0.375 | 7 | 0 | 3 |
| 1 | 2 | 1 | 4 | 0.0 | 1 | 0 | 0 |
| 2 | 5 | 5 | 8 | 0.375 | 8 | 0 | 5 |
| 3 | 2 | 4 | 8 | 0.375 | 4 | 0 | 2 |

Segments:

- `shuffle`: bound `20` lattice units, affected rows `24/26` (92.307692%).
- `bucket_probe`: bound `0` lattice units, affected rows `0/10` (0.0%).
- `aggregation`: bound `10` lattice units, affected rows `10/10` (100.0%).

Row attribution counts: quantization `5`, aggregation `4`, both `1`.

## Injected Failures

| Injection | Code | Table | Row | Column |
| --- | --- | --- | ---: | --- |
| nullmap | E_NULLMAP | measurements | 12 | sensor_key |
| scalef | E_SCALEF |  |  |  |
| part | E_PART |  |  |  |

## Verification

Independent verification status: `OK`. Checked reports: `3`; injection checks: `3`.

The verifier recomputed partition digests, output digests, fixed aggregates, attribution, error bounds, and dominant segment selection.
