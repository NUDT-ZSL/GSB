$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$scenario = Join-Path $script:Root "output\demo-recovery"
Reset-Directory $scenario
$server = $null
try {
    $server = Start-OrderQueue $scenario 19162 "$scenario\orderqueue.db" "$scenario\server-1.jsonl" "$scenario\recovery-1.json" "$scenario\ready-1.json" "30s"
    Invoke-Checked $script:Binary @("produce", "--addr", "127.0.0.1:19162", "--input", "data\producer.jsonl", "--report", "$scenario\producer-report.json")
    Invoke-Checked $script:Binary @("consume", "--addr", "127.0.0.1:19162", "--partition-key", "orders.eu", "--session", "recovery-session", "--limit", "3", "--window-timeout-ms", "30000", "--failure-plan", "data\no-failures.json", "--marker-out", "$scenario\pull-only-markers.jsonl", "--report", "$scenario\pull-only.json", "--pull-only")
    Invoke-Checked $script:Binary @("status", "--addr", "127.0.0.1:19162", "--out", "$scenario\status-before-crash.json")

    Stop-OrderQueueForce $server.Process
    $server = Start-OrderQueue $scenario 19162 "$scenario\orderqueue.db" "$scenario\server-2.jsonl" "$scenario\recovery-2.json" "$scenario\ready-2.json" "30s"

    $recovery = Get-Content -Raw -Path "$scenario\recovery-2.json" | ConvertFrom-Json
    if ($recovery.recovered_windows.Count -lt 1) {
        throw "recovery report did not list the unconfirmed window"
    }
    Invoke-Checked $script:Binary @("consume", "--addr", "127.0.0.1:19162", "--partition-key", "orders.eu", "--session", "recovery-session-2", "--limit", "3", "--window-timeout-ms", "30000", "--failure-plan", "data\no-failures.json", "--marker-out", "$scenario\recovery-markers.jsonl", "--report", "$scenario\recovery-consumer.json", "--target-offset", "6")
    Invoke-Verify "$scenario\orderqueue.db" "$scenario\verification-report.json" "$scenario\verify.jsonl"
    Invoke-Checked $script:Binary @("dump", "--db", "$scenario\orderqueue.db", "--out", "$scenario\ledger-dump.json")
} finally {
    if ($server) { Stop-OrderQueueForce $server.Process }
}
Write-Output "forced-termination recovery scenario passed: $scenario\recovery-2.json"
