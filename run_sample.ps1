# -*- coding: utf-8 -*-
# 项目 67 员工信息录入 —— 一键本地样例（PowerShell，Windows）
# 作用：启动本地模拟表单与状态接口 -> 跑通完整填报 -> 与预期报告对比 -> 停止服务
# 用法：在项目根目录执行  .\run_sample.ps1
# 可选参数：-Port 8765 -Browser chromium
param(
    [int]$Port = 8765,
    [string]$Browser = "chromium",
    [switch]$Headed
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$InputCsv = Join-Path $Root "samples\data\employees.csv"
$OutDir = Join-Path $Root "samples\out"
$Report = Join-Path $OutDir "sample_report.json"
$Expected = Join-Path $Root "samples\expected\expected_summary.json"
$LogDir = Join-Path $Root "samples\out"
New-Item -ItemType Directory -Force $OutDir | Out-Null
$ServerLog = Join-Path $LogDir "mock_server.log"

Write-Host "==> [1/4] 启动本地模拟服务（端口 $Port）..." -ForegroundColor Cyan
$server = Start-Process -FilePath "python" `
    -ArgumentList "-m", "mock_server.server", "--port", "$Port" `
    -WorkingDirectory $Root -RedirectStandardOutput $ServerLog `
    -RedirectStandardError "$ServerLog.err" -PassThru -WindowStyle Hidden

# 等待 /healthz 就绪（最多 15 秒）
$BaseUrl = "http://127.0.0.1:$Port"
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        Invoke-RestMethod -Uri "$BaseUrl/healthz" -TimeoutSec 2 | Out-Null
        $ready = $true
        break
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
if (-not $ready) {
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    throw "模拟服务启动失败，请查看 $ServerLog 与 $ServerLog.err"
}
Write-Host "    服务已就绪: $BaseUrl" -ForegroundColor Green

try {
    Write-Host "==> [2/4] 运行填报工具（CSV -> 表单提交 -> 状态回读 -> 报告）..." -ForegroundColor Cyan
    $cliArgs = @("-m", "employee_importer.cli",
                 "--input", $InputCsv,
                 "--report", $Report,
                 "--url", $BaseUrl,
                 "--browser", $Browser)
    if (-not $Headed) { $cliArgs += @() }  # 默认无头
    if ($Headed) { $cliArgs += "--no-headless" }
    & python @cliArgs
    if ($LASTEXITCODE -ne 0) { throw "填报工具运行失败（退出码 $LASTEXITCODE）" }

    Write-Host "==> [3/4] 报告与预期摘要对比..." -ForegroundColor Cyan
    python tests\compare_sample.py $Report $Expected
    if ($LASTEXITCODE -ne 0) { throw "报告对比未通过" }

    Write-Host "==> [4/4] 完成。报告文件: $Report" -ForegroundColor Green
}
finally {
    Write-Host "==> 停止本地模拟服务..." -ForegroundColor DarkGray
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
}
