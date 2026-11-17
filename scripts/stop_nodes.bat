@echo off
setlocal
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Continue'; Get-ChildItem -LiteralPath 'run' -Filter 'node-*.json' -ErrorAction SilentlyContinue | ForEach-Object { try { $info = Get-Content -Raw -LiteralPath $_.FullName | ConvertFrom-Json; $process = Get-Process -Id ([int]$info.pid) -ErrorAction SilentlyContinue; if ($null -ne $process) { Stop-Process -Id $process.Id -Force; Write-Host ('stopped ' + $info.node_id + ' pid=' + $info.pid) } else { Write-Host ('not-running ' + $info.node_id + ' pid=' + $info.pid) }; Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue } catch { Write-Warning $_ } }"
endlocal
