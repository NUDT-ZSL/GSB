# matgen Windows PowerShell 演示脚本
# 用法（在项目根目录）: powershell -ExecutionPolicy Bypass -File .\scripts\demo.ps1
# 前提: python --version 显示 3.11+，且已 pip install -r requirements.txt

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$SampleDir   = Join-Path $Root "samples\fixed"
$BuildRoot   = Join-Path $Root "build_demo_ps"
$StateDir    = Join-Path $BuildRoot "state"
$OutputDir   = Join-Path $BuildRoot "output"
$ArchiveDir  = Join-Path $BuildRoot "archive"
$StopFlag    = Join-Path $BuildRoot "stop.flag"

function Reset-Path($Path) { if (Test-Path $Path) { Remove-Item -Recurse -Force $Path } }

Write-Host "== 0. 环境检查 =="
python --version
python -c "import numpy; print('numpy', numpy.__version__)"

Write-Host "== 1. 清理并重建可独立清理的目录 =="
Reset-Path $BuildRoot
New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null

Write-Host "== 2. 生成固定种子样本 =="
python -m matgen.cli make-samples --sample-dir $SampleDir

Write-Host "== 3. 仅校验（无注入，应全部合法） =="
python -m matgen.cli validate --materials (Join-Path $SampleDir "materials.json") --config (Join-Path $SampleDir "config.json")

Write-Host "== 4. 异常注入驱动生成（3 个材质被拒，fabric 继续生成） =="
python -m matgen.cli run `
  --materials (Join-Path $SampleDir "materials.json") `
  --config (Join-Path $SampleDir "config.json") `
  --injections (Join-Path $SampleDir "injections.json") `
  --state-dir $StateDir --output-dir $OutputDir --archive-dir $ArchiveDir

Write-Host "== 5. 可处理终止：预置停止标记后运行，预期退出码 10 =="
Reset-Path $StateDir; Reset-Path $OutputDir
Set-Content -Path $StopFlag -Value "stop"
python -m matgen.cli run `
  --materials (Join-Path $SampleDir "materials.json") `
  --config (Join-Path $SampleDir "config.json") `
  --state-dir $StateDir --output-dir $OutputDir --archive-dir $ArchiveDir `
  --concurrency 1 --stop-flag $StopFlag
Write-Host "exit code = $LASTEXITCODE"
Remove-Item -Force $StopFlag

Write-Host "== 6. 恢复：版本一致则复用已完成块 =="
python -m matgen.cli recover `
  --materials (Join-Path $SampleDir "materials.json") `
  --config (Join-Path $SampleDir "config.json") `
  --state-dir $StateDir --output-dir $OutputDir --archive-dir $ArchiveDir

Write-Host "== 7. 损坏已持久化块摘要，预期 E_CHUNK_DIGEST_MISMATCH 并整体重做 =="
python -m matgen.cli corrupt-chunk --state-dir $StateDir --material-id mat_brick --chunk-index 0
python -m matgen.cli run `
  --materials (Join-Path $SampleDir "materials.json") `
  --config (Join-Path $SampleDir "config.json") `
  --state-dir $StateDir --output-dir $OutputDir --archive-dir $ArchiveDir

Write-Host "演示完成。报告位于: $OutputDir\run_report.json"
