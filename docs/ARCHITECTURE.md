# Architecture Notes

## 1. State model

The application uses a two-level model:

```text
generation
  validate snapshot
  segment snapshot
  tag snapshot
  summarize snapshot
```

A generation is identified by the tuple of input, configuration, dictionary,
and label-set checksums plus dictionary/label versions. A new generation is
created whenever that identity changes.

Each stage snapshot is a complete payload for the whole stage, never a patch.
The SQLite transaction writes the snapshot row and advances a global version
counter atomically.

## 2. Snapshot invariants

- `snapshot_version` is globally monotonic in one workspace.
- `payload_checksum = SHA256(canonical_json(payload).encode("utf-8"))`.
- Canonical JSON uses UTF-8, sorted keys, compact separators and `allow_nan=False`.
- `completed_doc_ids` must equal the generation's complete doc-id set.
- `recomputed_doc_ids == affected_doc_ids`.
- `recomputed_doc_ids ∩ reused_doc_ids = ∅`.
- `recomputed_doc_ids ∪ reused_doc_ids = all doc ids`.
- Stage order is fixed: validate → segment → tag → summarize.
- A stage can only be committed into the active generation.
- A lower dictionary or label-set version is rejected before writing.

The version counter and stage row live in the same `BEGIN IMMEDIATE`
transaction. A second writer gets SQLite `database is locked`, converted
immediately to `E_STAGE_BUSY`.

## 3. Recovery

The pipeline first loads committed snapshots from the active generation. It
does not reprocess a stage that exists. For the first missing stage it rebuilds
from that stage's input snapshot in memory.

No intermediate stage data is written to disk. Therefore a kill during a stage
leaves the previous stage as the last visible commit. On restart the partial
stage is rebuilt from its committed predecessor.

The recovery report separates historical provenance from this invocation:

```json
{
  "skipped_committed_stages": ["validate", "segment"],
  "executed_stages_this_run": ["tag", "summarize"],
  "invocation_recomputed": {
    "tag": ["..."],
    "summarize": ["..."]
  }
}
```

## 4. Invalidation

Invalidation is computed with read-only probes:

1. Rebuild validation in memory from the same input.
2. Run the new dictionary over every line and compare each segmented line with
   the previous generation's line.
3. Run the new label set over the probe segmentation and compare tag lines.
4. The affected suffix is the union of exact changed line sets.

`no_dict_hit_docs` are lines where both old and new segmentation only use
fallback edges. The implementation asserts that these docs never enter the
segment affected set and otherwise raises `E_INCONSISTENT`.

When committing the new generation, whole stage payloads are rebuilt:

- validation payload can be carried forward;
- segment/tag/summary line objects outside the affected set are copied from the
  base generation;
- affected line objects are computed from the new generation's resources;
- top-level dictionary/label references are always replaced with the new
  generation's versions and checksums.

This keeps the snapshot a complete replacement while limiting expensive
per-line recomputation.

## 5. Determinism

There are no timestamps in business payloads or final outputs. The only time
source in the application is the HTTP server log. Randomness is limited to
sample generation with a fixed seed. Canonical JSON and content hashes determine
file bytes.

Segmentation uses a DAG:

```text
score(path) = Σ log(freq(edge))
fallback edge: freq = 1, score = 0
```

Path comparison uses `(score desc, starts lexicographic asc, ends lexicographic
asc, words lexicographic asc)`. The score does not include a dictionary-total
normalization term, which preserves scores for lines that hit neither old nor
new dictionary entries.

Tag selection sorts candidate labels by descending score and then controlled
label index ascending.

## 6. Resource degradation

Candidate budget is a stage-level count of dictionary candidate edges. If the
budget is exceeded and policy is `degrade`, each start position keeps at most
`degrade_max_candidates_per_start` candidates. Dropped candidates are recorded
without changing the offset unit.

Tag memory is estimated from canonical candidate-score maps held during the
stage. On degradation, those maps are removed from intermediate/result lines
and `TAG_MEMORY_TRIM` is recorded. Final labels, word boundaries and codepoint
offsets are unchanged.

## 7. Testing strategy

The suite covers:

- exact repeatability of complete runs;
- path tie resolution with equal log scores;
- controlled-label tie resolution;
- offset coverage and `E_OFFSET_LEN`;
- recovery after each of the four stages;
- mid-stage hard termination with no partial commit;
- snapshot version monotonicity and full doc coverage;
- upgrade reuse, no-hit invariance and fresh-v2 byte equality;
- rollback and corruption business codes;
- fail-fast concurrent commit;
- resource hard limit and degradation;
- all required exception codes;
- `E_INCONSISTENT` categories.
