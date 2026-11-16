#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

go_bin="${GO:-go}"
if ! command -v "$go_bin" >/dev/null 2>&1; then
  echo "go command not found; install Go 1.21+ and add it to PATH" >&2
  exit 1
fi

"$go_bin" test ./...
"$go_bin" vet ./...

if command -v "${CC:-gcc}" >/dev/null 2>&1; then
  CGO_ENABLED=1 "$go_bin" test -race ./...
else
  echo "race test skipped: CGO C compiler not found (set CC to a working C compiler)"
fi
