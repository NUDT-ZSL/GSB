# Windows PowerShell verification entry point.
# Run from the project root:
#   powershell -ExecutionPolicy Bypass -File .\tools\Verify-Halftone.ps1

$ErrorActionPreference = "Stop"

python --version
python -m pip install "numpy>=1.26"
python -m halftone seed --root .
python -m unittest discover -s tests
python -m halftone run --root . --workers 2 --check
python -m halftone consistency-demo

Write-Host "Verification completed. See out\report.json for job and injection results."
