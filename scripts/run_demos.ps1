$ErrorActionPreference = "Stop"

$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }

function Invoke-Demo([string[]]$Arguments) {
    & $Python -m multilabel_lab.cli @Arguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

Invoke-Demo @(
    "classify",
    "--state-dir", "artifacts/snapshots/demo-normal",
    "--samples", "8",
    "--output", "artifacts/reports/normal.json"
)

Invoke-Demo @(
    "inject",
    "--state-dir", "artifacts/snapshots/demo-injection",
    "--samples", "8",
    "--injections", "examples/injections_multi.json",
    "--output", "artifacts/reports/injection_multi.json"
)

Invoke-Demo @(
    "classify",
    "--state-dir", "artifacts/snapshots/demo-resource",
    "--samples", "12",
    "--max-samples", "5",
    "--sample-policy", "truncate",
    "--output", "artifacts/reports/resource_degraded.json"
)

Invoke-Demo @(
    "verify",
    "--state-dir", "artifacts/snapshots/demo-normal",
    "--samples", "8",
    "--output", "artifacts/reports/verification.json"
)

Invoke-Demo @(
    "metrics",
    "--report", "artifacts/reports/injection_multi.json",
    "--output", "artifacts/reports/metrics_recompute.json"
)
