#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p bin
go build -o bin/lockd ./cmd/lockd
go build -o bin/lockctl ./cmd/lockctl
echo "built bin/lockd bin/lockctl"
