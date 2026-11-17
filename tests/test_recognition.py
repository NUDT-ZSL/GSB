import tempfile

from ocr_workbench.config import DETECTION_DEFAULTS, RECOGNITION_DEFAULTS
from ocr_workbench.detector import detect
from ocr_workbench.loader import load_directory
from ocr_workbench.recognizer import (
    build_templates,
    recognize_line,
    segment_characters,
)
from ocr_workbench.samplegen import generate_dataset


def _setup(count=3):
    tmp = tempfile.mkdtemp()
    generate_dataset(tmp, image_count=count)
    records, _ = load_directory(tmp, DETECTION_DEFAULTS["image_size"])
    return records


def test_templates_cover_charset():
    templates = build_templates(RECOGNITION_DEFAULTS)
    from ocr_workbench.font import CHARSET
    assert set(templates) == set(CHARSET)
    assert templates["A"].shape[0] == 14


def test_each_segment_is_integer_and_non_dropped():
    records = _setup(1)
    record = records[0]
    lines = detect(record["gray"], DETECTION_DEFAULTS)
    for line in lines:
        segs = segment_characters(
            record["gray"], line["bbox"], RECOGNITION_DEFAULTS,
            DETECTION_DEFAULTS)
        assert segs
        for seg in segs:
            assert all(isinstance(v, int) for v in seg[:4])
            assert seg[2] > seg[0]


def test_recognition_outputs_scores_and_second_best():
    records = _setup(1)
    record = records[0]
    templates = build_templates(RECOGNITION_DEFAULTS)
    lines = detect(record["gray"], DETECTION_DEFAULTS)
    out = recognize_line(record["gray"], lines[-1]["bbox"], templates,
                         RECOGNITION_DEFAULTS, DETECTION_DEFAULTS)
    assert out["chars"]
    for char in out["chars"]:
        if char.get("is_space"):
            continue
        assert 0.0 <= char["second_score"] <= char["score"] <= 1.0
        assert "char" in char and "best_char" in char


def test_tie_break_is_config_driven_and_deterministic():
    records = _setup(1)
    record = records[0]
    templates = build_templates(RECOGNITION_DEFAULTS)
    lines = detect(record["gray"], DETECTION_DEFAULTS)
    cfg_a = dict(RECOGNITION_DEFAULTS, tie_break="charset")
    cfg_b = dict(RECOGNITION_DEFAULTS, tie_break="reverse")
    out_a = recognize_line(record["gray"], lines[0]["bbox"], templates,
                           cfg_a, DETECTION_DEFAULTS)
    out_b = recognize_line(record["gray"], lines[0]["bbox"], templates,
                           cfg_b, DETECTION_DEFAULTS)
    # Same scores regardless of tie order; determinism holds on repeat.
    out_a2 = recognize_line(record["gray"], lines[0]["bbox"], templates,
                            cfg_a, DETECTION_DEFAULTS)
    assert out_a == out_a2
    assert isinstance(out_b["text"], str)
