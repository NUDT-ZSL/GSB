#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p artifacts

"$PYTHON" -m pytest -q | tee artifacts/pytest.txt
"$PYTHON" -m mtstream generate --out data/sample_bundle.json \
  > artifacts/generate_report.json
"$PYTHON" -m mtstream selfcheck --block-size 4 \
  --out artifacts/selfcheck.json > artifacts/selfcheck_stdout.json
"$PYTHON" -m mtstream decode --block-size 4 \
  > artifacts/decode_report.json
"$PYTHON" -m mtstream invalidate --mode unaligned \
  > artifacts/invalidation_unaligned.json
"$PYTHON" -m mtstream invalidate --mode aligned --position 5 \
  > artifacts/invalidation_aligned.json
"$PYTHON" -m mtstream invalidate --mode align_version \
  > artifacts/invalidation_align_version.json
"$PYTHON" -m mtstream invalidate --mode block_size --new-block-size 3 \
  > artifacts/invalidation_block_size.json
"$PYTHON" -m mtstream pool-check > artifacts/pool_check.json

"$PYTHON" - <<'PY'
import json
from pathlib import Path

artifacts = Path("artifacts")
decode = json.loads((artifacts / "decode_report.json").read_text())
selfcheck = json.loads((artifacts / "selfcheck.json").read_text())
invalidation = {}
for name in (
    "unaligned",
    "aligned",
    "align_version",
    "block_size",
):
    payload = json.loads(
        (artifacts / f"invalidation_{name}.json").read_text()
    )
    report = payload["report"]
    invalidation[name] = {
        "first_affected_step": report["first_affected_step"],
        "affected_steps": report["affected_steps"],
        "reused_steps": report["reused_steps"],
        "affected_cache_entries": len(report["affected_cache_entries"]),
        "reused_cache_entries": len(report["reused_cache_entries"]),
        "set_check": report["set_check"],
    }
summary = {
    "python": __import__("sys").version,
    "numpy": __import__("numpy").__version__,
    "pytest": (artifacts / "pytest.txt").read_text().strip(),
    "decode_consistency": decode["consistency"],
    "block_size": {
        "requested": decode["plan"]["block_size_requested"],
        "effective": decode["plan"]["block_size_effective"],
        "degraded": decode["plan"]["degraded"],
    },
    "timings_ns": decode["timings"],
    "first_block_timing": decode["blocks"][0]["timing"],
    "whole_timing": decode["whole"]["timing"],
    "semantic_hash": decode["whole"]["semantic_hash"],
    "intermediate_digest": decode["whole"]["intermediate_digest"],
    "prefix_cache_entries": decode["prefix_cache_entries"],
    "selfcheck_ok": selfcheck["ok"],
    "selfcheck_checks": {
        check["name"]: check["ok"] for check in selfcheck["checks"]
    },
    "invalidation": invalidation,
    "pool_check": json.loads(
        (artifacts / "pool_check.json").read_text()
    ),
}
(artifacts / "verification_summary.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
PY

