import json
import os
import tempfile

import numpy as np
from PIL import Image

from ocr_workbench.errors import (
    E_IMAGE_DECODE,
    E_IMAGE_SHAPE,
    E_TRUTH_OUT_OF_RANGE,
)
from ocr_workbench.loader import load_directory, load_sample
from ocr_workbench.samplegen import generate_dataset


def _truth(tmp, name="layout_000"):
    with open(os.path.join(tmp, name + ".json")) as handle:
        return json.load(handle)


def test_good_dataset_loads():
    tmp = tempfile.mkdtemp()
    generate_dataset(tmp, image_count=3)
    records, failures = load_directory(tmp, [480, 320])
    assert len(records) == 3
    assert failures == []
    assert records[0]["gray"].dtype == np.uint8
    assert records[0]["gray"].shape == (320, 480)
    assert records[0]["name"] == "layout_000"


def test_bad_png_header_is_decode_error():
    tmp = tempfile.mkdtemp()
    generate_dataset(tmp, image_count=1)
    png = os.path.join(tmp, "layout_000.png")
    with open(png, "wb") as handle:
        handle.write(b"NOTPNG" + b"\x00" * 32)
    record, failure = load_sample(png, [480, 320])
    assert record is None
    assert failure["code"] == E_IMAGE_DECODE


def test_wrong_mode_is_shape_error():
    tmp = tempfile.mkdtemp()
    generate_dataset(tmp, image_count=1)
    png = os.path.join(tmp, "rgb.png")
    Image.fromarray(np.zeros((320, 480, 3), dtype=np.uint8), "RGB").save(png)
    record, failure = load_sample(png, [480, 320])
    assert record is None
    assert failure["code"] == E_IMAGE_SHAPE


def test_wrong_size_is_shape_error():
    tmp = tempfile.mkdtemp()
    generate_dataset(tmp, image_count=1)
    record, failure = load_sample(
        os.path.join(tmp, "layout_000.png"), [400, 300])
    assert record is None
    assert failure["code"] == E_IMAGE_SHAPE


def test_truth_out_of_range():
    tmp = tempfile.mkdtemp()
    generate_dataset(tmp, image_count=1)
    truth = _truth(tmp)
    truth["lines"][0]["chars"][0]["bbox"] = [0, 0, 500, 400]
    with open(os.path.join(tmp, "layout_000.json"), "w") as handle:
        json.dump(truth, handle)
    record, failure = load_sample(
        os.path.join(tmp, "layout_000.png"), [480, 320])
    assert record is None
    assert failure["code"] == E_TRUTH_OUT_OF_RANGE


def test_one_bad_image_does_not_stop_peers():
    tmp = tempfile.mkdtemp()
    generate_dataset(tmp, image_count=3)
    with open(os.path.join(tmp, "layout_001.png"), "wb") as handle:
        handle.write(b"BAD")
    records, failures = load_directory(tmp, [480, 320])
    assert len(records) == 2
    assert [f["file"] for f in failures] == ["layout_001"]
    assert failures[0]["code"] == E_IMAGE_DECODE
