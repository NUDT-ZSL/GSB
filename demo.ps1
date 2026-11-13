<#
.SYNOPSIS
  本地订单查询接口 SQL 注入自查 —— 一键可复现演示（Windows PowerShell）。
.DESCRIPTION
  纯标准库、仅回环、确定性样本。脚本依次演示任务 14 条验收：
  环境生成 -> 自检 -> 配置拒绝 -> 可达性失败 -> 缺陷检测/确认 ->
  回显不误报 -> 修复对照 -> 部分修复残留 -> 故障续扫 -> 字节级复现。
  无需公网、无真实凭据、不使用 Docker。
.NOTES
  在项目根目录（本文件所在目录）运行： powershell -ExecutionPolicy Bypass -File .\demo.ps1
#>

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$env:PYTHONPATH = $Root
$py = "python"
$ws = Join-Path $Root "lab_ws"
$procs = @()

function Wait-Port([int]$Port, [int]$Seconds = 10) {
    & $py -c @"
import socket, sys, time
deadline = time.time() + $Seconds
while time.time() < deadline:
    s = socket.socket()
    try:
        s.connect(('127.0.0.1', $Port)); s.close(); sys.exit(0)
    except OSError:
        s.close(); time.sleep(0.2)
sys.exit('port $Port not ready')
"@
}

function Start-LabServer([string]$Mode, [int]$Port, [string]$FailAfter = "") {
    if ($FailAfter -ne "") { $env:SQLI_AUDIT_FAIL_AFTER = $FailAfter }
    else { Remove-Item Env:SQLI_AUDIT_FAIL_AFTER -ErrorAction SilentlyContinue }
    $p = Start-Process -FilePath $py -ArgumentList "lab_ws\server_$Mode.py" `
        -WorkingDirectory $Root -PassThru -WindowStyle Hidden
    $script:procs += $p
    Wait-Port $Port
    Write-Host "  [启动] mode=$Mode port=$Port pid=$($p.Id)"
}

function Stop-LabServers {
    Remove-Item Env:SQLI_AUDIT_FAIL_AFTER -ErrorAction SilentlyContinue
    foreach ($p in $script:procs) {
        if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    }
    $script:procs = @()
}

function Stop-OneServer([int]$Index) {
    $p = $script:procs[$Index]
    if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
}

function Section([string]$Title) {
    Write-Host "`n========== $Title ==========" -ForegroundColor Cyan
}

try {
    Section "0. 重新生成确定性实验环境（清理旧报告/状态）"
    if (Test-Path "$ws\reports") { Remove-Item "$ws\reports" -Recurse -Force }
    if (Test-Path "$ws\state") { Remove-Item "$ws\state" -Recurse -Force }
    & $py -m sqli_audit setup $ws | Out-Null
    Write-Host "  实验环境: $ws"

    Section "14. 内置自检（期望 42/42 通过，退出码 0）"
    & $py -m sqli_audit selfcheck
    if ($LASTEXITCODE -ne 0) { throw "自检未通过" }

    Section "1. 缺字段配置（期望退出码 1，列出缺失字段，无请求发出）"
    $bad = Join-Path $ws "bad_missing.json"
    '{"base_url":"http://127.0.0.1:8031","endpoint_path":"/orders","method":"GET","baseline":{"order_no":"ORD-1001"}}' |
        Set-Content -Path $bad -Encoding utf8
    & $py -m sqli_audit scan $bad
    Write-Host "  实际退出码=$LASTEXITCODE（期望 1）"

    Section "2. 服务未启动时扫描（期望可达性失败，退出码 2）"
    & $py -m sqli_audit scan "$ws\target_vulnerable.json"
    Write-Host "  实际退出码=$LASTEXITCODE（期望 2）"

    Section "3/5/6. 启动缺陷接口并扫描（可疑定位 + 已确认 + 证据）"
    Start-LabServer "vulnerable" 8031
    & $py -m sqli_audit scan "$ws\target_vulnerable.json"
    Copy-Item "$ws\reports\target_vulnerable_report.json" "$ws\reports\run1.json"
    & $py -m sqli_audit scan "$ws\target_vulnerable.json" | Out-Null
    Copy-Item "$ws\reports\target_vulnerable_report.json" "$ws\reports\run2.json"
    $h1 = (Get-FileHash "$ws\reports\run1.json").Hash
    $h2 = (Get-FileHash "$ws\reports\run2.json").Hash
    Write-Host ("  [验收6] 两次报告逐字节一致: " + ($h1 -eq $h2))

    Section "4. 纯回显正常接口（期望零可疑）"
    Start-LabServer "echo" 8034
    & $py -m sqli_audit scan "$ws\target_echo.json"

    Section "7. 参数化修复版本 + 修复前后对照（期望全部 fixed）"
    Start-LabServer "fixed" 8032
    & $py -m sqli_audit compare "$ws\target_fixed.json" `
        --before "$ws\reports\target_vulnerable_report.json" `
        --output "$ws\reports\comparison_fixed.json"

    Section "8. 部分修复版本（期望 order_no 残留，单独列出）"
    Start-LabServer "partial" 8033
    & $py -m sqli_audit compare "$ws\target_partial.json" `
        --before "$ws\reports\target_vulnerable_report.json" `
        --output "$ws\reports\comparison_partial.json"

    Section "9/11. 故障门控：前 5 次请求后断连（期望不中断 + 未完成项 + 覆盖不全提示）"
    Start-LabServer "flaky" 8035 -FailAfter "5"
    & $py -m sqli_audit scan "$ws\target_flaky.json" --state "$ws\state\demo_flaky_state.json"

    Section "10. 恢复服务后同状态文件重跑（期望补齐、不重复计数、已完成结论不变）"
    # 只重启故障的 flaky（第 5 个启动，索引 4），其余服务保持运行
    Stop-OneServer 4
    Start-LabServer "flaky" 8035
    & $py -m sqli_audit scan "$ws\target_flaky.json" --state "$ws\state\demo_flaky_state.json"

    Section "12/13. 时间占位与工作目录无关性"
    $flakyRep = Get-Content "$ws\reports\target_flaky_report.json" -Raw -Encoding utf8 | ConvertFrom-Json
    Write-Host ("  generated_at = " + $flakyRep.generated_at + " | elapsed = " + $flakyRep.elapsed)
    New-Item -ItemType Directory -Force -Path "$ws\cwd_a", "$ws\cwd_b" | Out-Null
    Push-Location "$ws\cwd_a"
    & $py -m sqli_audit scan "$ws\target_vulnerable.json" --report "$ws\reports\cwd_a.json" --state "$ws\state\cwd_a.json" | Out-Null
    Pop-Location
    Push-Location "$ws\cwd_b"
    & $py -m sqli_audit scan "$ws\target_vulnerable.json" --report "$ws\reports\cwd_b.json" --state "$ws\state\cwd_b.json" | Out-Null
    Pop-Location
    $ha = (Get-FileHash "$ws\reports\cwd_a.json").Hash
    $hb = (Get-FileHash "$ws\reports\cwd_b.json").Hash
    Write-Host ("  [验收13] 不同工作目录报告一致: " + ($ha -eq $hb))

    Write-Host "`n演示完成。报告目录: $ws\reports" -ForegroundColor Green
}
finally {
    Stop-LabServers
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    Remove-Item Env:SQLI_AUDIT_FAIL_AFTER -ErrorAction SilentlyContinue
}
