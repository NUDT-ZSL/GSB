"""Business error codes and process exit codes.

Business error codes are grouped by layer:
  E1xxx  declaration / normalization rejections
  E2xxx  consistency findings (conflict / shadow / dependency cycle)
  E3xxx  scheduling and resource-budget events
  E4xxx  checkpoint / recovery events
  E5xxx  storage / locking events
"""

# --- E1xxx: declaration rejections ---
E_DUPLICATE_RULE_ID = "E1001"
E_INVALID_TARGET = "E1002"
E_CONDITION_SYNTAX = "E1003"
E_FORBIDDEN_OPERATOR = "E1004"
E_PRIORITY_RANGE = "E1005"
E_VERSION_FORMAT = "E1006"
E_MISSING_FIELD = "E1007"
E_INVALID_ACTION = "E1008"

# --- E2xxx: findings ---
E_CONFLICT = "E2001"
E_SHADOW = "E2002"
E_DEPENDENCY_CYCLE = "E2003"

# --- E3xxx: scheduling / resource budget ---
E_QUEUE_FULL = "E3001"
E_TASK_TIMEOUT = "E3002"
E_TASK_MEMORY_EXCEEDED = "E3003"
E_BATCH_BUDGET_EXHAUSTED = "E3004"

# --- E4xxx: checkpoint / recovery ---
E_CHECKPOINT_VERSION_MISMATCH = "E4001"
E_POLICY_VERSION_CHANGED = "E4002"

# --- E5xxx: storage ---
E_LOCK_CONFLICT = "E5001"

# Process exit codes (bitmask, combinable).
EXIT_OK = 0
EXIT_REJECTIONS = 1        # one or more declarations were rejected
EXIT_FINDINGS = 2          # conflicts / shadows / cycles found
EXIT_RESOURCE_EVENTS = 4   # timeout / memory / budget / queue events
EXIT_LOCK_CONFLICT = 8     # could not serialize snapshot writes
EXIT_INTERNAL = 64         # unexpected internal failure
