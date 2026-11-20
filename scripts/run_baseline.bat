@echo off
rem Reproducible baseline for Windows: generate, run twice, compare hashes.
cd /d "%~dp0\.."
set SEED=20261120

python -m chart_reader generate --out samples --seed %SEED% --count 6
python -m chart_reader run --samples samples --config config\config.example.json --out reports\run_a.json --snapshots snapshots
python -m chart_reader run --samples samples --config config\config.example.json --out reports\run_b.json --snapshots snapshots
python -m chart_reader verify --report reports\run_a.json
python -m chart_reader verify --report reports\run_b.json
fc /b reports\run_a.json reports\run_b.json >nul && echo REPRODUCIBLE: reports byte-identical
