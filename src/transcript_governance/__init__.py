"""Deterministic transcript governance and diagnostics component."""

from .adjudicate import AdjudicationDecision, AdjudicationPair, adjudicate
from .attribution import DifferenceAttribution, attribute_difference
from .audio import AudioFeatures, FrameConfig, synthesize_sample_pcm, extract_features
from .cache import CacheKey, ContentAddressedCache
from .codes import Code, Diagnostic
from .consistency import ConsistencyReport, TimelineIndex, check_consistency
from .loading import load_transcript
from .migrate import MigrationResult, migrate_v1_to_v2, migrate_v2_to_v1
from .models import Snapshot, Utterance, Word
from .refresh import RefreshEngine, RefreshPlan

__version__ = "0.1.0"

__all__ = [
    "AdjudicationDecision",
    "AdjudicationPair",
    "AudioFeatures",
    "CacheKey",
    "Code",
    "ConsistencyReport",
    "ContentAddressedCache",
    "Diagnostic",
    "DifferenceAttribution",
    "FrameConfig",
    "MigrationResult",
    "RefreshEngine",
    "RefreshPlan",
    "Snapshot",
    "TimelineIndex",
    "Utterance",
    "Word",
    "adjudicate",
    "attribute_difference",
    "check_consistency",
    "extract_features",
    "load_transcript",
    "migrate_v1_to_v2",
    "migrate_v2_to_v1",
    "synthesize_sample_pcm",
]
