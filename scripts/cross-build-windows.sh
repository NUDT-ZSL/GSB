#!/usr/bin/env bash
# Optional: cross-compile Windows binaries from Linux/macOS for deployment.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p bin
GOOS=windows GOARCH=amd64 go build -o bin/lockd.exe ./cmd/lockd
GOOS=windows GOARCH=amd64 go build -o bin/lockctl.exe ./cmd/lockctl
echo "built bin/lockd.exe bin/lockctl.exe"
