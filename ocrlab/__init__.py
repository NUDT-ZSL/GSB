"""Deterministic local OCR pipeline laboratory."""

from .config import PipelineConfig
from .templates import TemplateLibrary, get_template_library

__all__ = ["PipelineConfig", "TemplateLibrary", "get_template_library"]

