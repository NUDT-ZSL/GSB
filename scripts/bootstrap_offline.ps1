param(
    [string]$WheelHouse = "wheels",
    [string]$Python = "py",
    [string]$PythonVersion = "-3.11"
)

$ErrorActionPreference = "Stop"
if (Test-Path ".venv") {
    Remove-Item ".venv" -Recurse -Force
}
& $Python $PythonVersion -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install --disable-pip-version-check --no-index --find-links $WheelHouse -r requirements.txt
& ".\.venv\Scripts\python.exe" -c "import numpy, PIL; print('numpy', numpy.__version__, 'Pillow', PIL.__version__)"
