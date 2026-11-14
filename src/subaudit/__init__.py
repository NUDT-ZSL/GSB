"""subaudit：离线字幕-人声发声区间逐句核查工具。"""

from .audit import audit_manifest
from .vad import VoiceSegment, detect_voice_segments

__all__ = ["audit_manifest", "detect_voice_segments", "VoiceSegment"]
__version__ = "1.0.0"
