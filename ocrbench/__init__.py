"""Local deterministic OCR diagnostic workbench.

The package implements a deterministic OCR computation graph, a content
addressed cache, incremental invalidation propagation, template snapshot
versioning, chunk/whole-page consistency, resource and concurrency controls,
cross-module self checks and a Tkinter diagnostic workbench.

Everything is generated from fixed seeds locally; there is no network or
external OCR model dependency.
"""

from .errors import OcrBenchError, ErrorCode

__all__ = ["OcrBenchError", "ErrorCode"]
__version__ = "1.0.0"
