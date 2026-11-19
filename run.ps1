param(
    [ValidateSet("demo", "validate", "ui", "generate", "show")]
    [string]$Command = "demo",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) {
    $Python = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $Python) {
    throw "Python 3.10+ was not found on PATH."
}

& $Python.Source -m annotation_app $Command @Rest
exit $LASTEXITCODE

