"""Deterministic NTFS-style permission audit pipeline."""

__version__ = "1.0.0"
LEDGER_LAYOUT_VERSION = "perm-audit-ledger-3"
POLICY_VERSION = "path-policy-1"
CODEC_VERSION = "mask-codec-1"
RULES_VERSION = "inherit-rules-1"

from .generator import generate_invalid_sample, generate_sample  # noqa: E402
from .pipeline import run_audit  # noqa: E402
from .verifier import verify_against_truth  # noqa: E402

__all__ = [
    "__version__",
    "LEDGER_LAYOUT_VERSION",
    "POLICY_VERSION",
    "CODEC_VERSION",
    "RULES_VERSION",
    "generate_sample",
    "generate_invalid_sample",
    "run_audit",
    "verify_against_truth",
]
