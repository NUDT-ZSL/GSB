from __future__ import annotations

import json
import sys

from app.verifier import verify_all


def main() -> int:
    report = verify_all()
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "request_count": report["request_count"],
                "score_mismatch_count": report["score_mismatch_count"],
                "breakdown_mismatch_count": report["breakdown_mismatch_count"],
                "injury_basis_mismatch_count": report[
                    "injury_basis_mismatch_count"
                ],
                "invariance": {
                    "case_count": report["invariance"]["case_count"],
                    "failure_count": report["invariance"]["failure_count"],
                },
                "first_rank_deviation": report["first_rank_deviation"],
                "checks": report["checks"],
                "consistency_overall": report["consistency"]["overall"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
