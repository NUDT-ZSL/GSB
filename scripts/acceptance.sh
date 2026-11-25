#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUN_DIR="${HSM_ACCEPTANCE_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/hsm-acceptance-XXXXXX")}"
mkdir -p "$RUN_DIR"
DB="$RUN_DIR/hsm.sqlite"

echo "[1/7] deterministic seed -> $RUN_DIR/samples"
python3 -m health_machine seed --out "$RUN_DIR/samples" >"$RUN_DIR/seed.json"

echo "[2/7] load and validate"
python3 -m health_machine load --samples "$RUN_DIR/samples" --db "$DB" | tee "$RUN_DIR/load.json"

echo "[3/7] merge v1/v2 thresholds"
python3 -m health_machine merge \
  --samples "$RUN_DIR/samples" \
  --db "$DB" \
  --output "$RUN_DIR/unified_thresholds.json" | tee "$RUN_DIR/merge.json"

echo "[4/7] probe all windows"
python3 -m health_machine probe --db "$DB" >"$RUN_DIR/probe.json"
python3 -m health_machine verify --db "$DB" | tee "$RUN_DIR/verify.json"
python3 -m health_machine status --db "$DB" >"$RUN_DIR/status.json"

python3 - <<'PY' "$RUN_DIR/status.json"
import json
import sys

status = json.load(open(sys.argv[1], encoding="utf-8"))
for item in status["instances"]:
    print(
        f"{item['instance_id']}: state={item['state']} "
        f"score={item['latest_window_score_bp']} "
        f"failure_streak={item['failure_streak']} "
        f"recovery_streak={item['recovery_streak']} "
        f"threshold_generation={item['threshold_generation']} "
        f"clock_watermark={item['logical_clock_watermark_ms']}"
    )
PY

echo "[5/7] patch closure and idempotency"
PATCH_DB="$RUN_DIR/patch.sqlite"
cp "$DB" "$PATCH_DB"
python3 -m health_machine patch --db "$PATCH_DB" \
  --file "$RUN_DIR/samples/patches/01_adjust_flap.json" >"$RUN_DIR/patch1.json"
python3 -m health_machine patch --db "$PATCH_DB" \
  --file "$RUN_DIR/samples/patches/01_adjust_flap.json" >"$RUN_DIR/patch1-duplicate.json"
python3 -m health_machine verify --db "$PATCH_DB" >"$RUN_DIR/patch1-verify.json"

echo "[6/7] business error code sample"
BAD_REGISTRY="$RUN_DIR/bad-registry.json"
python3 - <<'PY' "$RUN_DIR/samples/registry.json" "$BAD_REGISTRY"
import json
import sys

doc = json.load(open(sys.argv[1], encoding="utf-8"))
doc["services"][0]["instances"][0]["endpoint"] = "8.8.8.8:443"
json.dump(doc, open(sys.argv[2], "w", encoding="utf-8"))
PY
set +e
python3 -m health_machine load --registry "$BAD_REGISTRY" \
  --observations "$RUN_DIR/samples/observations.json" --db "$RUN_DIR/error.sqlite" \
  >"$RUN_DIR/error.json"
ERROR_CODE=$?
set -e
test "$ERROR_CODE" -eq 2
grep -q "HSM-1003" "$RUN_DIR/error.json"
echo "business error code HSM-1003 and location verified"

echo "[7/7] automated tests including SIGKILL and multiprocess concurrency"
python3 -m pytest -q

echo "ACCEPTANCE_OK run_dir=$RUN_DIR"
