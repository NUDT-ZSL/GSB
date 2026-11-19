param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CliArgs
)

$ErrorActionPreference = "Stop"
$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $python) {
    Write-Error "python is not available on PATH"
    exit 127
}

& $python.Source "$PSScriptRoot\colagg.py" @CliArgs
exit $LASTEXITCODE

