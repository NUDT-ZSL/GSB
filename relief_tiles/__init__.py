"""Deterministic, tileable height-field relief renderer."""

from .config import ReliefConfig, load_config
from .errors import BusinessError
from .gray import compute_normalization_stats, generate_full_gray
from .noise import synthesize_block_height, synthesize_height

__all__ = [
    "BusinessError",
    "ReliefConfig",
    "compute_normalization_stats",
    "generate_full_gray",
    "load_config",
    "synthesize_block_height",
    "synthesize_height",
]

__version__ = "1.0.0"
