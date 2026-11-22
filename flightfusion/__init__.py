"""FlightFusion: deterministic flight-control attitude fusion domain component."""

from .cache import ArtifactCache
from .config import load_default_workspace
from .errors import BusinessError, ErrorCode
from .formats import parse_description_file, parse_document
from .graph import FusionGraph
from .injection import Injection, InjectionPlan
from .migration import migrate_descriptions
from .snapshot import SnapshotStore


def run_demo(*args, **kwargs):
    from .demo import run_demo as implementation

    return implementation(*args, **kwargs)

__all__ = [
    "ArtifactCache",
    "BusinessError",
    "ErrorCode",
    "FusionGraph",
    "Injection",
    "InjectionPlan",
    "SnapshotStore",
    "load_default_workspace",
    "migrate_descriptions",
    "parse_description_file",
    "parse_document",
    "run_demo",
]

__version__ = "0.1.0"

