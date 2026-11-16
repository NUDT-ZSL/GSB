#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
port="${1:-19160}"
check_port "$port"
echo "port ${port} is available"
