$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$Database = "var/demo.sqlite"

python -m scheduler_diag load `
  --bundle samples/main `
  --db $Database `
  --reset `
  --out reports/demo-load.json

python -m scheduler_diag schedule `
  --bundle samples/main `
  --db $Database `
  --out reports/demo-schedule.json

python -m scheduler_diag status `
  --bundle samples/main `
  --db $Database `
  --out reports/demo-status.json

python -m scheduler_diag inject `
  --bundle samples/main `
  --db $Database `
  --out reports/demo-injections.json

python -m scheduler_diag verify `
  --bundle samples/main `
  --db $Database `
  --out reports/demo-verify.json
