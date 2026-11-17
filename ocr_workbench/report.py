"""JSON diagnostic report writer with atomic whole-file replacement."""

import json
import os
import tempfile


def atomic_write_json(path, payload):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-report-", suffix=".json",
                                    dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2,
                      sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def serialize_results(results):
    """Deterministic, field-stable serialization of image pipeline results."""
    payload = []
    for result in sorted(results, key=lambda r: r.name):
        lines = []
        for line_index, line in enumerate(result.lines):
            lines.append({
                "line_index": line_index,
                "bbox": line["bbox"],
                "text": line["text"],
                "chars": [
                    {
                        "bbox": char["bbox"],
                        "char": char["char"],
                        "best_char": char["best_char"],
                        "score": char["score"],
                        "second_score": char["second_score"],
                        "accepted": char["accepted"],
                        "is_space": char.get("is_space", False),
                    }
                    for char in line["chars"]
                ],
            })
        payload.append({
            "image": result.name,
            "image_fingerprint": result.fingerprint,
            "reused_stages": list(result.reused_stages),
            "recomputed_stages": list(result.recomputed_stages),
            "cache_keys": {
                stage: result.products[stage].cache_key
                for stage in sorted(result.products)
            },
            "lines": lines,
        })
    return payload


def build_report(global_version, config_bundle, results, failures,
                 command_log, snapshot_version, business_codes):
    return {
        "global_version": int(global_version),
        "font_version": "font-5x7-v1",
        "config_hash": {
            "detection": config_bundle.detection_fingerprint(),
            "recognition": config_bundle.recognition_fingerprint(),
        },
        "snapshot_version": snapshot_version,
        "business_codes": sorted(set(business_codes)),
        "failures": sorted(failures, key=lambda f: f["file"]),
        "cache": {
            "hits": _stage_lists(results, "reused_stages"),
            "invalidations": _stage_lists(results, "recomputed_stages"),
        },
        "command_responses": list(command_log),
        "results": serialize_results(results),
    }


def _stage_lists(results, attr):
    out = []
    for result in sorted(results, key=lambda r: r.name):
        out.append({"image": result.name,
                    "stages": sorted(getattr(result, attr))})
    return out
