# SQL Plan Attribution Experiment Report

This report is deterministic and generated from local SQLite samples only.

## Versions

- Data version: `dv_536d45df54fd3314771d429a`
- Statistics v1: `sv_3671274f0cac0df85372a0b4`
- Statistics v2: `sv_78b127fa0177eca06e1746da`
- Plan id: `lp_edd8e87108adde3194c2fd89`

## Normalized Plan

```sql
select o.id, o.amount, c.region from orders as o join customers as c on o.customer_id = c.id where o.amount >= 100 AND o.status = 'paid'
```

```text
op_003 FILTER predicate=o.amount >= 100 AND o.status = 'paid'
  op_002 JOIN predicate=o.customer_id = c.id
    op_000 SCAN table=orders alias=o
    op_001 SCAN table=customers alias=c
```

## Operator Attribution (current statistics v2)

| Operator | Type | Estimated | Actual | Est/Actual | Deviation | Heat |
|---|---|---:|---:|---:|---:|---|
| SCAN orders AS o | scan | 24 | 24 | 1.0 | 0 | low |
| SCAN customers AS c | scan | 8 | 8 | 1.0 | 0 | low |
| JOIN o.customer_id = c.id | join | 24 | 24 | 1.0 | 0 | low |
| FILTER o.amount >= 100 AND o.status = 'paid' | filter | 11 | 14 | 0.785714 | 2143 | critical |

Primary attribution operator: `op_003`.

## Statistics Version Comparison

Changed operators: **2**

| Operator | Type | Old estimate | New estimate | Old score | New score | Score delta |
|---|---|---:|---:|---:|---:|---:|
| JOIN o.customer_id = c.id | join | 14 | 24 | 4167 | 0 | -4167 |
| FILTER o.amount >= 100 AND o.status = 'paid' | filter | 8 | 11 | 4286 | 2143 | -2143 |

## Snapshot Restoration

Snapshot `baseline` restored with whole-state replacement: `True`.

Replayed plans after opening a new engine: **3**

## Anomaly Injection

| Anomaly | Expected code | Observed code | First event id | Repeat event id | Stable |
|---|---|---|---|---|---|
| skew | E_SKEW | E_SKEW | `87856a25a9fffa6f07f62f30` | `87856a25a9fffa6f07f62f30` | True |
| stats | E_STATS | E_STATS | `7c30196944f11d3157b65d19` | `7c30196944f11d3157b65d19` | True |
| replay | E_REPLAY | E_REPLAY | `d666f49915fa3f16352fb471` | `d666f49915fa3f16352fb471` | True |

## Validation Checks

| Check | Result |
|---|---|
| same sample re-ingest yields same data version | PASS |
| same sample re-ingest yields same content digest | PASS |
| whitespace/case/parenthesis variant yields same plan id | PASS |
| variant yields same normalized SQL | PASS |
| stable topological serialization | PASS |
| subquery and aggregate rejected with E_PARSE | PASS |
| same statistics version yields identical estimates | PASS |
| statistics version is emitted by estimator | PASS |
| estimator and actual collection use one data version | PASS |
| v1 heatmap contains all operators | PASS |
| zero actual rows use explicit ratio sentinel | PASS |
| statistics change creates a new version | PASS |
| old statistics version remains available | PASS |
| v2 heatmap is whole-set replacement | PASS |
| v2 heatmap contains no stale v1 operator rows | PASS |
| statistics comparison identifies changed operators | PASS |
| statistics comparison marks version difference | PASS |
| snapshot stores plan history | PASS |
| snapshot restore replaces whole session | PASS |
| restored history replays to identical deviation scores | PASS |
| restored heatmap is present | PASS |
| skew anomaly reports E_SKEW | PASS |
| skew anomaly is reproducible | PASS |
| stats anomaly reports E_STATS | PASS |
| stats anomaly is reproducible | PASS |
| replay anomaly reports E_REPLAY | PASS |
| replay anomaly is reproducible | PASS |
| structured anomaly logs use stable ordering | PASS |
| repeated injection deduplicates stable log records | PASS |
| anomaly injection leaves data version unchanged | PASS |

Overall: **PASS**
