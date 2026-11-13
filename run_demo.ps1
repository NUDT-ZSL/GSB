# 一键演示：生成确定性样本 -> 剪辑 -> 展示产物与报告（Windows PowerShell 5.1+）
# 用法：./run_demo.ps1
# 注意：本脚本含中文输出，保存为 UTF-8；以下两行把控制台切换到 UTF-8 代码页。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$OutputEncoding = [System.Text.Encoding]::UTF8

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$sampleDir = Join-Path $root "samples"
$outDir = Join-Path $root "output\demo"

Remove-Item -Recurse -Force $sampleDir -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $outDir -ErrorAction SilentlyContinue

Write-Host "== 1. 生成确定性样本 ==" -ForegroundColor Cyan
python -X utf8 tools/generate_samples.py --dir $sampleDir
if ($LASTEXITCODE -ne 0) { throw "样本生成失败，退出码 $LASTEXITCODE" }

Write-Host ""
Write-Host "== 2. 执行剪辑（跳过警告走 stderr，属预期输出）==" -ForegroundColor Cyan
# 2>&1 把原生命令 stderr 合并进当前流，避免 PS 5.1 将其包装成 NativeCommandError
python -X utf8 -m wav_splitter --wav (Join-Path $sampleDir "sample.wav") `
    --csv (Join-Path $sampleDir "marks.csv") --output $outDir 2>&1 |
    ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0) { throw "剪辑失败，退出码 $LASTEXITCODE" }

Write-Host ""
Write-Host "== 3. 输出目录产物 ==" -ForegroundColor Cyan
Get-ChildItem $outDir | Select-Object Name, Length

Write-Host ""
Write-Host "== 4. JSON 报告 ==" -ForegroundColor Cyan
Get-Content (Join-Path $outDir "report.json") -Encoding utf8

Write-Host ""
Write-Host "== 5. 运行验收测试 ==" -ForegroundColor Cyan
python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "测试失败，退出码 $LASTEXITCODE" }
