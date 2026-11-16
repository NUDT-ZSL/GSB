#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
bash scripts/build.sh
bash scripts/demo-normal.sh
bash scripts/demo-failure.sh
bash scripts/demo-recovery.sh
bash scripts/demo-faults.sh
echo "all scenarios passed"
