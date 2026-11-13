# 一键演示：生成样本 -> 业务处理 -> 11 项验收（Windows PowerShell）
# 用法：在项目根目录执行  .\demo.ps1
$ErrorActionPreference = "Stop"

Write-Host "== [1/3] 生成确定性测试样本 ==" -ForegroundColor Cyan
python -I tests/make_samples.py testdata

Write-Host "`n== [2/3] 执行业务处理 ==" -ForegroundColor Cyan
python -I src/voice_clip_tool.py `
  --wav testdata/voice_mono.wav `
  --config testdata/config_ok.json `
  --out-wav output/demo_merged.wav `
  --out-report output/demo_report.txt
Write-Host "业务处理退出码: $LASTEXITCODE"

Write-Host "`n== [3/3] 运行 11 项验收 ==" -ForegroundColor Cyan
python -I tests/verify.py verify_result.txt
exit $LASTEXITCODE
