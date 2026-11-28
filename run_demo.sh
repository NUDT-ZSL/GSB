#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p artifacts
python3 -m aqua_edge demo --commands-file demo_commands.txt | tee artifacts/demo_output.txt
