#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

go_bin="${GO:-go}"
if ! command -v "$go_bin" >/dev/null 2>&1; then
  echo "go command not found; install Go 1.21+ and add it to PATH" >&2
  exit 1
fi

mkdir -p bin
"$go_bin" build -o bin/localdns ./cmd/localdns
"$go_bin" build -o bin/dnsupstream ./cmd/dnsupstream
"$go_bin" build -o bin/dnsdemo ./cmd/dnsdemo

exec ./bin/dnsdemo \
  -zone samples/zone.json \
  -wire samples/answer.wire \
  -wire-query samples/query.wire \
  -upstream-exe ./bin/dnsupstream

