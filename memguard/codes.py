"""Stable business codes used across parsing, judging and reporting."""

# Violation codes (per-access judgement)
E_BOUND_OVER = "E_BOUND_OVER"        # inside ancestors, beyond own region
E_ANCESTOR_OVER = "E_ANCESTOR_OVER"  # crosses an ancestor interval
E_AFTER_LIFE = "E_AFTER_LIFE"        # access after region lifecycle end

# Trace stream anomalies (region level)
E_SEQ_GAP = "E_SEQ_GAP"              # missing access_seq inside [min, max]

# Declaration resolution
E_REGION_CYCLE = "E_REGION_CYCLE"    # parent reference cycle
E_PARENT_UNKNOWN = "E_PARENT_UNKNOWN"
E_DUP_REGION = "E_DUP_REGION"

# Parse / schema rejects
E_JSON_LINE = "E_JSON_LINE"          # unparsable JSON line / document
E_SCHEMA_FIELD = "E_SCHEMA_FIELD"    # field type/range violation
E_KIND_UNKNOWN = "E_KIND_UNKNOWN"    # access_kind not in whitelist
E_DUP_TRACE = "E_DUP_TRACE"          # duplicated trace_id

# Runtime
E_LOCK = "E_LOCK"                    # concurrent writer lock conflict
E_CHECKPOINT = "E_CHECKPOINT"        # checkpoint mismatch / half-write
E_VERSION = "E_VERSION"              # layout/struct/snapshot version change

VIOLATION_CODES = (E_BOUND_OVER, E_ANCESTOR_OVER, E_AFTER_LIFE)

# Judgement precedence (documented, deterministic):
#   E_AFTER_LIFE > E_ANCESTOR_OVER > E_BOUND_OVER
