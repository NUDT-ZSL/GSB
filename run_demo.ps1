# -*- coding: utf-8 -*-
<#
.SYNOPSIS
  本地文件上传服务安全审计 —— 一键可复现演示脚本（仅使用 127.0.0.1，无容器）。
.DESCRIPTION
  依次执行：
    场景 A：启动“存在漏洞版”服务 -> 审计 -> 预期高危路径遍历 + 中危类型绕过，目录外生成证据文件
    场景 B：关闭服务 -> 审计 -> 预期中文连接错误、退出码 1、不生成 report.json
    场景 C：启动“已修复版”服务 -> 回归审计 -> 预期无高/中危、目录外无新增文件
  结束后停止全部演示服务进程。
.NOTES
  需 Python 3.10+，仅用标准库。建议在项目根目录执行：
    powershell -ExecutionPolicy Bypass -File .\run_demo.ps1
#>

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$RuntimeRoot = Join-Path $ProjectRoot "demo_runtime"
$VulnDir = Join-Path $RuntimeRoot "uploads"
$FixedRoot = Join-Path $ProjectRoot "demo_runtime_fixed"
$FixedDir = Join-Path $FixedRoot "uploads"

function Wait-Health {
    param([string]$Url, [int]$MaxAttempts = 30)
    for ($i = 0; $i -lt $MaxAttempts; $i++) {
        try {
            Invoke-RestMethod -Uri "$Url/health" -TimeoutSec 1 | Out-Null
            return $true
        } catch { Start-Sleep -Milliseconds 300 }
    }
    return $false
}

function Start-Server {
    param([string]$Script, [int]$Port, [string]$UploadDir, [string]$LogName)
    $log = Join-Path $RuntimeRoot $LogName
    $env:UPLOAD_DIR = $UploadDir
    $proc = Start-Process -FilePath "python" `
        -ArgumentList @("-I", $Script) `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput "$log.out" -RedirectStandardError "$log.err" `
        -PassThru -WindowStyle Hidden
    if (-not (Wait-Health -Url "http://127.0.0.1:$Port")) {
        throw "服务 $Script 在 127.0.0.1:$Port 启动超时，请查看日志 $log.err"
    }
    return $proc
}

function Stop-ServerQuiet {
    param($Proc)
    if ($Proc -and -not $Proc.HasExited) {
        Stop-Process -Id $Proc.Id -Force -Confirm:$false
        Write-Host "  已停止服务进程 PID=$($Proc.Id)"
    }
}

New-Item -ItemType Directory -Force -Path $RuntimeRoot, $VulnDir, $FixedRoot, $FixedDir | Out-Null
$vulnProc = $null
$fixedProc = $null

try {
    # ================================================================ 场景 A：漏洞版
    Write-Host "`n========== 场景 A：对存在漏洞版服务执行审计 ==========" -ForegroundColor Cyan
    $vulnProc = Start-Server -Script (Join-Path $ProjectRoot "vuln_server\server_vuln.py") `
        -Port 8000 -UploadDir $VulnDir -LogName "vuln_server"

    $reportA = Join-Path $RuntimeRoot "report_vuln.json"
    if (Test-Path $reportA) { Remove-Item $reportA -Force }
    python -I (Join-Path $ProjectRoot "auditor\auditor.py") `
        --url http://127.0.0.1:8000 --dir $VulnDir --report $reportA
    $rcA = $LASTEXITCODE
    Write-Host "  [A] 退出码=$rcA（0 表示审计流程成功完成）"
    $outsideA = Get-ChildItem $RuntimeRoot -Filter "auditscan_*" -File
    Write-Host "  [A] 上传目录外的越界证据文件数: $($outsideA.Count)（预期 3）" -ForegroundColor Yellow
    $insideA = Get-ChildItem $VulnDir -Filter "auditscan_*" -File
    Write-Host "  [A] 上传目录内残留测试文件数: $($insideA.Count)（预期 0，已自动清理）" -ForegroundColor Yellow

    Stop-ServerQuiet $vulnProc
    $vulnProc = $null

    # ================================================================ 场景 B：服务不可达
    Write-Host "`n========== 场景 B：服务关闭后执行审计（错误处理验证） ==========" -ForegroundColor Cyan
    $reportB = Join-Path $RuntimeRoot "report_unreachable.json"
    if (Test-Path $reportB) { Remove-Item $reportB -Force }
    python -I (Join-Path $ProjectRoot "auditor\auditor.py") `
        --url http://127.0.0.1:8000 --dir $VulnDir --report $reportB
    $rcB = $LASTEXITCODE
    Write-Host "  [B] 退出码=$rcB（预期 1），报告是否存在: $(Test-Path $reportB)（预期 False）" -ForegroundColor Yellow

    # ================================================================ 场景 C：修复版回归
    Write-Host "`n========== 场景 C：对已修复版服务执行回归审计 ==========" -ForegroundColor Cyan
    $fixedProc = Start-Server -Script (Join-Path $ProjectRoot "fixed_server\server_fixed.py") `
        -Port 8001 -UploadDir $FixedDir -LogName "fixed_server"

    $reportC = Join-Path $FixedRoot "report_fixed.json"
    if (Test-Path $reportC) { Remove-Item $reportC -Force }
    python -I (Join-Path $ProjectRoot "auditor\auditor.py") `
        --url http://127.0.0.1:8001 --dir $FixedDir --report $reportC
    $rcC = $LASTEXITCODE
    Write-Host "  [C] 退出码=$rcC（0 表示审计流程成功完成）"
    $outsideC = Get-ChildItem $FixedRoot -Filter "auditscan_*" -File
    Write-Host "  [C] 上传目录外新增文件数: $($outsideC.Count)（预期 0）" -ForegroundColor Yellow
    $insideC = Get-ChildItem $FixedDir -Filter "auditscan_*" -File
    Write-Host "  [C] 上传目录内残留测试文件数: $($insideC.Count)（预期 0）" -ForegroundColor Yellow

    Write-Host "`n========== 演示结论 ==========" -ForegroundColor Green
    Write-Host "漏洞版报告: $reportA"
    Write-Host "修复版报告: $reportC"
    Write-Host "可用以下命令对比高/中危条目数量："
    Write-Host "  python -c `"import json;r=json.load(open(r'$reportA',encoding='utf-8'));print('漏洞版:',r['summary'])`""
    Write-Host "  python -c `"import json;r=json.load(open(r'$reportC',encoding='utf-8'));print('修复版:',r['summary'])`""
}
finally {
    Stop-ServerQuiet $vulnProc
    Stop-ServerQuiet $fixedProc
    Write-Host "`n演示服务进程已全部清理。"
}
