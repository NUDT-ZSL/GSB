# Requires Python 3.11+ with sqlite3. No third-party packages are required.
$ErrorActionPreference = "Stop"

py -3.11 generate_samples.py --root samples/generated
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

py -3.11 -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

py -3.11 -m sbom_audit.verify --sample-dir samples/generated --work-dir .verify-work
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

py -3.11 run_audit.py import-snapshots `
  --sample-dir samples/generated `
  --fresh `
  --ledger .sbom-audit/ledger.sqlite3 `
  --checkpoint .sbom-audit/checkpoint.json `
  --report .sbom-audit/report.json
$importExit = $LASTEXITCODE
if ($importExit -gt 1) { exit $importExit }

py -3.11 run_audit.py verify-checkpoint `
  --ledger .sbom-audit/ledger.sqlite3 `
  --checkpoint .sbom-audit/checkpoint.json
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

py -3.11 run_audit.py export-closure `
  --ledger .sbom-audit/ledger.sqlite3 `
  --checkpoint .sbom-audit/checkpoint.json `
  --out .sbom-audit/closure.json
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Verification completed. Import exit code: $importExit (0=clean processing, 1=partial rejection/findings)."
