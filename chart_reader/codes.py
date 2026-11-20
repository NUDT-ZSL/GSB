"""Business codes returned by the engine.

Every abnormal or degraded outcome is reported with one of these codes
plus the chart_id (and stage / field / category where applicable).
"""

E_TICK_LOC_INVALID = "E_TICK_LOC_INVALID"          # tick pixel out of canvas / invalid
E_TICK_COUNT_MISMATCH = "E_TICK_COUNT_MISMATCH"    # tick count != truth declaration
E_AXIS_RANGE_INVALID = "E_AXIS_RANGE_INVALID"      # axis range non-positive
E_POINT_OUT_OF_CANVAS = "E_POINT_OUT_OF_CANVAS"    # point candidate outside canvas
E_DUPLICATE_SERIES = "E_DUPLICATE_SERIES"          # duplicated series id
E_CALIB_DEGENERATE = "E_CALIB_DEGENERATE"          # tick pixels coincide, no slope
E_SERIES_INVALID = "E_SERIES_INVALID"              # empty series / too many candidates
E_ERROR_UNTRACEABLE = "E_ERROR_UNTRACEABLE"        # error interval / ratio not traceable
E_CONFIG_INVALID = "E_CONFIG_INVALID"              # illegal threshold / tolerance / ...
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"        # old version tried to overwrite new
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"          # checksum mismatch on load
E_INCONSISTENT = "E_INCONSISTENT"                  # cross-stage self-check failed

ALL_CODES = frozenset({
    E_TICK_LOC_INVALID,
    E_TICK_COUNT_MISMATCH,
    E_AXIS_RANGE_INVALID,
    E_POINT_OUT_OF_CANVAS,
    E_DUPLICATE_SERIES,
    E_CALIB_DEGENERATE,
    E_SERIES_INVALID,
    E_ERROR_UNTRACEABLE,
    E_CONFIG_INVALID,
    E_SNAPSHOT_ROLLBACK,
    E_SNAPSHOT_CORRUPT,
    E_INCONSISTENT,
})
