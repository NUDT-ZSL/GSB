"""Local fault-isolation and failover console.

The package is intentionally dependency-free and only uses the Python
standard library so it can run on a stock Windows Python installation.
"""

from .codes import BusinessCode
from .errors import BusinessError
from .models import Scene
from .scene import SceneLoader

__all__ = ["BusinessCode", "BusinessError", "Scene", "SceneLoader"]
__version__ = "1.0.0"
