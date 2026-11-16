#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GO_BIN="${GO:-go}"
OUT="${1:-$ROOT/out/demo}"
BIN="$ROOT/bin/meshctl"
SUMMARY="$OUT/demo-summary.txt"
LOG="$OUT/mesh.log"
PORTS=(18101 18102 18103 18104 18105 18106)
MOCK_PID=""

cleanup() {
  if [[ -n "$MOCK_PID" ]] && kill -0 "$MOCK_PID" 2>/dev/null; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if ! command -v "$GO_BIN" >/dev/null 2>&1; then
  echo "E_RULE: go command not found (set GO=/path/to/go)" >&2
  exit 2
fi
if command -v ss >/dev/null 2>&1; then
  for port in "${PORTS[@]}"; do
    if ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN; then
      echo "E_RULE: loopback port $port is already in use" >&2
      exit 2
    fi
  done
fi

rm -rf "$OUT"
mkdir -p "$OUT" "$ROOT/bin"
: >"$SUMMARY"
: >"$LOG"

echo "== build ==" | tee -a "$SUMMARY"
"$GO_BIN" build -o "$BIN" "$ROOT/cmd/meshctl"
"$BIN" version | tee -a "$SUMMARY"

echo "== start local mock upstreams ==" | tee -a "$SUMMARY"
"$BIN" mock \
  -endpoint '127.0.0.1:18101=ok:2' \
  -endpoint '127.0.0.1:18102=ok:2' \
  -endpoint '127.0.0.1:18103=ok:2' \
  -endpoint '127.0.0.1:18104=ok:2' \
  -endpoint '127.0.0.1:18105=fail:2' \
  -endpoint '127.0.0.1:18106=ok:2' \
  -ready-file "$OUT/mock.ready" >"$OUT/mock.log" 2>&1 &
MOCK_PID=$!
for _ in $(seq 1 100); do
  [[ -f "$OUT/mock.ready" ]] && break
  if ! kill -0 "$MOCK_PID" 2>/dev/null; then
    echo "E_UPSTREAM: mock failed to start" >&2
    cat "$OUT/mock.log" >&2 || true
    exit 4
  fi
  sleep 0.05
done
[[ -f "$OUT/mock.ready" ]] || { echo "E_UPSTREAM: mock readiness timeout" >&2; exit 4; }

snapshot_for() {
  local round="$1"
  ls -1 "$OUT"/snapshot-"$round"-*.json 2>/dev/null | tail -n 1 || true
}

verify_snapshot() {
  local snapshot="$1"
  "$BIN" verify -snapshot "$snapshot" -log "$LOG"
}

run_case() {
  local label="$1" rules_file="$2" requests_file="$3" round="$4" expected="$5"
  shift 5
  echo "== $label ==" | tee -a "$SUMMARY"
  set +e
  "$BIN" run -rules "$rules_file" -requests "$requests_file" -round "$round" -out "$OUT" -log "$LOG" "$@" >"$OUT/run-$round.out" 2>&1
  local code=$?
  set -e
  grep -vE '^(VERIFY |VERIFY_OK)' "$OUT/run-$round.out" | tee -a "$SUMMARY"
  if [[ "$code" != "$expected" ]]; then
    echo "ASSERT_FAILED round=$round exit=$code expected=$expected" | tee -a "$SUMMARY"
    exit 1
  fi
  echo "RUN_RESULT round=$round exit=$code expected=$expected" | tee -a "$SUMMARY"
  local snapshot
  snapshot="$(snapshot_for "$round")"
  if [[ -n "$snapshot" ]]; then
    echo "VERIFY_TOOL round=$round snapshot=$snapshot" | tee -a "$SUMMARY"
    verify_snapshot "$snapshot" | tee -a "$SUMMARY"
  fi
}

run_case "cold-start round" "$ROOT/configs/rules-v1.json" "$ROOT/configs/requests-cold.json" cold 0
run_case "hot rules round" "$ROOT/configs/rules-v2.json" "$ROOT/configs/requests-hot.json" hot 0

COLD_SNAPSHOT="$(snapshot_for cold)"
HOT_SNAPSHOT="$(snapshot_for hot)"
echo "== route/version diff ==" | tee -a "$SUMMARY"
"$BIN" diff -from "$COLD_SNAPSHOT" -to "$HOT_SNAPSHOT" -out "$OUT/route-diff.json" | tee -a "$SUMMARY"

run_case "fault-injection round" "$ROOT/configs/rules-v2.json" "$ROOT/configs/requests-fault.json" fault 4
run_case "upstream-unreachable CLI injection" "$ROOT/configs/rules-v2.json" "$ROOT/configs/requests-upstream-inject.json" upstream-inject 4 -inject-failure upstream
run_case "corrupted rule input" "$ROOT/configs/rules-broken.json" "$ROOT/configs/requests-hot.json" rule-corrupt 2
run_case "rule-corruption CLI injection" "$ROOT/configs/rules-v2.json" "$ROOT/configs/requests-hot.json" rule-inject 2 -inject-failure rule
run_case "snapshot-write CLI injection" "$ROOT/configs/rules-v2.json" "$ROOT/configs/requests-hot.json" snapshot-inject 5 -inject-failure snapshot

# This successful round proves business failures above did not poison the process state.
run_case "post-failure recovery round" "$ROOT/configs/rules-v2.json" "$ROOT/configs/requests-hot.json" post-failure 0

echo "== forced termination and recovery ==" | tee -a "$SUMMARY"
set +e
"$BIN" run -rules "$ROOT/configs/rules-v2.json" -requests "$ROOT/configs/requests-crash.json" -round crash -out "$OUT" -log "$LOG" >"$OUT/crash.stdout" 2>"$OUT/crash.stderr" &
CRASH_PID=$!
sleep 0.6
kill -9 "$CRASH_PID" 2>/dev/null
wait "$CRASH_PID" 2>/dev/null
CRASH_EXIT=$?
set -e
echo "CRASH exit=$CRASH_EXIT checkpoint=$OUT/checkpoint-current.json" | tee -a "$SUMMARY"
if [[ "$CRASH_EXIT" != "137" ]]; then
  echo "ASSERT_FAILED expected SIGKILL exit 137" | tee -a "$SUMMARY"
  exit 1
fi
"$BIN" recover -out "$OUT" -checkpoint "$OUT/checkpoint-current.json" -log "$LOG" >"$OUT/recover.out" 2>&1
cat "$OUT/recover.out" | tee -a "$SUMMARY"
RERUN_FILE="$(sed -n 's/.*rerun_file=\([^ ]*\).*/\1/p' "$OUT/recover.out" | tail -n1)"
if [[ -z "$RERUN_FILE" || ! -f "$RERUN_FILE" ]]; then
  echo "ASSERT_FAILED recovery did not produce a rerun request file" | tee -a "$SUMMARY"
  exit 1
fi
run_case "rerun of unknown requests" "$ROOT/configs/rules-v2.json" "$RERUN_FILE" crash-retry 0

echo "== stable scenario summaries ==" | tee -a "$SUMMARY"
grep -E '^(VERIFY|RECOVER|DIFF|CRASH|RUN_RESULT) ' "$SUMMARY" | tee "$OUT/scenario-summaries.txt"

echo "== final artifact list ==" | tee -a "$SUMMARY"
find "$OUT" -maxdepth 1 -type f ! -name '.*' -printf '%f\n' | sort | tee -a "$SUMMARY"
echo "DEMO_OK artifacts=$OUT"
