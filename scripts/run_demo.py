"""One-shot deterministic demo: generate samples, replay, verify, report.

Runs fully offline with only NumPy + Pillow. Exit code is always 0 for
handled business errors (they are recorded in the failure list); an unexpected
programming error propagates with a non-zero code.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ieditor.cli import run_replay_batch  # noqa: E402
from ieditor.config import (load_quant_rules, load_render_config,  # noqa: E402
                            load_version_map)


def main() -> int:
    config_dir = ROOT / "config"
    config = load_render_config(config_dir / "render_config.json")
    version_map = load_version_map(config_dir / "version_map.json")
    quant_rules = load_quant_rules(config_dir / "quant_rules.json")

    work_run = ROOT / "workspace_demo" / "workspace_run"
    work_replay = ROOT / "workspace_demo" / "workspace_replay"
    shutil.rmtree(work_run, ignore_errors=True)
    shutil.rmtree(work_replay, ignore_errors=True)

    sequence = config_dir / "replay_sequence.json"
    first = run_replay_batch(ROOT, sequence, work_run, config_dir, False)
    second = run_replay_batch(ROOT, sequence, work_replay, config_dir, False)

    reproducible = (first["rendered_fingerprints"] ==
                    second["rendered_fingerprints"])
    report_equal = ((work_run / "diagnose_a.json").read_bytes() ==
                    (work_replay / "diagnose_a.json").read_bytes())

    verification = {
        "business_code": "E_OK",
        "replay_pixel_reproducible": reproducible,
        "diagnose_report_reproducible": report_equal,
        "rendered_fingerprints": first["rendered_fingerprints"],
        "failure_count": len(first["failures"]),
        "failure_codes": sorted({f["code"] for f in first["failures"]}),
        "failures": first["failures"],
    }
    out = ROOT / "workspace_demo" / "verification.json"
    out.write_text(json.dumps(verification, ensure_ascii=False,
                              indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(json.dumps(verification, ensure_ascii=False, indent=2,
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
