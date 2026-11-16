#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
BIN="$ROOT/output/bin/orderqueue"

reset_dir() {
  local dir="$1"
  mkdir -p "$dir"
  find "$dir" -mindepth 1 -delete
}

check_port() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}$"; then
      echo "TCP port ${port} is already in use" >&2
      return 1
    fi
  elif command -v lsof >/dev/null 2>&1; then
    if lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "TCP port ${port} is already in use" >&2
      return 1
    fi
  else
    if (echo >/dev/tcp/127.0.0.1/"${port}") >/dev/null 2>&1; then
      echo "TCP port ${port} is already in use" >&2
      return 1
    fi
  fi
}

wait_ready() {
  local ready_file="$1"
  local pid="$2"
  for _ in $(seq 1 200); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "server process exited before readiness" >&2
      return 1
    fi
    if [[ -s "$ready_file" ]] && grep -q '"ready": true' "$ready_file"; then
      return 0
    fi
    sleep 0.05
  done
  echo "timed out waiting for readiness file ${ready_file}" >&2
  return 1
}

start_server() {
  local scenario_dir="$1"
  local port="$2"
  local db="$3"
  local log="$4"
  local recovery="$5"
  local ready="$6"
  local window_timeout="$7"
  check_port "$port"
  "$BIN" serve \
    --listen "127.0.0.1:${port}" \
    --db "$db" \
    --log "$log" \
    --recovery-report "$recovery" \
    --ready-file "$ready" \
    --max-retries 3 \
    --window-timeout "$window_timeout" \
    >"$scenario_dir/server.stdout" \
    2>"$scenario_dir/server.stderr" &
  SERVER_PID=$!
  wait_ready "$ready" "$SERVER_PID"
}

stop_server_force() {
  local pid="${1:-${SERVER_PID:-}}"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
}

run_verify() {
  local db="$1"
  local report="$2"
  local log="$3"
  "$BIN" verify --db "$db" --out "$report" --log "$log"
}

run_fault() {
  local kind="$1"
  local dir="$2"
  local out="$3"
  mkdir -p "$dir"
  "$BIN" fault --kind "$kind" --out "$out" --work-dir "$dir" --log "$dir/fault.jsonl"
}
