"""生成确定性本地样本（对象清单 / 桶声明 / 存储配置）。"""
import base64
import hashlib
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "samples"


def make_object(object_key, version, chunk_payloads):
    chunks = []
    for seq, payload in enumerate(chunk_payloads):
        content = payload.encode("utf-8")
        chunks.append({
            "seq": seq,
            "content_b64": base64.b64encode(content).decode("ascii"),
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    return {"object_key": object_key, "version": version, "chunks": chunks}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    config = {
        "max_chunk_size": 1024,
        "cache_max_entries": 3,
        "read_latency_budget_ms": 50.0,
        "fixed_scale": 1000,
        "compare_tolerance": 0.05,
        "error_bound_budget": 0.5,
        "clock_step_ms": 5,
        "assembly_chunk_threshold": 4,
        "assembly_cache_threshold": 8,
        "clock_regression_threshold_ms": 100,
        "budget_window_factor": 4,
    }
    objects = [
        make_object("docs/readme.txt", 1, [
            "# chunkmeta\n",
            "local object store chunk & metadata manager.\n",
            "fixed-point diagnostics included.\n",
        ]),
        make_object("bin/blob.bin", 3, [
            f"blob-chunk-{i:02d}:" + "x" * 40 for i in range(9)
        ]),
    ]
    buckets = [
        {
            "bucket": "media-store",
            "consistency": "strong",
            "metadata": {
                "owner": "local-dev",
                "tier": "hot",
                "retention_days": 30,
                "replicated": False,
            },
            "clock_ms": 1000,
        }
    ]
    (OUT / "config.json").write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "objects.json").write_text(
        json.dumps(objects, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "buckets.json").write_text(
        json.dumps(buckets, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"samples written to {OUT}")


if __name__ == "__main__":
    main()
