<#
.SYNOPSIS
  本机反射型脚本注入探测工具 —— 一键演示（PowerShell，仅标准库）。

.DESCRIPTION
  1. 在 127.0.0.1 上启动脆弱版(8033)与修复版(8034)演示站点；
  2. 确定性生成探测用例清单与历史报告样本；
  3. 对脆弱版扫描两次（验证结论稳定）、对修复版复核、演示外部地址拒绝；
  4. 输出 JSON 报告并按退出码给出结论；
  5. 结束后自动停止演示站点。

  全程只访问本机回环地址，不访问任何外部目标。
#>

param(
  [int]$VulnPort = 8033,
  [int]$FixedPort = 8034
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Wait-Ready([string]$url) {
  for ($i = 0; $i -lt 30; $i++) {
    try { Invoke-WebRequest -UseBasicParsing $url -TimeoutSec 1 | Out-Null; return }
    catch { Start-Sleep -Milliseconds 300 }
  }
  throw "服务未就绪: $url"
}

$vuln = Start-Process -FilePath python -ArgumentList `
  "demo_site/server.py", "--port", $VulnPort, "--mode", "vulnerable" `
  -WorkingDirectory $Root -PassThru -WindowStyle Hidden
$fixed = Start-Process -FilePath python -ArgumentList `
  "demo_site/server.py", "--port", $FixedPort, "--mode", "fixed" `
  -WorkingDirectory $Root -PassThru -WindowStyle Hidden

try {
  Wait-Ready "http://127.0.0.1:$VulnPort/safe?q=ready"
  Wait-Ready "http://127.0.0.1:$FixedPort/safe?q=ready"
  New-Item -ItemType Directory -Force samples, reports | Out-Null

  Write-Host "`n[1/5] 生成确定性样本" -ForegroundColor Cyan
  python -m xss_probe gen-cases  --output samples/cases.json
  python -m xss_probe gen-history --output samples/history_report.json

  Write-Host "`n[2/5] 脆弱版站点扫描（存在风险时退出码应为 1）" -ForegroundColor Cyan
  python -m xss_probe scan --base-url "http://127.0.0.1:$VulnPort" `
    --cases samples/cases.json --report reports/vuln_run1.json `
    --fix-status "vulnerable-2026-11-14" --history samples/history_report.json
  Write-Host "脆弱版退出码 = $LASTEXITCODE（预期 1）"

  Write-Host "`n[3/5] 重复扫描验证稳定性" -ForegroundColor Cyan
  python -m xss_probe scan --base-url "http://127.0.0.1:$VulnPort" `
    --cases samples/cases.json --report reports/vuln_run2.json `
    --fix-status "vulnerable-2026-11-14" --history samples/history_report.json | Out-Null
  python -c "import json;a=json.load(open('reports/vuln_run1.json',encoding='utf-8'));b=json.load(open('reports/vuln_run2.json',encoding='utf-8'));print('两次报告完全一致:', json.dumps(a,ensure_ascii=False,sort_keys=True)==json.dumps(b,ensure_ascii=False,sort_keys=True))"

  Write-Host "`n[4/5] 修复版站点复核（无风险时退出码应为 0）" -ForegroundColor Cyan
  python -m xss_probe scan --base-url "http://127.0.0.1:$FixedPort" `
    --cases samples/cases.json --report reports/fixed_run.json `
    --fix-status "fixed-2026-11-14" --history samples/history_report.json
  Write-Host "修复版退出码 = $LASTEXITCODE（预期 0）"

  Write-Host "`n[5/5] 外部地址拒绝演示（退出码应为 3）" -ForegroundColor Cyan
  python -m xss_probe scan --base-url "http://example.com/" `
    --cases samples/cases.json --report reports/blocked.json `
    --fix-status blocked-test
  Write-Host "外部地址退出码 = $LASTEXITCODE（预期 3，且不生成报告）"
}
finally {
  Stop-Process -Id $vuln.Id, $fixed.Id -Force -ErrorAction SilentlyContinue
  Write-Host "`n演示站点已停止。报告目录: $Root\reports" -ForegroundColor Green
}
