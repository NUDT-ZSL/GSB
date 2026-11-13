# 一键演示脚本（Windows PowerShell）
# 用法： 在项目根目录执行  pwsh -File demo.ps1
# 作用： 启动漏洞版/修复版本地靶场，运行扫描器覆盖全部验收场景，并打印退出码。
# 约束： 所有服务仅监听 127.0.0.1，扫描器也只允许访问本机地址，全程无外部网络访问。

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = "python"
$Out = Join-Path $Root "demo_output"
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$env:PYTHONIOENCODING = "utf-8"

function Start-Lab($Mode, $Port, $Extra = @(), $WaitReady = $true) {
    $args = @("-I", (Join-Path $Root "lab\vuln_server.py"),
              "--mode", $Mode, "--port", "$Port", "--host", "127.0.0.1") + $Extra
    $p = Start-Process $Py -ArgumentList $args -PassThru -WindowStyle Hidden
    # 延迟启动的靶场不等待就绪（用于演示 --delay 扫描等待能力）
    if ($WaitReady) {
        for ($i = 0; $i -lt 50; $i++) {
            try { (New-Object Net.Sockets.TcpClient).Connect("127.0.0.1", $Port); break }
            catch { Start-Sleep -Milliseconds 200 }
        }
    }
    return $p
}

Write-Host "=== 1. 启动漏洞版(8021) 与修复版(8022) 靶场 ===" -ForegroundColor Cyan
$vuln = Start-Lab "vulnerable" 8021
$fixed = Start-Lab "fixed" 8022

try {
    Write-Host "`n=== 2. 对漏洞服务扫描（预期退出码 1） ===" -ForegroundColor Cyan
    & $Py -I (Join-Path $Root "local_injection_scanner\cli.py") `
        --target http://127.0.0.1:8021 `
        --report (Join-Path $Out "vuln_report.json") `
        --params username,item --log (Join-Path $Out "vuln_scan.log")
    Write-Host "退出码 = $LASTEXITCODE" -ForegroundColor Yellow

    Write-Host "`n=== 3. 对修复版服务扫描（预期退出码 0，无缺陷） ===" -ForegroundColor Cyan
    & $Py -I (Join-Path $Root "local_injection_scanner\cli.py") `
        --target http://127.0.0.1:8022 `
        --report (Join-Path $Out "fixed_report.json") --params username,item
    Write-Host "退出码 = $LASTEXITCODE" -ForegroundColor Yellow

    Write-Host "`n=== 4. 服务持续不可达（预期退出码 3，无安全判定） ===" -ForegroundColor Cyan
    & $Py -I (Join-Path $Root "local_injection_scanner\cli.py") `
        --target http://127.0.0.1:59999 `
        --report (Join-Path $Out "dead_report.json") --params username,item
    Write-Host "退出码 = $LASTEXITCODE" -ForegroundColor Yellow

    Write-Host "`n=== 5. 非本机目标被拒绝（预期退出码 2，提示仅支持本地目标） ===" -ForegroundColor Cyan
    & $Py -I (Join-Path $Root "local_injection_scanner\cli.py") `
        --target http://8.8.8.8:8000 --report (Join-Path $Out "x.json")
    Write-Host "退出码 = $LASTEXITCODE" -ForegroundColor Yellow

    Write-Host "`n=== 6. 延迟探测：靶场 8 秒后才监听，--delay 20 等待后完成 ===" -ForegroundColor Cyan
    $late = Start-Lab "vulnerable" 8023 @("--start-delay", "8") -WaitReady $false
    try {
        Start-Sleep -Milliseconds 300
        try { (New-Object Net.Sockets.TcpClient).Connect("127.0.0.1", 8023)
              Write-Host "（端口已开放）" }
        catch { Write-Host "扫描前端口确认关闭，扫描器将轮询等待..." -ForegroundColor DarkGray }
        $t0 = Get-Date
        & $Py -I (Join-Path $Root "local_injection_scanner\cli.py") `
            --target http://127.0.0.1:8023 `
            --report (Join-Path $Out "delay_report.json") `
            --params username --delay 20 --log (Join-Path $Out "delay_scan.log")
        $code = $LASTEXITCODE
        Write-Host ("退出码 = {0}，实际等待 {1:N1} 秒" -f $code, ((Get-Date)-$t0).TotalSeconds) -ForegroundColor Yellow
    } finally { Stop-Process -Id $late.Id -Force -ErrorAction SilentlyContinue }

    Write-Host "`n全部演示完成，报告与日志位于: $Out" -ForegroundColor Green
}
finally {
    Stop-Process -Id $vuln.Id -Force -ErrorAction SilentlyContinue
    Stop-Process -Id $fixed.Id -Force -ErrorAction SilentlyContinue
}
