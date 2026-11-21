"""Local diagnostic workbench for tool-result format evolution and RAG assembly.

The package is deliberately dependency-light: only the Python standard library
is required at runtime. NumPy is used solely by the deterministic sample
generator (see :mod:`diagwb.samples`).
"""

__all__ = ["errors", "hashing", "models", "formats", "migration",
           "equivalence", "cache", "limits", "store", "snapshots",
           "graph", "lineage", "workbench"]

__version__ = "1.0.0"
