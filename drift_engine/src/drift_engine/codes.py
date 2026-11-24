"""Stable business codes shared by parsers, merge, adjudication and the CLI."""

# --- rejection / error codes (stable, never rename) ---
E_SNAPSHOT_FATAL = "E_SNAPSHOT_FATAL"          # snapshot unreadable / structurally invalid
E_CLOCK_MISMATCH = "E_CLOCK_MISMATCH"          # declared observation clock disagrees with spec
E_RECORD_DUP = "E_RECORD_DUP"                  # host/config identity not unique in snapshot
E_BAD_TS = "E_BAD_TS"                          # time not a non-negative integer
E_BAD_GENERATION = "E_BAD_GENERATION"          # evidence algebra missing / non-monotonic
E_UNKNOWN_KEY = "E_UNKNOWN_KEY"                # flat/canonical key not declared in spec
E_BAD_HOST = "E_BAD_HOST"                      # host identifier not declared in spec
E_VALUE_PARSE = "E_VALUE_PARSE"                # v1 value string cannot be parsed
E_TYPE_MISMATCH = "E_TYPE_MISMATCH"            # aligned value types disagree
E_INTERVAL_REVERSED = "E_INTERVAL_REVERSED"    # interval low endpoint greater than high
E_ENUM_NOT_ALLOWED = "E_ENUM_NOT_ALLOWED"      # enum/mask label outside whitelist
E_DIGEST_MISMATCH = "E_DIGEST_MISMATCH"        # shard evidence vs SQLite index disagreement
E_CONFLICT = "E_CONFLICT"                      # cross-generation value / verdict conflict
E_WINDOW_AMBIGUOUS = "E_WINDOW_AMBIGUOUS"      # quantization band straddles the window edge
E_DEP_CYCLE = "E_DEP_CYCLE"                    # dependency cycle reached from drift roots
E_CHECKPOINT_RESIDUE = "E_CHECKPOINT_RESIDUE"  # half-written checkpoint residue detected
E_VERSION_REBUILD = "E_VERSION_REBUILD"        # merge/whitelist version change forces rebuild

# --- value relation codes (order-independent outcomes) ---
R_EQUAL = "R_EQUAL"
R_CONTAINS = "R_CONTAINS"
R_EXCLUSIVE = "R_EXCLUSIVE"

# --- disposition verdicts ---
D_IMMEDIATE = "IMMEDIATE"      # deterministically over the window, not frozen
D_GRACE = "GRACE"              # deterministically inside the observation window
D_AMBIGUOUS = "AMBIGUOUS"      # quantization band straddles the window edge
D_FROZEN = "FROZEN"            # freeze rule hit: never immediate even if over window
D_REVOKED = "REVOKED"          # tombstoned evidence revoked the drift
D_NOMINAL = "NOMINAL"          # no baseline crossing observed

# --- process exit codes ---
EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_FATAL = 3
