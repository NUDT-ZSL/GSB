# Deterministic offline verification for Windows PowerShell.
# Requires Python 3.11 or 3.12 on PATH as `python`.
$ErrorActionPreference = "Continue"

python --version
python -m pytest --version
if ($LASTEXITCODE -ne 0) {
    python -m pip install pytest
}

python -m account_verify.samplegen --output samples
python -m pytest -q

python -m account_verify.cli import --samples samples --state state

# Exit code 1 is expected here because the fixed sample contains one policy conflict.
python -m account_verify.cli migrate --state state --batch-size 2

python -m account_verify.cli export-report `
    --state state `
    --samples samples `
    --output reports\baseline-report.json

python -m account_verify.cli use-token tok-alpha-valid --state state

# Second use must return E_TOKEN_REUSED and exit code 1.
python -m account_verify.cli use-token tok-alpha-valid --state state

python -m account_verify.cli migrate `
    --state state `
    --upgrade-policy samples\policy_upgrade_v3.json

python -m account_verify.cli export-report `
    --state state `
    --samples samples `
    --output reports\diagnostic-report.json

python -m account_verify.cli verify-checkpoint --state state

Write-Host "Expected CLI contract: success=0, partial/business rejection=1, parameter error=2, lock conflict=3."
