$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Get-Command go -ErrorAction SilentlyContinue)) {
    throw "go command not found; install Go 1.21+ and add it to PATH"
}

$Udp = if ($env:LOCALDNS_UDP) { $env:LOCALDNS_UDP } else { "127.0.0.1:15353" }
$Tcp = if ($env:LOCALDNS_TCP) { $env:LOCALDNS_TCP } else { "127.0.0.1:15354" }
$Upstream = if ($env:LOCALDNS_UPSTREAM) { $env:LOCALDNS_UPSTREAM } else { "127.0.0.1:15355" }
$Ready = Join-Path $env:TEMP ("localdns-ready-" + [guid]::NewGuid().ToString("N"))

New-Item -ItemType Directory -Force -Path bin | Out-Null
go build -o bin\localdns.exe ./cmd/localdns
go build -o bin\dnsupstream.exe ./cmd/dnsupstream

$UpstreamProcess = Start-Process `
    -FilePath (Join-Path $Root "bin\dnsupstream.exe") `
    -ArgumentList @("-listen", $Upstream, "-ready-file", $Ready) `
    -PassThru -WindowStyle Hidden

try {
    $readyAddress = $null
    for ($i = 0; $i -lt 50; $i++) {
        if (Test-Path $Ready) {
            $readyAddress = (Get-Content $Ready -Raw).Trim()
            if ($readyAddress) { break }
        }
        if ($UpstreamProcess.HasExited) { throw "mock upstream exited before becoming ready" }
        Start-Sleep -Milliseconds 100
    }
    if (-not $readyAddress) { throw "timed out waiting for mock upstream" }

    Write-Host "mock upstream child: $readyAddress (pid $($UpstreamProcess.Id))"
    Write-Host "try: udp same.demo.test A ; tcp api.external.test A ; tcp portal.external.test A ; stats ; quit"
    & .\bin\localdns.exe `
        -zone samples\zone.json `
        -wire samples\answer.wire `
        -wire-query samples\query.wire `
        -udp $Udp `
        -tcp $Tcp `
        -upstream $readyAddress
}
finally {
    if ($UpstreamProcess -and -not $UpstreamProcess.HasExited) {
        Stop-Process -Id $UpstreamProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $Ready -ErrorAction SilentlyContinue
}

