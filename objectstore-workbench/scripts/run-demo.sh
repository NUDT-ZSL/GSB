#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m oswb demo --chunk-size 131072 --global-concurrency 4 --session-quota 2
