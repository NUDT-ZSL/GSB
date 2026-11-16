$ErrorActionPreference = "Stop"
$script:Root = (Convert-Path (Join-Path $PSScriptRoot ".."))
$script:Binary = Join-Path $script:Root "output\bin\orderqueue.exe"
Set-Location $script:Root

function Reset-Directory([string]$Path) {
    if (Test-Path $Path) {
        Get-ChildItem -Force -Path $Path | Remove-Item -Recurse -Force
    } else {
        New-Item -ItemType Directory -Force -Path $Path | Out-Null
    }
}

function Test-TcpPortInUse([int]$Port) {
    try {
        $listeners = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop
        if ($listeners) { return $true }
    } catch {
    }
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if ($async.AsyncWaitHandle.WaitOne(250)) {
            $client.EndConnect($async)
            return $true
        }
    } catch {
    } finally {
        $client.Close()
    }
    return $false
}

function Quote-Argument([string]$Value) {
    if ($Value -match '[\s"]') {
        return '"' + ($Value -replace '"', '\"') + '"'
    }
    return $Value
}

function Start-OrderQueue(
    [string]$ScenarioDir,
    [int]$Port,
    [string]$Db,
    [string]$Log,
    [string]$Recovery,
    [string]$Ready,
    [string]$WindowTimeout = "5s"
) {
    if (Test-TcpPortInUse $Port) {
        throw "TCP port $Port is already in use"
    }
    $serverArgs = @(
        "serve",
        "--listen", "127.0.0.1:$Port",
        "--db", $Db,
        "--log", $Log,
        "--recovery-report", $Recovery,
        "--ready-file", $Ready,
        "--max-retries", "3",
        "--window-timeout", $WindowTimeout
    )
    $argumentLine = ($serverArgs | ForEach-Object { Quote-Argument $_ }) -join " "
    $process = Start-Process -FilePath $script:Binary -ArgumentList $argumentLine -PassThru -NoNewWindow `
        -RedirectStandardOutput (Join-Path $ScenarioDir "server.stdout") `
        -RedirectStandardError (Join-Path $ScenarioDir "server.stderr")
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            throw "server exited before readiness with code $($process.ExitCode); see $ScenarioDir\server.stderr"
        }
        if (Test-Path $Ready) {
            try {
                $info = Get-Content -Raw -Path $Ready | ConvertFrom-Json
                if ($info.ready -eq $true) {
                    return [pscustomobject]@{ Process = $process; Ready = $info }
                }
            } catch {
            }
        }
        Start-Sleep -Milliseconds 50
    }
    throw "timed out waiting for readiness file $Ready"
}

function Stop-OrderQueueForce($Process) {
    if ($null -eq $Process) { return }
    if (-not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        try { $Process.WaitForExit(5000) | Out-Null } catch { }
    }
}

function Invoke-Checked([string]$FilePath, [string[]]$Arguments) {
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath exited with code $LASTEXITCODE"
    }
}

function Start-OrderQueueClient([string]$Executable, [string[]]$Arguments, [string]$StdoutPath, [string]$StderrPath) {
    $argumentLine = ($Arguments | ForEach-Object { Quote-Argument $_ }) -join " "
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Executable
    $info.Arguments = $argumentLine
    $info.WorkingDirectory = $script:Root
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.CreateNoWindow = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    if (-not $process.Start()) { throw "failed to start $Executable" }
    return [pscustomobject]@{
        Process = $process
        StdoutTask = $process.StandardOutput.ReadToEndAsync()
        StderrTask = $process.StandardError.ReadToEndAsync()
        StdoutPath = $StdoutPath
        StderrPath = $StderrPath
    }
}

function Wait-Client([object]$Client, [string]$Name) {
    $Client.Process.WaitForExit()
    $stdout = $Client.StdoutTask.Result
    $stderr = $Client.StderrTask.Result
    [System.IO.File]::WriteAllText($Client.StdoutPath, $stdout)
    [System.IO.File]::WriteAllText($Client.StderrPath, $stderr)
    if ($Client.Process.ExitCode -ne 0) {
        throw "$Name exited with code $($Client.Process.ExitCode): $stderr"
    }
}

function Invoke-Verify([string]$Db, [string]$Report, [string]$Log) {
    Invoke-Checked $script:Binary @("verify", "--db", $Db, "--out", $Report, "--log", $Log)
}

function Invoke-Fault([string]$Kind, [string]$WorkDir, [string]$Report) {
    New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
    Invoke-Checked $script:Binary @("fault", "--kind", $Kind, "--out", $Report, "--work-dir", $WorkDir, "--log", (Join-Path $WorkDir "fault.jsonl"))
}
