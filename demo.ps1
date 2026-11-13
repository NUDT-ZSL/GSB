# pysentry 一键演示脚本（Windows PowerShell，全程离线，无第三方库）
# 用法： 在项目根目录执行  ./demo.ps1
# 可选：  ./demo.ps1 -DemoDir D:\some\dir   自定义样例与报告输出目录
param(
    [string]$DemoDir = (Join-Path $env:TEMP "pysentry_demo_run")
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Py = "python"

Write-Host "==> 0. Python 版本" -ForegroundColor Cyan
& $Py --version

Write-Host "`n==> 1. 生成确定性样例到 $DemoDir" -ForegroundColor Cyan
if (Test-Path $DemoDir) { Remove-Item -Recurse -Force $DemoDir }
& $Py -I (Join-Path $Root "generate_samples.py") $DemoDir

Write-Host "`n==> 2. 扫描修复前目录（验收 1/2/3/4/5/6/12）" -ForegroundColor Cyan
& $Py -I (Join-Path $Root "run_scanner.py") scan (Join-Path $DemoDir "before") `
    -o (Join-Path $DemoDir "before_report.json")

Write-Host "`n==> 3. 两次运行字节一致性（验收 6）" -ForegroundColor Cyan
& $Py -I (Join-Path $Root "run_scanner.py") scan (Join-Path $DemoDir "before") `
    -o (Join-Path $DemoDir "run1.json") 2>$null
& $Py -I (Join-Path $Root "run_scanner.py") scan (Join-Path $DemoDir "before") `
    -o (Join-Path $DemoDir "run2.json") 2>$null
$same = (Get-FileHash (Join-Path $DemoDir "run1.json")).Hash -eq
        (Get-FileHash (Join-Path $DemoDir "run2.json")).Hash
Write-Host "两次输出字节一致: $same"

Write-Host "`n==> 4. 空目录 / 不存在目录（验收 7）" -ForegroundColor Cyan
& $Py -I (Join-Path $Root "run_scanner.py") scan (Join-Path $DemoDir "empty") `
    -o (Join-Path $DemoDir "empty_report.json")

Write-Host "`n==> 5. 语法错误文件隔离（验收 8）" -ForegroundColor Cyan
& $Py -I (Join-Path $Root "run_scanner.py") scan (Join-Path $DemoDir "syntax_err") `
    -o (Join-Path $DemoDir "syntax_report.json")

Write-Host "`n==> 6. 修复前后对比（验收 9/10）" -ForegroundColor Cyan
& $Py -I (Join-Path $Root "run_scanner.py") compare `
    (Join-Path $DemoDir "before") (Join-Path $DemoDir "after") `
    -o (Join-Path $DemoDir "diff.json")

Write-Host "`n==> 7. 运行自动化验收测试（21 项）" -ForegroundColor Cyan
& $Py -I (Join-Path $Root "tests\test_acceptance.py")

Write-Host "`n演示完成。报告位于: $DemoDir" -ForegroundColor Green
