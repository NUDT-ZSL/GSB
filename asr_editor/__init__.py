"""ASR transcript interoperability editor.

The package keeps all timing in integer milliseconds and treats the
token-level unified representation as the authoritative workspace state.
"""

from .models import Token, Transcript, Utterance

__all__ = ["Token", "Transcript", "Utterance"]
__version__ = "1.0.0"
