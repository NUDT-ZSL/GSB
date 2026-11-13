"""MVST - deterministic multi-language audio/subtitle/video mastering tool.

Pure standard-library implementation.  The package exposes four command line
sub-commands through :mod:`mvst.cli`: ``validate``, ``package``, ``info`` and
``selftest``.
"""

__version__ = "1.0.0"
CONTAINER_VERSION = 1
MAGIC = b"MVST"
MASTER_SAMPLE_RATE = 48000
VIDEO_FPS = 25
LANGUAGE_WHITELIST = ("en", "es", "fr", "ja")
