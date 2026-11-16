#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
go version
python3 --version
mkdir -p bin
go build -trimpath -o bin/middleware ./cmd/middleware
echo "Built bin/middleware"
