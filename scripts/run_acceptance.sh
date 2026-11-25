#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
CACHE_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$CACHE_DIR"
}
trap cleanup EXIT

"$PYTHON" scripts/generate_samples.py --out data
"$PYTHON" -m unittest discover -s tests -v

"$PYTHON" -m ragdiag run \
  --cache "$CACHE_DIR/cache" \
  --out-json reports/acceptance-baseline.json \
  --out-html reports/acceptance-baseline.html \
  > reports/acceptance-baseline.summary.json

"$PYTHON" -m ragdiag run \
  --cache "$CACHE_DIR/cache" \
  --out-json reports/acceptance-repeat.json \
  --out-html reports/acceptance-repeat.html \
  > reports/acceptance-repeat.summary.json

"$PYTHON" -m ragdiag invalidate \
  --repository data/tool_results.next.json \
  --baseline-repository data/tool_results.json \
  --requests data/requests.jsonl \
  --schedule data/schedule.json \
  --corpus data/evidence_corpus.jsonl \
  --cache "$CACHE_DIR/cache" \
  --out-json reports/acceptance-invalidation.json \
  --out-html reports/acceptance-invalidation.html \
  > reports/acceptance-invalidation.summary.json

"$PYTHON" - <<'PY'
import json
from pathlib import Path

root = Path("reports")
baseline = json.loads((root / "acceptance-baseline.json").read_text())
repeat = json.loads((root / "acceptance-repeat.json").read_text())
invalidation = json.loads((root / "acceptance-invalidation.json").read_text())
summary = {
    "baseline_business_codes": baseline["summary"]["business_codes"],
    "baseline_block_equivalence": baseline["block_equivalence"]["status"],
    "repeat_cache_hit_requests": repeat["summary"]["cache_hit_requests"],
    "invalidation_affected_requests": invalidation["invalidation"]["affected_requests"],
    "invalidation_affected_stages": invalidation["invalidation"]["affected_stages"],
    "invalidation_counts": invalidation["invalidation"]["counts"],
    "invalidation_coverage": invalidation["invalidation"]["coverage"],
    "invalidation_cache_hit_requests": invalidation["summary"]["cache_hit_requests"],
    "snapshot_id": invalidation["snapshot"]["snapshot_id"],
    "reports": {
        "baseline_html": "reports/acceptance-baseline.html",
        "repeat_html": "reports/acceptance-repeat.html",
        "invalidation_html": "reports/acceptance-invalidation.html",
    },
}
(root / "acceptance-summary.json").write_text(
    json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    encoding="utf-8",
)
print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
PY

echo "Acceptance reports were written under $ROOT/reports"

