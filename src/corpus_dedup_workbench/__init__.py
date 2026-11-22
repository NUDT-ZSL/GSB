"""Deterministic local corpus dedup diagnostics workbench."""

from .config import WorkbenchConfig
from .errors import WorkbenchError
from .service import DedupService

__all__ = ["DedupService", "WorkbenchConfig", "WorkbenchError"]
__version__ = "0.1.0"


def main(argv: list[str] | None = None) -> int:
    from .cli import main as cli_main

    return cli_main(argv)

