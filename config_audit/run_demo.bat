@echo off
REM Windows local demo: no Docker, no network at runtime.
setlocal
python -m config_audit.generate_samples
python -m config_audit.cli audit --inputs samples/node_a.json samples/node_b.json --places 0 --abs-tolerance 5 --relative-ppm 2000 --report runs\report_p0.json --snapshot runs\snap_p0.json --log runs\audit.log
python -m config_audit.cli audit --inputs samples/node_a.json samples/node_b.json --places 3 --abs-tolerance 5 --relative-ppm 2000 --report runs\report_p3.json
python -m config_audit.cli restore --inputs samples/node_a.json samples/node_b.json --snapshot runs\snap_p0.json --report runs\replay.json
python -m config_audit.experiment
endlocal
