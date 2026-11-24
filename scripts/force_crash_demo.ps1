# 强制终止（模拟进程被 kill / 崩溃）与恢复演示
# 强制终止使用隐藏参数 --force-crash-after N：在原子持久化 N 个分块后以退出码 137 硬退出，
# 不写中断标记、不写报告；重启后仅依据已持久化且摘要有效的分块恢复。

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$SampleDir  = Join-Path $Root "samples\fixed"
$BuildRoot  = Join-Path $Root "build_crash_ps"
$StateDir   = Join-Path $BuildRoot "state"
$OutputDir  = Join-Path $BuildRoot "output"
$ArchiveDir = Join-Path $BuildRoot "archive"

if (Test-Path $BuildRoot) { Remove-Item -Recurse -Force $BuildRoot }
New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null

Write-Host "== 强制终止：持久化 2 块后硬退出（预期退出码 137） =="
python -m matgen.cli run `
  --materials (Join-Path $SampleDir "materials.json") `
  --config (Join-Path $SampleDir "config.json") `
  --state-dir $StateDir --output-dir $OutputDir --archive-dir $ArchiveDir `
  --concurrency 1 --force-crash-after 2
Write-Host "observed exit code = $LASTEXITCODE"

Write-Host "== 崩溃后状态：应只有两个原子块，无报告、无 .tmp 残留 =="
Get-ChildItem -Recurse $StateDir -Filter "chunk_*.npz" | Select-Object -ExpandProperty FullName

Write-Host "== 重启恢复 =="
python -m matgen.cli recover `
  --materials (Join-Path $SampleDir "materials.json") `
  --config (Join-Path $SampleDir "config.json") `
  --state-dir $StateDir --output-dir $OutputDir --archive-dir $ArchiveDir
Write-Host "恢复完成，报告: $OutputDir\run_report.json"
