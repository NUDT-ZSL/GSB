# 一键演示：生成确定性清单 -> 正常核对 -> 异常场景（坏清单/只读目录）
# 用法：在项目根目录执行  powershell -ExecutionPolicy Bypass -File run_demo.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"

Write-Host "== 1. 生成确定性业务样例 ==" -ForegroundColor Cyan
& $py scripts\generate_sample.py samples\manifest.json

Write-Host "`n== 2. 正常核对（输出到 output_demo，不存在则创建） ==" -ForegroundColor Cyan
& $py -m subtitle_audit samples\manifest.json output_demo

Write-Host "`n== 3. 异常：清单不存在（预期退出码 2，无输出） ==" -ForegroundColor Cyan
& $py -m subtitle_audit samples\not_exist.json output_demo2
Write-Host "退出码 = $LASTEXITCODE（预期非零），output_demo2 是否存在：$(Test-Path output_demo2)"

Write-Host "`n== 4. 异常：非法 JSON（预期退出码 2） ==" -ForegroundColor Cyan
$bad = Join-Path $env:TEMP "broken_manifest.json"
Set-Content -Path $bad -Value '{ sheets: [,, ' -Encoding utf8
& $py -m subtitle_audit $bad output_demo3
Write-Host "退出码 = $LASTEXITCODE（预期非零），output_demo3 是否存在：$(Test-Path output_demo3)"
Remove-Item $bad -Force

Write-Host "`n== 5. 异常：只读输出目录（预期退出码 3） ==" -ForegroundColor Cyan
$ro = Join-Path $env:TEMP "subtitle_ro_demo"
if (Test-Path $ro) { Remove-Item -Recurse -Force $ro }
New-Item -ItemType Directory -Path $ro | Out-Null
$me = (whoami).Trim()
icacls $ro /deny "$me`:(WD)" | Out-Null
& $py -m subtitle_audit samples\manifest.json $ro
Write-Host "退出码 = $LASTEXITCODE（预期非零）"
icacls $ro /remove:d $me | Out-Null
Remove-Item -Recurse -Force $ro

Write-Host "`n演示结束。正常产物位于 output_demo\" -ForegroundColor Green
