# samples

All audit inputs in this directory are produced by the deterministic generator:

```bash
python -m firewall_audit.samplegen --out samples --seed 20261125
```

`sample_manifest.json` is the baseline sample. `incremental/sample_manifest.json`
reuses the same cases and rule snapshots with mapping generation 4, which adds
the missing `v1-semantic-only` / `v2-semantic-only` pair and demonstrates an
affected-case closure of exactly one case.
