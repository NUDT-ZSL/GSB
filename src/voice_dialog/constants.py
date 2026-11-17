"""Stable names, constants, and business codes shared by all stages."""

from __future__ import annotations

REPORT_VERSION = "1.0"
CACHE_SCHEMA_VERSION = 1
SNAPSHOT_SCHEMA_VERSION = 1

COARSE_EMOTIONS = ("negative", "neutral", "positive")
FINE_EMOTIONS = ("angry", "disgust", "fear", "happy", "neutral", "sad")

# The synthetic labels use the six-class taxonomy.  This map is used only for
# reporting accuracy when a coarse classifier is requested.
FINE_TO_COARSE = {
    "angry": "negative",
    "disgust": "negative",
    "fear": "negative",
    "happy": "positive",
    "neutral": "neutral",
    "sad": "negative",
}

FEATURE_NAMES = (
    "f0_mean",
    "f0_std",
    "f0_range",
    "f0_slope",
    "energy_mean",
    "energy_std",
    "energy_dynamic_range",
    "zcr_mean",
    "zcr_std",
    "voiced_ratio",
    "speech_rate",
    "rhythm_regularity",
    "pause_ratio",
    "activity_ratio",
)

CORE_FEATURES = (
    "f0_mean",
    "f0_std",
    "energy_mean",
    "energy_dynamic_range",
    "zcr_mean",
    "voiced_ratio",
    "speech_rate",
)
COMPACT_FEATURES = (
    "f0_mean",
    "f0_std",
    "energy_mean",
    "energy_dynamic_range",
    "speech_rate",
    "rhythm_regularity",
)
BALANCED_FEATURES = (
    "f0_mean",
    "f0_std",
    "f0_range",
    "energy_mean",
    "energy_std",
    "energy_dynamic_range",
    "zcr_mean",
    "zcr_std",
    "voiced_ratio",
    "speech_rate",
    "rhythm_regularity",
    "pause_ratio",
)

STAGES = ("features", "scores", "fusion")
CLASSIFIERS = ("nearest_centroid", "weighted_logistic_regression")

E_AUDIO_FORMAT = "E_AUDIO_FORMAT"
E_ZERO_DURATION = "E_ZERO_DURATION"
E_NONFINITE_SAMPLE = "E_NONFINITE_SAMPLE"
E_NONFINITE_FEATURE = "E_NONFINITE_FEATURE"
E_INVALID_GRANULARITY = "E_INVALID_GRANULARITY"
E_CONFIG_MISMATCH = "E_CONFIG_MISMATCH"
E_INVALID_CONFIG = "E_INVALID_CONFIG"
E_MISSING_AUDIO = "E_MISSING_AUDIO"
E_MISSING_PREDECESSOR = "E_MISSING_PREDECESSOR"
E_SCRIPT_INVALID = "E_SCRIPT_INVALID"
E_INTERNAL = "E_INTERNAL"

F0_MIN_HZ = 60.0
F0_MAX_HZ = 500.0
F0_QUANTUM_HZ = 10
