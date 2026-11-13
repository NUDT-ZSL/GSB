# 一键演示：生成确定性样本 -> 运行两条清单 -> 运行完整验收
# 用法（PowerShell）: ./run_demo.ps1
$ErrorActionPreference = "Stop"
# 保证中文标题在 GBK 控制台下不乱码
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$py = "python"

Write-Host "== 1. 生成确定性测试样本与清单 ==" -ForegroundColor Cyan
& $py -I make_samples.py --workdir work

Write-Host "`n== 2. 运行主清单（含缺失/非PCM/位深/声道/跳过等分支）==" -ForegroundColor Cyan
& $py -I process.py `
  --manifest work/samples/edit_list.json `
  --output   work/output/dialogue_main.wav `
  --report   work/output/report_main.json `
  --log      work/output/run_main.log

Write-Host "`n== 3. 运行间隔清单（两个0.5s片段 + 200ms静音 = 1.2s）==" -ForegroundColor Cyan
& $py -I process.py `
  --manifest work/samples/gap_list.json `
  --output   work/output/dialogue_gap.wav `
  --report   work/output/report_gap.json `
  --log      work/output/run_gap.log

Write-Host "`n== 4. 运行 29 项自动化验收 ==" -ForegroundColor Cyan
& $py -I verify.py
exit $LASTEXITCODE
