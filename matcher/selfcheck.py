"""Stage 8: cross-module consistency self-check."""
from .errors import BusinessError, E_INCONSISTENT
from .migrate import status_multiset


def selfcheck(v1_rows, v2_rows, replay_report):
    """Verify v1/v2 row parity, per-image TP/FP/FN derivability, replay."""
    problems = []
    if len(v1_rows) != len(v2_rows):
        problems.append(
            "row count differs: v1=%d v2=%d" % (len(v1_rows), len(v2_rows))
        )
    if status_multiset(v1_rows) != status_multiset(v2_rows):
        problems.append("per-image TP/FP/FN sets differ between v1 and v2")
    if replay_report.get("rows") != len(v1_rows):
        problems.append(
            "replay covered %d rows but v1 holds %d"
            % (replay_report.get("rows", -1), len(v1_rows))
        )
    if problems:
        raise BusinessError(
            E_INCONSISTENT,
            "; ".join(problems),
            field="consistency",
            context={"problems": problems},
        )
    return {
        "row_count": len(v1_rows),
        "images": replay_report.get("images", 0),
        "status": "consistent",
    }
