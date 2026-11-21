import json

import numpy as np
import pytest

from pano_workbench.errors import (
    E_INSTANCE_DUP,
    E_PIXEL_CONFLICT,
    E_VERSION_UNSUPPORTED,
    PanoError,
)
from pano_workbench.formats import encode_v1, encode_v2, read_mask
from pano_workbench.generator import generate_sample
from pano_workbench.merge import merge_masks


def _parsed_all(sample, cfg):
    return [
        read_mask(sample.pred_v1, cfg),
        read_mask(sample.pred_v2, cfg),
        read_mask(sample.pred_rle, cfg),
    ]


def test_three_format_merge_matches_source_arrays(cfg):
    sample = generate_sample(cfg)
    mask = merge_masks(_parsed_all(sample, cfg), cfg)
    scored = ~mask.ignore_mask
    assert np.array_equal(mask.class_map, sample.pred_class)
    assert np.array_equal(mask.instance_map, sample.pred_instance)
    assert np.allclose(mask.conf_map, sample.pred_conf, atol=1e-6)
    assert mask.sources == (1, 2, 3)
    # ignore pixels carry no scoring content
    assert (mask.class_map[~scored] == 255).all()
    assert (mask.instance_map[~scored] == 0).all()


def test_merge_is_byte_reproducible(cfg):
    sample = generate_sample(cfg)
    a = merge_masks(_parsed_all(sample, cfg), cfg)
    b = merge_masks(_parsed_all(sample, cfg), cfg)
    assert a.canonical_bytes() == b.canonical_bytes()


def test_pixel_conflict_between_sources(cfg):
    sample = generate_sample(cfg)
    cls = sample.pred_class.copy()
    ys, xs = np.where(sample.pred_instance == 1)
    y, x = int(ys[0]), int(xs[0])
    cls[y, x] = (int(cls[y, x]) % cfg.num_categories) + 1   # different category
    v1 = read_mask(encode_v1(cls, cfg.mapping_version), cfg)
    v2 = read_mask(sample.pred_v2, cfg)
    with pytest.raises(PanoError) as exc:
        merge_masks([v1, v2], cfg)
    assert exc.value.code == E_PIXEL_CONFLICT
    assert (exc.value.details["x"], exc.value.details["y"]) == (x, y)


def test_instance_conflict_between_sources(cfg):
    sample = generate_sample(cfg)
    inst = sample.pred_instance.copy()
    ys, xs = np.where(inst == 1)
    y, x = int(ys[0]), int(xs[0])
    inst[y, x] = 2                                          # different instance
    v2a = read_mask(sample.pred_v2, cfg)
    v2b = read_mask(
        encode_v2(sample.pred_class, inst, sample.pred_conf, cfg.mapping_version),
        cfg,
    )
    with pytest.raises(PanoError) as exc:
        merge_masks([v2a, v2b], cfg)
    assert exc.value.code == E_PIXEL_CONFLICT


def test_instance_id_reused_with_another_category(cfg):
    sample = generate_sample(cfg)
    cls = sample.pred_class.copy()
    inst = sample.pred_instance.copy()
    # paint instance id 1 onto a background patch with a different category
    patch = (sample.pred_instance == 0) & (~sample.pred_ignore)
    ys, xs = np.where(patch)
    y, x = int(ys[0]), int(xs[0])
    cls[y, x] = (int(sample.pred_class[inst == 1][0]) % cfg.num_categories) + 1
    inst[y, x] = 1
    v2 = read_mask(encode_v2(cls, inst, sample.pred_conf, cfg.mapping_version), cfg)
    with pytest.raises(PanoError) as exc:
        merge_masks([v2], cfg)
    assert exc.value.code == E_INSTANCE_DUP
    assert exc.value.details["instance_id"] == 1


def test_rle_duplicate_instance_id(cfg):
    doc = json.loads(generate_sample(cfg).pred_rle)
    doc["segments"].append(dict(doc["segments"][0]))     # same instance id
    with pytest.raises(PanoError) as exc:
        read_mask(doc, cfg)
    assert exc.value.code == E_INSTANCE_DUP


def test_mapping_version_mismatch(cfg):
    sample = generate_sample(cfg)
    v1 = read_mask(encode_v1(sample.pred_class, cfg.mapping_version + 1), cfg)
    with pytest.raises(PanoError) as exc:
        merge_masks([v1], cfg)
    assert exc.value.code == E_VERSION_UNSUPPORTED
    assert exc.value.details["expected"] == cfg.mapping_version
