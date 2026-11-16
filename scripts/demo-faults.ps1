$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$scenario = Join-Path $script:Root "output\demo-faults"
Reset-Directory $scenario
Invoke-Fault "invalid-partition" "$scenario\invalid-work" "$scenario\invalid-partition.json"
Invoke-Fault "corrupt-ledger" "$scenario\corrupt-work" "$scenario\corrupt-ledger.json"
Invoke-Fault "persistent-failure" "$scenario\poison-work" "$scenario\persistent-failure.json"
Write-Output "fault injection scenario passed: $scenario"
