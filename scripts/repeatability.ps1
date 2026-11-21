$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

foreach ($Run in 1, 2) {
  python -m scheduler_diag load `
    --bundle samples/main `
    --db "var/repeat-$Run.sqlite" `
    --reset `
    --out "reports/repeat-$Run-load.json" | Out-Null
  python -m scheduler_diag schedule `
    --bundle samples/main `
    --db "var/repeat-$Run.sqlite" `
    --out "reports/repeat-$Run-schedule.json" | Out-Null
  python -m scheduler_diag status `
    --bundle samples/main `
    --db "var/repeat-$Run.sqlite" `
    --out "reports/repeat-$Run-status.json" | Out-Null
}

foreach ($Name in "load", "schedule", "status") {
  $First = (Get-FileHash "reports/repeat-1-$Name.json" -Algorithm SHA256).Hash
  $Second = (Get-FileHash "reports/repeat-2-$Name.json" -Algorithm SHA256).Hash
  if ($First -ne $Second) {
    throw "repeatability failed for $Name"
  }
}

Write-Output "repeatability: byte-identical"
