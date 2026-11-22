$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" }

& $Python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.11 or newer is required" }

if (-not (Test-Path "$Root\.venv")) {
    & $Python -m venv --system-site-packages "$Root\.venv"
}

$WheelDir = if ($env:WHEEL_DIR) { $env:WHEEL_DIR } else { "$Root\wheels" }
$VenvPython = "$Root\.venv\Scripts\python.exe"
& $VenvPython -c "import numpy, pytest"
$HasSetuptools = $true
try { & $VenvPython -c "import setuptools" } catch { $HasSetuptools = $false }
if ($LASTEXITCODE -ne 0) { $HasSetuptools = $false }
if ($HasSetuptools) {
    if ((Test-Path $WheelDir) -and (Get-ChildItem "$WheelDir\*.whl" -ErrorAction SilentlyContinue)) {
        & $VenvPython -m pip install --no-index --find-links $WheelDir -e "$Root[test]"
    } else {
        & $VenvPython -m pip install --no-index --no-deps --no-build-isolation -e $Root
    }
} else {
    $SiteDir = & $VenvPython -c "import sysconfig; print(sysconfig.get_paths()['purelib'])"
    Set-Content -Path "$SiteDir\ic_ledger_project.pth" -Value $Root -Encoding UTF8
    $Wrapper = "$Root\.venv\Scripts\ic-ledger.cmd"
    if (-not (Test-Path $Wrapper)) {
        Set-Content -Path $Wrapper -Encoding ASCII -Value "@`"$VenvPython`" -m ic_ledger %*"
    }
}
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Write-Output "environment ready: $Root\.venv\Scripts\python.exe"
