#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
go_bin="${GO:-go}"
"$go_bin" run ./cmd/mkfixtures -out samples

