#!/usr/bin/env bash
# Launch the Tkinter simulator UI.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=env.sh
source scripts/env.sh
exec python3 -m ddp_sim gui "$@"
