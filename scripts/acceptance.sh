#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${1:-data}"
CLOCK_BASE="${CLOCK_BASE:-1000}"

echo "==> Resetting generated SQLite/object/lock state in $DATA_DIR"
python3 - "$DATA_DIR" <<'PY'
import shutil
import sys
from pathlib import Path

data_dir = Path(sys.argv[1])
for suffix in ("", "-wal", "-shm"):
    path = data_dir / f"state.sqlite3{suffix}"
    path.unlink(missing_ok=True)
shutil.rmtree(data_dir / "objects", ignore_errors=True)
shutil.rmtree(data_dir / "locks", ignore_errors=True)
PY

echo "==> Generating fixed-seed samples"
python3 -m dataplane seed --data-dir "$DATA_DIR"

echo "==> Loading and validating"
python3 -m dataplane load --data-dir "$DATA_DIR" --clock-ms "$CLOCK_BASE"

echo "==> Generating baseline caches"
python3 -m dataplane generate \
  --data-dir "$DATA_DIR" --clock-ms 2000 \
  --budget-ms 100000 --concurrency 4

echo "==> Applying conflict patches and invalidation closure"
python3 -m dataplane patch \
  --data-dir "$DATA_DIR" --clock-ms 3000 \
  --budget-ms 100000 --concurrency 4

echo "==> Verifying hashes, ledgers and deterministic recomputation"
python3 -m dataplane verify \
  --data-dir "$DATA_DIR" --clock-ms 3100 --strict

echo "==> Installing incremental config generation"
python3 -m dataplane reconfigure \
  --data-dir "$DATA_DIR" \
  --package "$DATA_DIR/config_v2_incremental" \
  --clock-ms 4000
python3 -m dataplane generate \
  --data-dir "$DATA_DIR" --clock-ms 4100 \
  --budget-ms 100000 --concurrency 4
python3 -m dataplane patch \
  --data-dir "$DATA_DIR" --clock-ms 4200 \
  --budget-ms 100000 --concurrency 4
python3 -m dataplane verify \
  --data-dir "$DATA_DIR" --clock-ms 4300 --strict

CRASH_DIR="$(mktemp -d /tmp/dp-acceptance-crash-XXXX)"
echo "==> Crash/recovery demonstration in $CRASH_DIR"
python3 -m dataplane seed --data-dir "$CRASH_DIR" >/dev/null
python3 -m dataplane load --data-dir "$CRASH_DIR" --clock-ms 1000 >/dev/null
set +e
DP_CRASH_AT='materialized_committed:case-001:0' \
  python3 -m dataplane generate \
    --data-dir "$CRASH_DIR" --clock-ms 2000 --concurrency 4 \
    --budget-ms 100000 >/dev/null 2>&1
crash_code=$?
set -e
if [[ "$crash_code" -ne 97 ]]; then
  echo "expected deterministic crash exit 97, got $crash_code" >&2
  exit 1
fi
python3 -m dataplane resume \
  --data-dir "$CRASH_DIR" --clock-ms 2100 \
  --budget-ms 100000 --concurrency 4 >/dev/null
python3 -m dataplane verify \
  --data-dir "$CRASH_DIR" --clock-ms 2200 --strict >/dev/null

echo "ACCEPTANCE_OK data_dir=$DATA_DIR crash_dir=$CRASH_DIR"
