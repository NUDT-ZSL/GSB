# Run from repository root in Windows PowerShell:
#   python -m venv .venv
#   .venv\Scripts\Activate.ps1
#   pip install pytest
#   .\scripts\demo.ps1

$ErrorActionPreference = "Continue"

python -m auth_interop.samplegen --output samples --seed 20261125
python -m pytest -q

Write-Host "`n[import] intentional bad/conflict fixtures make exit code 1 expected"
python -m auth_interop.cli import --sample-dir samples --state-dir state --report artifacts/reports/import-report.json --force-rebuild
Write-Host "import_exit=$LASTEXITCODE"

Write-Host "`n[redeem happy path] expected exit 0"
python -m auth_interop.cli redeem --state-dir state --sample-dir samples --clock samples/clock.json --attempts samples/redemption_attempts_success.json --report artifacts/reports/redeem-success.json
Write-Host "redeem_happy_exit=$LASTEXITCODE"

Write-Host "`n[redeem full diagnostic set] expected exit 1"
python -m auth_interop.cli redeem --state-dir state --sample-dir samples --clock samples/clock.json --attempts samples/redemption_attempts.json --report artifacts/reports/redeem-report.json
Write-Host "redeem_full_exit=$LASTEXITCODE"

Write-Host "`n[revocation and cycle] expected exit 1"
python -m auth_interop.cli revoke --state-dir state --sample-dir samples --clock samples/clock.json --requests samples/revocation_requests.json --report artifacts/reports/revoke-report.json
Write-Host "revoke_exit=$LASTEXITCODE"

Write-Host "`n[checkpoint verify] expected exit 0"
python -m auth_interop.cli checkpoint --state-dir state --report artifacts/reports/checkpoint-verify.json
Write-Host "checkpoint_exit=$LASTEXITCODE"

Write-Host "`n[batch] two clean samples and one malformed sample; expected exit 1"
python -m auth_interop.cli import --batch-dir samples/batch --state-root state/batch --report-dir artifacts/reports/batch --force-rebuild
Write-Host "batch_exit=$LASTEXITCODE"

Write-Host "`n[view]"
python -m auth_interop.cli view --state-dir state --revocations samples/revocation_requests.json --format table
