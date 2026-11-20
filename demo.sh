#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
DEMO_ROOT="$ROOT/results/demo"

"$PYTHON" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or newer is required")
PY

rm -rf "$DEMO_ROOT"
mkdir -p "$DEMO_ROOT"

run_flow() {
    local label="$1"
    local dir="$DEMO_ROOT/$label"
    local db="$dir/state.sqlite"
    rm -rf "$dir"
    mkdir -p "$dir"

    "$PYTHON" -m slowplan load \
        --db "$db" \
        --sample-db samples/sample_db.sqlite \
        --stats samples/stats_snapshot.json \
        > "$dir/01-load.json"

    "$PYTHON" -m slowplan ingest \
        --db "$db" \
        --samples samples/slow_queries.json \
        > "$dir/02-ingest-initial.json"

    "$PYTHON" -m slowplan profile \
        --db "$db" \
        > "$dir/03-profile-v1.json"

    "$PYTHON" -m slowplan apply \
        --db "$db" \
        --patch samples/stats_patch_v2.json \
        > "$dir/04-apply-v2.json"

    "$PYTHON" -m slowplan ingest \
        --db "$db" \
        --samples samples/slow_queries_after_refresh.json \
        > "$dir/05-ingest-after-refresh.json"

    set +e
    SLOWPLAN_FAULT_POINT=profile_after_journal \
        "$PYTHON" -m slowplan profile --db "$db" \
        > "$dir/06-profile-crash.json" 2> "$dir/06-profile-crash.stderr"
    local crash_code=$?
    set -e
    if [[ "$crash_code" -ne 91 ]]; then
        echo "expected injected hard exit 91, got $crash_code" >&2
        exit 1
    fi

    "$PYTHON" -m slowplan resume --db "$db" > "$dir/07-resume.json"
    "$PYTHON" -m slowplan status --db "$db" > "$dir/status.json"
    "$PYTHON" -m slowplan verify --db "$db" > "$dir/verify.json"
}

run_flow run1
run_flow run2

for artifact in \
    02-ingest-initial.json \
    03-profile-v1.json \
    04-apply-v2.json \
    05-ingest-after-refresh.json \
    07-resume.json \
    status.json \
    verify.json; do
    cmp "$DEMO_ROOT/run1/$artifact" "$DEMO_ROOT/run2/$artifact"
done

"$PYTHON" - "$DEMO_ROOT" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
status = json.loads((root / "run1" / "status.json").read_text(encoding="utf-8"))
verify = json.loads((root / "run1" / "verify.json").read_text(encoding="utf-8"))
print("demo_ok=true")
print(f"active_stats_version={status['active_stats_version']}")
print(f"committed_stats_version={status['committed_stats_version']}")
print(f"sample_watermark={status['sample_watermark']}")
print(f"logical_clock_ms={status['logical_clock_ms']}")
print(f"invalidations={status['invalidations']['total']}")
print(f"pending_invalidations={status['invalidations']['pending']}")
print(f"verify={str(verify['ok']).lower()}")
print(f"state_digest={verify['state_digest']}")
print(f"status_digest={verify['status_digest']}")
for template in status["templates"]:
    print(
        "template="
        + template["template_id"]
        + " status="
        + template["profile_status"]
        + " snapshot="
        + str(template["profile_stats_version"])
        + " path="
        + str(template["selected_path"])
    )
PY

echo "two independent runs are byte-identical for status and verify"
