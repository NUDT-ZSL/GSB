#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

python3 -m unittest discover -s tests -v
python3 scripts/generate_sample.py --out examples/data

WORK="$(mktemp -d "${TMPDIR:-/tmp}/ckptmgr-validation.XXXXXX")"
echo "validation workspace: $WORK"

python3 -m ckptmgr save --root "$WORK" \
  --plan examples/data/plan.json \
  --strategy examples/data/strategy.json \
  --save-plan examples/data/checkpoint-manifest.json \
  --version 1 --mode full --clock-ms 1000 >"$WORK/save-v1.json"

python3 -m ckptmgr save --root "$WORK" \
  --plan examples/data/plan.json \
  --strategy examples/data/strategy.json \
  --save-plan examples/data/checkpoint-manifest.json \
  --version 2 --mode incremental --clock-ms 1100 >"$WORK/save-v2.json"

python3 -m ckptmgr save --root "$WORK" \
  --plan examples/data/plan.json \
  --strategy examples/data/strategy.json \
  --save-plan examples/data/checkpoint-manifest.json \
  --version 3 --mode incremental --clock-ms 1200 >"$WORK/save-v3.json"

python3 -m ckptmgr restore --root "$WORK" \
  --checkpoint demo --version 2 --clock-ms 1300 >"$WORK/restore-v2.json"

python3 -m ckptmgr verify --root "$WORK" >"$WORK/verify.json"
python3 -m ckptmgr status --root "$WORK" >"$WORK/status.json"

python3 - "$WORK" <<'PY'
import json
import sys
from pathlib import Path

work = Path(sys.argv[1])
restore = json.loads((work / "restore-v2.json").read_text())
verify = json.loads((work / "verify.json").read_text())
status = json.loads((work / "status.json").read_text())
print(
    json.dumps(
        {
            "restore_version": restore["version"],
            "stitched_equals_independent": restore["stitched_equals_independent"],
            "parameter_digest": restore["parameter_digest"],
            "verified_versions": [item["version"] for item in verify["checkpoints"]],
            "pointer": status["pointers"][0],
            "objects": status["objects"],
            "orphan_objects": status["orphan_objects"],
            "workspace": str(work),
        },
        indent=2,
    )
)
PY
