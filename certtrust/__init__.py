"""certtrust: local certificate chain & trust-anchor management tool.

Fully offline, deterministic, standard-library only.
"""

STRUCTURE_VERSION = 1
TOOL_VERSION = "1.0.0"

KNOWN_POLICY_VERSIONS = frozenset({"1.0.0", "1.1.0", "2.0.0"})

__all__ = ["STRUCTURE_VERSION", "TOOL_VERSION", "KNOWN_POLICY_VERSIONS"]
