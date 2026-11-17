# HDR Colour Conversion Component

This workspace contains a self-contained Python implementation of deterministic
SDR/HLG/PQ conversion, gamut/white-point mapping, HDR tone mapping, statistics,
error injection and recoverable atomic output.

Runtime: Python 3.11+ standard library only. NumPy is not required by the
implementation or tests, and no network or external media is used.

## Quick Start

```bash
python generate_samples.py --output samples
python -m hdr_color samples/task_pq-bt2020-to-sdr-bt709.json
python -m unittest discover -v
```

Windows launcher:

```bat
python run_task.py samples\task_pq-bt2020-to-sdr-bt709.json
```

Outputs are written under each task's resolved `output_dir`:

- `converted_frames.json`: final target linear-light RGB in cd/m2 plus target
  transfer-encoded RGB.
- `statistics_report.json`: frame/window statistics, curve constants,
  chromaticity-derived matrices, mapping diagnostics and injection records.
- `run-state.json`: running, interrupted, failed or complete state.

Detailed semantics and schemas are in `docs/TECHNICAL.md`. Actual local results
are recorded in `docs/VALIDATION.md`.
