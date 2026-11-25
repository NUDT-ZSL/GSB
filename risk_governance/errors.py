"""Business error codes used by reports and tests."""

E_QUANTILE_ORDER = "E_QUANTILE_ORDER"
E_ZERO_DENOM = "E_ZERO_DENOM"
E_BOUNDARY_FLIP = "E_BOUNDARY_FLIP"
E_BOUNDARY_STATS = "E_BOUNDARY_STATS"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_COVERAGE_GAP = "E_COVERAGE_GAP"
E_RECOVERY_STATE = "E_RECOVERY_STATE"

KNOWN_CODES = {
    E_QUANTILE_ORDER,
    E_ZERO_DENOM,
    E_BOUNDARY_FLIP,
    E_BOUNDARY_STATS,
    E_SNAPSHOT_ROLLBACK,
    E_COVERAGE_GAP,
    E_RECOVERY_STATE,
}


def make_error(code, message, **details):
    error = {"code": code, "message": message}
    error.update(details)
    return error
