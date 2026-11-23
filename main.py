"""Alternative ASGI entry point: uvicorn main:app."""

from diagnostic_workbench.api import app

__all__ = ["app"]

