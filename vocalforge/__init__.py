"""VocalForge Local: deterministic timbre-cloning and pronunciation-modeling lab."""

__all__ = ["LabService"]
__version__ = "1.0.0"


def __getattr__(name: str):
    if name == "LabService":
        from .service import LabService

        return LabService
    raise AttributeError(name)
