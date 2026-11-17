"""Detection / recognition configuration loading and parameter fingerprints."""

import hashlib
import json

from .errors import E_CONFIG_INVALID, WorkbenchError
from .font import FONT_VERSION

DETECTION_DEFAULTS = {
    "image_size": [480, 320],
    "window_sizes": [[12, 12], [18, 18]],
    "window_step": 4,
    "binary_threshold": 160,
    "density_threshold": 0.08,
    "edge_threshold": 0.04,
    "merge_gap": 6,
    "connectivity": 8,
    "line_gap": 6,
    "min_char_height": 10,
    "candidate_score_threshold": 0.10,
    "max_workers": 2,
    "interact_budget_ms": 50,
    "snapshot_interval": 2,
}

RECOGNITION_DEFAULTS = {
    "template_set": "builtin-5x7",
    "glyph_scale": 2,
    "metric": "coverage",
    "confidence_threshold": 0.60,
    "min_char_width": 10,
    "char_pitch": 12,
    "space_gap": 12,
    "tie_break": "charset",
    "merge_narrow": True,
    "narrow_ratio": 0.6,
}


def _load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _merge(defaults, overrides):
    merged = dict(defaults)
    for key, value in (overrides or {}).items():
        if key not in defaults:
            raise WorkbenchError(E_CONFIG_INVALID, f"unknown parameter '{key}'")
        merged[key] = value
    return merged


def _validate_detection(cfg):
    if cfg["window_step"] <= 0:
        raise WorkbenchError(E_CONFIG_INVALID, "window_step must be positive")
    if not cfg["window_sizes"]:
        raise WorkbenchError(E_CONFIG_INVALID, "window_sizes cannot be empty")
    for size in cfg["window_sizes"]:
        if len(size) != 2 or size[0] <= 0 or size[1] <= 0:
            raise WorkbenchError(E_CONFIG_INVALID, "invalid window size")
    if not 0 <= cfg["binary_threshold"] <= 255:
        raise WorkbenchError(E_CONFIG_INVALID, "binary_threshold out of range")
    if cfg["connectivity"] not in (4, 8):
        raise WorkbenchError(E_CONFIG_INVALID, "connectivity must be 4 or 8")
    if cfg["max_workers"] < 1:
        raise WorkbenchError(E_CONFIG_INVALID, "max_workers must be >= 1")
    if cfg["interact_budget_ms"] < 0:
        raise WorkbenchError(E_CONFIG_INVALID, "interact_budget_ms must be >= 0")
    if cfg["snapshot_interval"] < 1:
        raise WorkbenchError(E_CONFIG_INVALID, "snapshot_interval must be >= 1")


def _validate_recognition(cfg):
    if cfg["metric"] not in ("xor", "mse", "coverage"):
        raise WorkbenchError(E_CONFIG_INVALID, "unsupported metric")
    if cfg["tie_break"] not in ("charset", "reverse", "score_then_charset"):
        raise WorkbenchError(E_CONFIG_INVALID, "unsupported tie_break")
    if not 0.0 <= cfg["confidence_threshold"] <= 1.0:
        raise WorkbenchError(E_CONFIG_INVALID, "confidence_threshold out of range")
    if cfg["glyph_scale"] < 1:
        raise WorkbenchError(E_CONFIG_INVALID, "glyph_scale must be >= 1")


class ConfigBundle:
    """Immutable view over detection and recognition configuration."""

    def __init__(self, detection, recognition):
        self.detection = detection
        self.recognition = recognition

    @classmethod
    def load(cls, detection_path, recognition_path):
        det_raw = _load_json(detection_path)
        rec_raw = _load_json(recognition_path)
        detection = _merge(DETECTION_DEFAULTS, det_raw)
        recognition = _merge(RECOGNITION_DEFAULTS, rec_raw)
        _validate_detection(detection)
        _validate_recognition(recognition)
        return cls(detection, recognition)

    def with_overrides(self, det_overrides=None, rec_overrides=None):
        detection = _merge(self.detection, det_overrides or {})
        recognition = _merge(self.recognition, rec_overrides or {})
        _validate_detection(detection)
        _validate_recognition(recognition)
        return ConfigBundle(detection, recognition)

    def detection_fingerprint(self):
        return fingerprint(self.detection)

    def recognition_fingerprint(self):
        return fingerprint(self.recognition)

    def recognition_segmentation_fingerprint(self):
        from .pipeline import SEGMENTATION_REC_PARAMS
        subset = {key: self.recognition[key] for key in SEGMENTATION_REC_PARAMS}
        return fingerprint(subset)


def fingerprint(payload):
    """Stable SHA-256 fingerprint over JSON-canonicalizable payload."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def stage_cache_key(stage, image_fingerprint, det_fp, rec_fp):
    payload = {
        "stage": stage,
        "image": image_fingerprint,
        "detection": det_fp,
        "recognition": rec_fp,
        "font": FONT_VERSION,
    }
    return fingerprint(payload)
