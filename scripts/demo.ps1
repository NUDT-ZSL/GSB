$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Out = if ($args.Count -gt 0) { $args[0] } else { Join-Path $Root "out\demo" }
$Bin = Join-Path $Root "bin\meshctl.exe"
$Log = Join-Path $Out "mesh.log"
$Summary = Join-Path $Out "demo-summary.txt"
$Ports = 18101..18106
$Mock = $null

function Write-Summary([string]$Text) {
    $Text | Tee-Object -FilePath $Summary -Append
}

function Quote-NativeArg([string]$Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Test-PortFree([int]$Port) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if ($async.AsyncWaitHandle.WaitOne(120) -and $client.Connected) { return $false }
        return $true
    } catch {
        return $true
    } finally {
        $client.Close()
    }
}

function Snapshot-For([string]$Round) {
    $item = Get-ChildItem $Out -Filter "snapshot-$Round-*.json" -File | Sort-Object Name | Select-Object -Last 1
    if ($null -eq $item) { return "" }
    return $item.FullName
}

function Invoke-Verify([string]$SnapshotPath) {
    & $Bin verify -snapshot $SnapshotPath -log $Log
    if ($LASTEXITCODE -ne 0) { throw "E_VERIFY: verification failed for $SnapshotPath" }
}

function Invoke-Round([string]$Label, [string]$Rules, [string]$Requests, [string]$Round, [int]$Expected, [string[]]$Extra) {
    Write-Summary "== $Label =="
    $arguments = @("run", "-rules", $Rules, "-requests", $Requests, "-round", $Round, "-out", $Out, "-log", $Log) + $Extra
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $Bin @arguments 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldPreference
    }
    $output | Tee-Object -FilePath $Summary -Append
    if ($code -ne $Expected) {
        throw "ASSERT_FAILED round=$Round exit=$code expected=$Expected"
    }
    $snapshot = Snapshot-For $Round
    if ($snapshot -ne "") {
        Write-Summary "VERIFY_TOOL round=$Round snapshot=$snapshot"
        Invoke-Verify $snapshot | Tee-Object -FilePath $Summary -Append
    }
}

try {
    if (Get-Command go -ErrorAction SilentlyContinue) {
        $go = "go"
    } else {
        throw "E_RULE: go command is not on PATH"
    }
    foreach ($port in $Ports) {
        if (-not (Test-PortFree $port)) { throw "E_RULE: loopback port $port is already in use" }
    }

    if (Test-Path $Out) { Remove-Item -Recurse -Force $Out }
    New-Item -ItemType Directory -Force -Path $Out, (Join-Path $Root "bin") | Out-Null
    Set-Content -Path $Summary -Value ""
    Set-Content -Path $Log -Value ""

    Write-Summary "== build =="
    & $go build -o $Bin (Join-Path $Root "cmd\meshctl")
    if ($LASTEXITCODE -ne 0) { throw "build failed" }
    (& $Bin version) | Tee-Object -FilePath $Summary -Append

    Write-Summary "== start local mock upstreams =="
    $mockArgs = @(
        "mock",
        "-endpoint", "127.0.0.1:18101=ok:2",
        "-endpoint", "127.0.0.1:18102=ok:2",
        "-endpoint", "127.0.0.1:18103=ok:2",
        "-endpoint", "127.0.0.1:18104=ok:2",
        "-endpoint", "127.0.0.1:18105=fail:2",
        "-endpoint", "127.0.0.1:18106=ok:2",
        "-ready-file", (Quote-NativeArg (Join-Path $Out "mock.ready"))
    )
    $Mock = Start-Process -FilePath $Bin -ArgumentList $mockArgs -RedirectStandardOutput (Join-Path $Out "mock.log") -RedirectStandardError (Join-Path $Out "mock.err") -PassThru
    $ready = $false
    for ($i = 0; $i -lt 100; $i++) {
        if (Test-Path (Join-Path $Out "mock.ready")) { $ready = $true; break }
        if ($Mock.HasExited) { throw "E_UPSTREAM: mock process exited" }
        Start-Sleep -Milliseconds 50
    }
    if (-not $ready) { throw "E_UPSTREAM: mock readiness timeout" }

    Invoke-Round "cold-start round" (Join-Path $Root "configs\rules-v1.json") (Join-Path $Root "configs\requests-cold.json") "cold" 0 @()
    Invoke-Round "hot rules round" (Join-Path $Root "configs\rules-v2.json") (Join-Path $Root "configs\requests-hot.json") "hot" 0 @()

    $cold = Snapshot-For "cold"
    $hot = Snapshot-For "hot"
    Write-Summary "== route/version diff =="
    (& $Bin diff -from $cold -to $hot -out (Join-Path $Out "route-diff.json")) | Tee-Object -FilePath $Summary -Append

    Invoke-Round "fault-injection round" (Join-Path $Root "configs\rules-v2.json") (Join-Path $Root "configs\requests-fault.json") "fault" 4 @()
    Invoke-Round "upstream-unreachable CLI injection" (Join-Path $Root "configs\rules-v2.json") (Join-Path $Root "configs\requests-upstream-inject.json") "upstream-inject" 4 @("-inject-failure", "upstream")
    Invoke-Round "corrupted rule input" (Join-Path $Root "configs\rules-broken.json") (Join-Path $Root "configs\requests-hot.json") "rule-corrupt" 2 @()
    Invoke-Round "rule-corruption CLI injection" (Join-Path $Root "configs\rules-v2.json") (Join-Path $Root "configs\requests-hot.json") "rule-inject" 2 @("-inject-failure", "rule")
    Invoke-Round "snapshot-write CLI injection" (Join-Path $Root "configs\rules-v2.json") (Join-Path $Root "configs\requests-hot.json") "snapshot-inject" 5 @("-inject-failure", "snapshot")
    Invoke-Round "post-failure recovery round" (Join-Path $Root "configs\rules-v2.json") (Join-Path $Root "configs\requests-hot.json") "post-failure" 0 @()

    Write-Summary "== forced termination and recovery =="
    $crash = Start-Process -FilePath $Bin -ArgumentList @(
        "run", "-rules", (Quote-NativeArg (Join-Path $Root "configs\rules-v2.json")),
        "-requests", (Quote-NativeArg (Join-Path $Root "configs\requests-crash.json")),
        "-round", "crash", "-out", (Quote-NativeArg $Out), "-log", (Quote-NativeArg $Log)
    ) -RedirectStandardOutput (Join-Path $Out "crash.stdout") -RedirectStandardError (Join-Path $Out "crash.stderr") -PassThru
    Start-Sleep -Milliseconds 600
    Stop-Process -Id $crash.Id -Force
    $crash.WaitForExit()
    Write-Summary "CRASH exit=$($crash.ExitCode) checkpoint=$Out\checkpoint-current.json"

    $recoverOutput = & $Bin recover -out $Out -checkpoint (Join-Path $Out "checkpoint-current.json") -log $Log 2>&1
    $recoverCode = $LASTEXITCODE
    $recoverOutput | Tee-Object -FilePath $Summary -Append
    if ($recoverCode -ne 0) { throw "recovery failed" }
    $match = [regex]::Match(($recoverOutput -join "`n"), "rerun_file=([^ ]+)")
    if (-not $match.Success) { throw "recovery did not produce a rerun file" }
    $rerunFile = $match.Groups[1].Value
    Invoke-Round "rerun of unknown requests" (Join-Path $Root "configs\rules-v2.json") $rerunFile "crash-retry" 0 @()

    Write-Summary "== stable scenario summaries =="
    Select-String -Path $Summary -Pattern '^(VERIFY|RECOVER|DIFF|CRASH) ' | ForEach-Object { $_.Line } | Tee-Object -FilePath (Join-Path $Out "scenario-summaries.txt")
    Write-Output "DEMO_OK artifacts=$Out"
} finally {
    if ($null -ne $Mock -and -not $Mock.HasExited) {
        Stop-Process -Id $Mock.Id -Force -ErrorAction SilentlyContinue
    }
}
