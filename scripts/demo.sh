#!/usr/bin/env bash
set -u

cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
DEMO="artifacts/demo"
mkdir -p "$DEMO"
PIPELINE_STATE="$(mktemp -d "$DEMO/pipeline-state.XXXXXX")"
REPLAY_STATE="$(mktemp -d "$DEMO/replay-state.XXXXXX")"
STALE_STATE="$(mktemp -d "$DEMO/stale-state.XXXXXX")"
RECOVERY_STATE="$(mktemp -d "$DEMO/recovery-state.XXXXXX")"

run_cli() {
  "$PY" -m deprule.cli "$@"
}

code_of() {
  "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("code") or d.get("business_code") or "MISSING")' "$1"
}

echo "== [1/8] deterministic seed =="
run_cli seed --out data > "$DEMO/01-seed.json"
echo "seed: $(code_of "$DEMO/01-seed.json")"

echo "== [2/8] v1/v2/v3 semantic and parse equivalence =="
run_cli verify-equivalence \
  --tasks data/tasks.jsonl \
  --v1 data/rules-v1.json \
  --v2 data/rules-v2/v2_manifest.json \
  --v3 data/rules-v3.json \
  --state-dir "$DEMO/equiv-state" \
  --out "$DEMO/02-equivalence-report.json" \
  > "$DEMO/02-equivalence-cli.json"
echo "equivalence: $(code_of "$DEMO/02-equivalence-cli.json")"

run_cli verify-equivalence \
  --tasks data/tasks.jsonl \
  --v1 data/rules-v1.json \
  --v2 data/rules-v2/v2_manifest.json \
  --v3 data/rules-v3-drift.json \
  --state-dir "$DEMO/drift-state" \
  > "$DEMO/02-semantic-drift.json" || true
echo "semantic-drift-sample: $(code_of "$DEMO/02-semantic-drift.json")"

echo "== [3/8] base parse and trace replay =="
run_cli parse \
  --tasks data/tasks.jsonl \
  --rules data/rules-v1.json \
  --state-dir "$PIPELINE_STATE" \
  --out-dir "$DEMO/base" \
  --fresh > "$DEMO/03-parse-base.json"
echo "parse-base: $(code_of "$DEMO/03-parse-base.json")"

run_cli replay \
  --export "$DEMO/base/parse-result.json" \
  --tasks data/tasks.jsonl \
  --rules data/rules-v1.json \
  --state-dir "$REPLAY_STATE" \
  > "$DEMO/03-replay-ok.json"
echo "replay-same-rule: $(code_of "$DEMO/03-replay-ok.json")"

echo "== [4/8] targeted invalidation after root-policy revision =="
run_cli parse \
  --tasks data/tasks.jsonl \
  --rules data/rules-v2-rev2/v2_manifest.json \
  --old-rules data/rules-v1.json \
  --previous-ledger "$DEMO/base/invalidation-ledger.json" \
  --state-dir "$PIPELINE_STATE" \
  --out-dir "$DEMO/incremental" \
  > "$DEMO/04-incremental.json"
echo "incremental: $(code_of "$DEMO/04-incremental.json")"

run_cli parse \
  --tasks data/tasks.jsonl \
  --rules data/rules-v2-rev2/v2_manifest.json \
  --state-dir "$DEMO/fresh-rev2-state" \
  --out-dir "$DEMO/fresh-rev2" \
  --fresh > "$DEMO/04-fresh-rev2.json"
cmp -s "$DEMO/incremental/parse-result.json" "$DEMO/fresh-rev2/parse-result.json"
echo "incremental-vs-fresh-byte-identical: OK"

echo "== [5/8] stale export under changed cache-key versions =="
run_cli replay \
  --export "$DEMO/base/parse-result.json" \
  --tasks data/tasks.jsonl \
  --rules data/rules-v2-rev2/v2_manifest.json \
  --state-dir "$STALE_STATE" \
  > "$DEMO/05-replay-stale.json" || true
echo "replay-changed-rule: $(code_of "$DEMO/05-replay-stale.json")"

echo "== [6/8] forced kill in stage 3 and checkpoint recovery =="
set +e
run_cli parse \
  --tasks data/tasks.jsonl \
  --rules data/rules-v1.json \
  --state-dir "$RECOVERY_STATE" \
  --out-dir "$DEMO/crashed" \
  --fresh \
  --crash-mid-stage 3 \
  --no-commit-ledger > "$DEMO/06-crash-stdout.json" 2> "$DEMO/06-crash.stderr"
crash_code=$?
set -e
echo "forced-crash-exit: $crash_code"

run_cli parse \
  --tasks data/tasks.jsonl \
  --rules data/rules-v1.json \
  --state-dir "$RECOVERY_STATE" \
  --out-dir "$DEMO/recovered" \
  --no-commit-ledger > "$DEMO/06-recovery.json"
echo "recovery: $(code_of "$DEMO/06-recovery.json")"
cmp -s "$DEMO/recovered/parse-result.json" "$DEMO/base/parse-result.json"
echo "recovery-vs-clean-byte-identical: OK"

echo "== [7/8] format errors, migration and rollback =="
for name in unknown-version bad-pipe-count zero-window bad-tag-array; do
  run_cli read-rule --rules "data/malformed/$name.json" > "$DEMO/07-$name.json" || true
  echo "$name: $(code_of "$DEMO/07-$name.json")"
done

run_cli migrate --source data/rules-v1.json --to v3 --out "$DEMO/migrated-v3.json" \
  > "$DEMO/07-migrate-v3.json"
echo "migrate-v1-to-v3: $(code_of "$DEMO/07-migrate-v3.json")"

VERSION_STATE="$(mktemp -d "$DEMO/version-state.XXXXXX")"
run_cli commit-rules --state-dir "$VERSION_STATE" --rules data/rules-v1.json > "$DEMO/07-commit-v1.json"
run_cli commit-rules --state-dir "$VERSION_STATE" --rules data/rules-v2-rev2/v2_manifest.json > "$DEMO/07-commit-v2.json"
run_cli commit-rules --state-dir "$VERSION_STATE" --rules data/rules-v1.json > "$DEMO/07-rollback.json" || true
echo "rollback-old-revision: $(code_of "$DEMO/07-rollback.json")"

echo "== [8/8] non-blocking concurrent commit =="
BUSY_STATE="$(mktemp -d "$DEMO/busy-state.XXXXXX")"
run_cli demo-concurrency --state-dir "$BUSY_STATE" --rules data/rules-v1.json > "$DEMO/08-concurrency.json"
echo "concurrent-commit: $(code_of "$DEMO/08-concurrency.json")"

"$PY" - <<'PY'
import json
from pathlib import Path

demo = Path("artifacts/demo")
inc = json.loads((demo / "04-incremental.json").read_text())
recovery = json.loads((demo / "06-recovery.json").read_text())
busy = json.loads((demo / "08-concurrency.json").read_text())
stale = json.loads((demo / "05-replay-stale.json").read_text())
rollback = json.loads((demo / "07-rollback.json").read_text())
drift = json.loads((demo / "02-semantic-drift.json").read_text())

assert inc["code"] == "OK"
assert set(inc["recomputed_kinds"]) == {"root_crosscheck", "checksums", "export"}
assert set(inc["reused_kinds"]) == {"normalize", "preanalysis", "arcs", "labels"}
assert inc["linguistic_result_changed_sentence_ids"] == ["en-001"]
assert stale["code"] == "E_STALE_RESULT"
assert len(stale["details"]["stale_sentence_ids"]) == 6
assert drift["code"] == "E_SEMANTIC_DRIFT"
assert "root_policy.search_window" in drift["details"]["difference_field"]
assert len(recovery["recovered_node_instances"]) == 12
assert busy["code"] == "E_STAGE_BUSY"
assert rollback["code"] == "E_SNAPSHOT_ROLLBACK"
print("DEMO_ASSERTIONS: OK")
print("recomputed_kinds:", ",".join(inc["recomputed_kinds"]))
print("reused_kinds:", ",".join(inc["reused_kinds"]))
print("linguistic_changed:", ",".join(inc["linguistic_result_changed_sentence_ids"]))
print("recovered_instances:", len(recovery["recovered_node_instances"]))
PY
