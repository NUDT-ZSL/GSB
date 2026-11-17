#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p snapshots temp reports artifacts
find snapshots -mindepth 1 -delete
find temp -mindepth 1 -delete
find reports -mindepth 1 -delete
find artifacts -mindepth 1 -type f ! -name 'workspace.json' -delete
echo "cleared snapshots/, temp/, reports/, and generated artifacts/"

