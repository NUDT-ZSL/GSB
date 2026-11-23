"""Windows-friendly ASGI entry point: uvicorn app:app."""

from diagnostic_workbench.api import app

__all__ = ["app"]

