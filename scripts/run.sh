#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
exec go run ./cmd/middleware -addr "${ADDR:-127.0.0.1:19090}" -data "${DATA_DIR:-.runtime/data}" -retry-after "${RETRY_AFTER:-1s}"
