#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

go_bin="${GO:-go}"
udp_addr="${LOCALDNS_UDP:-127.0.0.1:15353}"
tcp_addr="${LOCALDNS_TCP:-127.0.0.1:15354}"
upstream_addr="${LOCALDNS_UPSTREAM:-127.0.0.1:15355}"

mkdir -p bin
"$go_bin" build -o bin/localdns ./cmd/localdns
"$go_bin" build -o bin/dnsupstream ./cmd/dnsupstream

ready_file="$(mktemp)"
upstream_log="$(mktemp)"
cleanup() {
  if [[ -n "${upstream_pid:-}" ]] && kill -0 "$upstream_pid" 2>/dev/null; then
    kill "$upstream_pid" 2>/dev/null || true
    wait "$upstream_pid" 2>/dev/null || true
  fi
  rm -f "$ready_file" "$upstream_log"
}
trap cleanup EXIT INT TERM

./bin/dnsupstream -listen "$upstream_addr" -ready-file "$ready_file" >"$upstream_log" 2>&1 &
upstream_pid=$!
for _ in $(seq 1 50); do
  [[ -s "$ready_file" ]] && break
  if ! kill -0 "$upstream_pid" 2>/dev/null; then
    echo "upstream failed:" >&2
    cat "$upstream_log" >&2
    exit 1
  fi
  sleep 0.1
done
if [[ ! -s "$ready_file" ]]; then
  echo "timed out waiting for upstream readiness" >&2
  cat "$upstream_log" >&2
  exit 1
fi

exec 3<"$ready_file"
IFS= read -r actual_upstream <&3 || actual_upstream="$upstream_addr"
exec 3<&-
echo "mock upstream child: $actual_upstream (pid $upstream_pid)"
echo "try: udp same.demo.test A ; tcp api.external.test A ; tcp portal.external.test A ; stats ; quit"

./bin/localdns \
  -zone samples/zone.json \
  -wire samples/answer.wire \
  -wire-query samples/query.wire \
  -udp "$udp_addr" \
  -tcp "$tcp_addr" \
  -upstream "$actual_upstream"

