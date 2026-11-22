"""Independently re-run cross-stage consistency checks on an existing report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ts_pipeline.consistency import verify_report_consistency
from ts_pipeline.errors import InconsistentError, PipelineError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    path = Path(args.report)
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "business_code": "E_CONFIG_INVALID",
                    "error": {
                        "code": "E_CONFIG_INVALID",
                        "field": "report",
                        "message": str(exc),
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    try:
        result = verify_report_consistency(report)
    except InconsistentError as exc:
        print(
            json.dumps(
                {
                    "business_code": exc.code,
                    "status": "FAIL",
                    "error": exc.to_dict(),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except PipelineError as exc:
        print(
            json.dumps(
                {
                    "business_code": exc.code,
                    "status": "FAIL",
                    "error": exc.to_dict(),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
