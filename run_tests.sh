#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3.11 -m unittest discover -s tests -v
