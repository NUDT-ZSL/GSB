"""MatForge deterministic texture studio."""

from .errors import MatForgeError
from .params import MaterialParams, NoiseParams
from .pipeline import generate_block, generate_full
from .plan import BlockPlan
from .presets import get_preset, preset_names, preset_summaries
from .studio import MaterialStudio

__all__ = [
    "BlockPlan",
    "MatForgeError",
    "MaterialParams",
    "MaterialStudio",
    "NoiseParams",
    "generate_block",
    "generate_full",
    "get_preset",
    "preset_names",
    "preset_summaries",
]

__version__ = "0.1.0"

