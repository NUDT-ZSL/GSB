$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$scenario = Join-Path $script:Root "output\demo-normal"
Reset-Directory $scenario
$server = $null
try {
    $server = Start-OrderQueue $scenario 19160 "$scenario\orderqueue.db" "$scenario\server.jsonl" "$scenario\recovery-report.json" "$scenario\ready.json" "5s"
    Invoke-Checked $script:Binary @("produce", "--addr", "127.0.0.1:19160", "--input", "data\producer.jsonl", "--report", "$scenario\producer-report.json")

    $eu = Start-OrderQueueClient $script:Binary @("consume", "--addr", "127.0.0.1:19160", "--partition-key", "orders.eu", "--session", "normal-eu", "--limit", "2", "--window-timeout-ms", "5000", "--failure-plan", "data\no-failures.json", "--marker-out", "$scenario\eu-markers.jsonl", "--report", "$scenario\eu-consumer.json", "--target-offset", "6") "$scenario\eu-consumer.stdout" "$scenario\eu-consumer.stderr"
    $us = Start-OrderQueueClient $script:Binary @("consume", "--addr", "127.0.0.1:19160", "--partition-key", "orders.us", "--session", "normal-us", "--limit", "2", "--window-timeout-ms", "5000", "--failure-plan", "data\no-failures.json", "--marker-out", "$scenario\us-markers.jsonl", "--report", "$scenario\us-consumer.json", "--target-offset", "4") "$scenario\us-consumer.stdout" "$scenario\us-consumer.stderr"
    $audit = Start-OrderQueueClient $script:Binary @("consume", "--addr", "127.0.0.1:19160", "--partition-key", "audit.internal", "--session", "normal-audit", "--limit", "3", "--window-timeout-ms", "5000", "--failure-plan", "data\no-failures.json", "--marker-out", "$scenario\audit-markers.jsonl", "--report", "$scenario\audit-consumer.json", "--target-offset", "3") "$scenario\audit-consumer.stdout" "$scenario\audit-consumer.stderr"
    Wait-Client $eu "normal-eu"
    Wait-Client $us "normal-us"
    Wait-Client $audit "normal-audit"

    Invoke-Verify "$scenario\orderqueue.db" "$scenario\verification-report.json" "$scenario\verify.jsonl"
    Invoke-Checked $script:Binary @("dump", "--db", "$scenario\orderqueue.db", "--out", "$scenario\ledger-dump.json")
} finally {
    if ($server) { Stop-OrderQueueForce $server.Process }
}
Write-Output "normal scenario passed: $scenario\verification-report.json"
