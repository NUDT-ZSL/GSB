#!/usr/bin/env bash
# Deterministic demo for the two upload-service defects (Git Bash / POSIX sh).
# Usage:
#   1. start a service in another terminal:
#        python vuln_server.py            (port 8001)
#        python fixed_server.py           (port 8002)
#   2. run:  ./demo.sh vuln     or     ./demo.sh fixed
set -euo pipefail
cd "$(dirname "$0")"

case "${1:-}" in
  vuln)  port=8001; workdir="vuln_workdir" ;;
  fixed) port=8002; workdir="fixed_workdir" ;;
  *) echo "usage: $0 {vuln|fixed}" >&2; exit 2 ;;
esac

echo "== detector against $1 service (port $port) =="
python detect.py --target "$1" --url "http://127.0.0.1:$port" --base-dir "$workdir"

echo
echo "== on-disk state of $workdir =="
if [ -d "$workdir" ]; then
  find "$workdir" -type f -exec ls -l {} \;
else
  echo "(workdir not created yet)"
fi
