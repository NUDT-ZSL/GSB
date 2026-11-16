# CDN edge-cache consistency workbench: repeatable demo (Windows).
# Prereq: Python 3.11 from python.org (Tkinter included), no other program
# occupying loopback high ports used by the demo.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
python -m cdnlab.demo
