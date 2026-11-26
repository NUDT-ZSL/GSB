#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "[1/5] Generating deterministic samples"
python3 generate_samples.py --out samples

echo "[2/5] Running automated tests"
python3 -m unittest tests.test_engine

echo "[3/5] Running full pipeline and incremental scenarios"
python3 run.py --mode all --samples samples --output artifacts --workers 2

CLEAN_DIR="$(mktemp -d /tmp/emotion-diag-clean-XXXXXX)"
RECOVERY_DIR="$(mktemp -d /tmp/emotion-diag-recovery-XXXXXX)"

echo "[4/5] Checking deterministic clean run and SIGKILL recovery"
python3 run.py --mode full --samples samples --output "$CLEAN_DIR" --no-process-pool

set +e
python3 run.py --mode full --samples samples --output "$RECOVERY_DIR" \
  --no-process-pool --crash-stage state_anomaly --crash-after-frame 300 \
  --hard-crash
CRASH_CODE=$?
set -e
if [[ "$CRASH_CODE" -ne 137 ]]; then
  echo "expected SIGKILL exit 137, got $CRASH_CODE" >&2
  exit 1
fi
test -f "$RECOVERY_DIR/state/work/state_anomaly.work.json"

python3 run.py --mode full --samples samples --output "$RECOVERY_DIR" --no-process-pool

python3 - "$CLEAN_DIR" "$RECOVERY_DIR" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

clean = Path(sys.argv[1])
recovered = Path(sys.argv[2])
names = [
    "frame_features.jsonl",
    "frame_feature_matrix.npy",
    "emotion_states.jsonl",
    "anomalies.json",
    "unified_ledger.jsonl",
    "migration_summary.json",
]
for name in names:
    left = hashlib.sha256((clean / name).read_bytes()).hexdigest()
    right = hashlib.sha256((recovered / name).read_bytes()).hexdigest()
    assert left == right, f"{name} differs after recovery"

report = json.loads((recovered / "diagnostic_report.json").read_text())
assert report["anomalies"]["anomaly_count"] == 3
assert report["migration"]["cross_version_equivalent"] is True
assert report["recovery"]["resumed_after_uncommitted_discard"] is True
assert report["stage_reuse"]["feature_extraction"]["reused_ranges"] == [[0, report["frame_count"]]]
assert [s["version"] for s in report["stages"]] == [1, 2, 3, 4]
print("recovery byte-comparison: OK")
PY

echo "[5/5] Acceptance verification passed"
echo "Clean output: $CLEAN_DIR"
echo "Recovery output: $RECOVERY_DIR"
