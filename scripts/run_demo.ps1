param(
    [string]$Python = "python",
    [string]$Out = "artifacts/demo"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$env:PYTHONUTF8 = "1"

Write-Host "Python: $Python"
& $Python --version
if ($LASTEXITCODE -ne 0) {
    throw "无法运行 Python: $Python"
}

& $Python -m kgpipe demo --out $Out
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$Summary = Join-Path $Out "demo-summary.json"
Write-Host "演示结果: $Summary"
Get-Content $Summary -Raw | ConvertFrom-Json | Format-List

