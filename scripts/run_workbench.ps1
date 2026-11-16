# Launch the Tkinter diagnostic workbench against the latest demo session.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
python -m cdnlab.demo            # produce a fresh session first
python -m cdnlab.workbench --db artifacts\demo\scenario4_restore_replay.sqlite
