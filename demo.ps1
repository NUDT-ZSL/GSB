# 一键本地演示脚本（PowerShell，Windows）。
# 全程离线：仅生成本地确定性样本、扫描、修复后复扫、比对、哈希校验。
# 用法：在仓库根目录执行  ./demo.ps1

$ErrorActionPreference = "Stop"

$work = "demo_workdir"
$out = "demo_output"

if (Test-Path $work) { Remove-Item -Recurse -Force $work }
if (Test-Path $out) { Remove-Item -Recurse -Force $out }

Write-Host "==> [1/5] 确定性生成样本、损坏样本与清单" -ForegroundColor Cyan
python -m scanner.cli generate $work

Write-Host "`n==> [2/5] 首次扫描（修复前）" -ForegroundColor Cyan
python -m scanner.cli scan $work -o "$out/report_run1.json"

Write-Host "`n==> [3/5] 同输入再次扫描，验证逐字节一致" -ForegroundColor Cyan
python -m scanner.cli scan $work -o "$out/report_run2.json"
$h1 = (Get-FileHash "$out/report_run1.json" -Algorithm SHA256).Hash
$h2 = (Get-FileHash "$out/report_run2.json" -Algorithm SHA256).Hash
if ($h1 -eq $h2) {
    Write-Host "确定性校验通过：两次输出 SHA256 均为 $h1" -ForegroundColor Green
} else {
    throw "确定性校验失败：两次输出哈希不一致"
}

Write-Host "`n==> [4/5] 对声称已修复的样本目录复扫" -ForegroundColor Cyan
python -m scanner.cli scan "$work/fixed_samples" -o "$out/report_fixed.json"

Write-Host "`n==> [5/5] 修复前后比对，确认暴露消除" -ForegroundColor Cyan
python -m scanner.cli compare "$out/report_run1.json" "$out/report_fixed.json" -o "$out/comparison.json"

Write-Host "`n演示完成，产物位于 $out ：" -ForegroundColor Yellow
Get-ChildItem $out | Select-Object Name, Length
