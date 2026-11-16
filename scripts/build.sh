#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
if ! command -v go >/dev/null 2>&1; then
  echo "go is not on PATH; install Go 1.21+ or add it to PATH" >&2
  exit 1
fi
export CGO_ENABLED=0
mkdir -p output/bin
go build -mod=vendor -trimpath -o output/bin/orderqueue ./cmd/orderqueue
go build -mod=vendor -trimpath -o output/bin/orderverify ./cmd/orderverify
echo "built output/bin/orderqueue and output/bin/orderverify"
