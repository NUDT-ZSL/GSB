#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

scenario="$ROOT/output/demo-faults"
reset_dir "$scenario"
run_fault invalid-partition "$scenario/invalid-work" "$scenario/invalid-partition.json"
run_fault corrupt-ledger "$scenario/corrupt-work" "$scenario/corrupt-ledger.json"
run_fault persistent-failure "$scenario/poison-work" "$scenario/persistent-failure.json"
echo "fault injection scenario passed: $scenario"
