"""Four-stage detection/recognition pipeline with fingerprinted caching.

Stages:
  detection     -> raw candidate blocks + window coverage evidence
  aggregation   -> ordered text-line candidates
  segmentation  -> character segments per line
  recognition   -> recognized chars / text per line

Each stage product carries a cache key derived from the image content
fingerprint, the relevant parameter fingerprints and the font template
version.  Changing a parameter only invalidates the stages that depend on it
(and their downstream consumers); untouched stages are reused without any
re-execution.
"""

from dataclasses import dataclass, field

from . import detector
from .config import stage_cache_key
from .recognizer import build_templates, recognize_line

STAGES = ("detection", "aggregation", "segmentation", "recognition")

# Each stage only names the parameter fingerprints it actually reads, so an
# unrelated edit keeps the stage's cache key stable and the product reusable.
STAGE_PARAMS = {
    "detection": {"detection": "det"},
    "aggregation": {"detection": "det"},
    "segmentation": {"detection": "det", "recognition": "rec-seg"},
    "recognition": {"detection": "det", "recognition": "rec"},
}

# Recognition parameters that alter character segmentation geometry.  Matching
# metric / confidence only affect scoring, so they leave segmentation reusable.
SEGMENTATION_REC_PARAMS = (
    "glyph_scale", "min_char_width", "char_pitch", "space_gap",
    "merge_narrow", "narrow_ratio",
)


def _stage_key(stage, image_fp, det_fp, rec_fp, rec_seg_fp=None):
    payload = {
        "stage": stage,
        "image": image_fp,
        "font": __import__("ocr_workbench.font", fromlist=["FONT_VERSION"]).FONT_VERSION,
    }
    for name, kind in STAGE_PARAMS[stage].items():
        if kind == "det":
            payload[name] = det_fp
        elif kind == "rec":
            payload[name] = rec_fp
        else:
            payload[name] = rec_seg_fp
    from .config import fingerprint
    return fingerprint(payload)


@dataclass
class StageProduct:
    stage: str
    cache_key: str
    payload: object
    reused: bool = False


@dataclass
class ImageResult:
    name: str
    fingerprint: str
    products: dict = field(default_factory=dict)
    lines: list = field(default_factory=list)
    reused_stages: list = field(default_factory=list)
    recomputed_stages: list = field(default_factory=list)


class StageCache:
    """Multi-version stage cache keyed by (image, stage, cache key).

    Keeping every distinct cache key (bounded by the number of distinct
    parameter combinations observed) lets a later edit return to an earlier
    parameter set and reuse the original stage products instead of rebuilding
    them.  Lookup remains O(1).
    """

    def __init__(self):
        self._store = {}

    def get(self, name, stage, cache_key):
        return self._store.get((name, stage, cache_key))

    def put(self, name, stage, cache_key, payload):
        product = StageProduct(stage, cache_key, payload)
        self._store[(name, stage, cache_key)] = product
        return product

    def clear_image(self, name):
        for key in list(self._store):
            if key[0] == name:
                del self._store[key]


def _key(stage, image_fp, det_fp, rec_fp, rec_seg_fp=None):
    return _stage_key(stage, image_fp, det_fp, rec_fp, rec_seg_fp)


def run_image(record, config_bundle, cache, templates=None):
    """Execute the pipeline for one image, reusing every still-valid stage."""
    det_cfg = config_bundle.detection
    rec_cfg = config_bundle.recognition
    det_fp = config_bundle.detection_fingerprint()
    rec_fp = config_bundle.recognition_fingerprint()
    rec_seg_fp = config_bundle.recognition_segmentation_fingerprint()
    image_fp = record["fingerprint"]
    gray = record["gray"]
    name = record["name"]

    result = ImageResult(name=name, fingerprint=image_fp)
    if templates is None:
        templates = build_templates(rec_cfg)

    # Stage 1: detection (raw mask evidence).
    det_key = _key("detection", image_fp, det_fp, rec_fp, rec_seg_fp)
    entry = cache.get(name, "detection", det_key)
    if entry is not None:
        result.products["detection"] = entry
        result.reused_stages.append("detection")
    else:
        mask = detector.stroke_mask(gray, det_cfg["binary_threshold"])
        payload = {"mask": mask, "binary_threshold": det_cfg["binary_threshold"]}
        result.products["detection"] = cache.put(
            name, "detection", det_key, payload)
        result.recomputed_stages.append("detection")

    # Stage 2: line aggregation.
    agg_key = _key("aggregation", image_fp, det_fp, rec_fp, rec_seg_fp)
    entry = cache.get(name, "aggregation", agg_key)
    if entry is not None:
        result.products["aggregation"] = entry
        result.reused_stages.append("aggregation")
    else:
        lines = detector.detect(gray, det_cfg)
        result.products["aggregation"] = cache.put(
            name, "aggregation", agg_key, lines)
        result.recomputed_stages.append("aggregation")
    line_candidates = result.products["aggregation"].payload

    # Stages 3+4 run per line but are stored as one image-level product.
    seg_key = _key("segmentation", image_fp, det_fp, rec_fp, rec_seg_fp)
    rec_key = _key("recognition", image_fp, det_fp, rec_fp, rec_seg_fp)

    seg_entry = cache.get(name, "segmentation", seg_key)
    if seg_entry is not None:
        result.products["segmentation"] = seg_entry
        result.reused_stages.append("segmentation")
        segments_by_line = seg_entry.payload
    else:
        segments_by_line = []
        for line in line_candidates:
            segments = detector_typed_segments(
                gray, line["bbox"], rec_cfg, det_cfg)
            segments_by_line.append(segments)
        result.products["segmentation"] = cache.put(
            name, "segmentation", seg_key, segments_by_line)
        result.recomputed_stages.append("segmentation")

    rec_entry = cache.get(name, "recognition", rec_key)
    if rec_entry is not None:
        result.products["recognition"] = rec_entry
        result.reused_stages.append("recognition")
        result.lines = rec_entry.payload
    else:
        recognized = []
        for line in line_candidates:
            recognized.append(
                recognize_line(gray, line["bbox"], templates, rec_cfg, det_cfg))
        result.products["recognition"] = cache.put(
            name, "recognition", rec_key, recognized)
        result.recomputed_stages.append("recognition")
        result.lines = recognized

    return result


def detector_typed_segments(gray, bbox, rec_cfg, det_cfg):
    from .recognizer import segment_characters
    return segment_characters(gray, bbox, rec_cfg, det_cfg)


def invalidate_plan(det_changed, rec_changed):
    """Return the ordered stages that must be recomputed after an edit."""
    plan = []
    if det_changed:
        plan.extend(["detection", "aggregation", "segmentation", "recognition"])
    elif rec_changed:
        plan.extend(["segmentation", "recognition"])
    return plan
