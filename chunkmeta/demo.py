"""端到端演示：加载本地样本，完成落盘/读取/诊断/逐出/恢复全流程，
输出 JSON 运行记录。

用法：python -m chunkmeta.demo --samples samples --workdir /tmp/chunkmeta-demo
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from .cache import MetadataCache
from .samples import load_samples
from .service import StoreService


def run_demo(samples_dir, workdir) -> dict:
    workdir = Path(workdir)
    if workdir.exists():
        shutil.rmtree(workdir)  # 验证前清空，保证可重复
    workdir.mkdir(parents=True)
    record = {"workdir": str(workdir), "steps": {}}

    manifests, buckets, config = load_samples(samples_dir)
    record["steps"]["load"] = {
        "objects": [m.object_key for m in manifests],
        "buckets": [b.bucket for b in buckets],
    }
    svc = StoreService(workdir / "store", config)
    bucket = buckets[0]

    # 1. 分片落盘 + 元数据提交
    ingests = [svc.ingest(m, bucket) for m in manifests]
    record["steps"]["ingest"] = [
        {"object_key": r.object_key, "version": r.version,
         "idempotent": r.idempotent, "commit_clock": r.commit_clock,
         "chunks": len(r.chunk_digests), "meta_versions": r.meta_versions}
        for r in ingests
    ]
    record["steps"]["chunk_files"] = svc.chunks.count_files()

    # 2. 整读与范围读一致性
    reads = {}
    for m in manifests:
        expect = b"".join(c.content for c in m.chunks)
        full = svc.read(bucket.bucket, m.object_key)
        part = svc.range_read(bucket.bucket, m.object_key, 3, 17)
        reads[m.object_key] = {
            "full_read_match": full.data == expect,
            "range_read_match": part.data == expect[3:20],
            "mode": full.assembly.mode,
            "trigger": full.assembly.trigger,
            "rejected_mode": full.assembly.rejected_mode,
            "rejected_reason": full.assembly.rejected_reason,
            "batch_error_bound_ms": full.assembly.batch_error_bound_ms,
            "consistency_anomaly": full.assembly.consistency_anomaly,
            "latency": full.assembly.latency.to_dict(),
        }
    record["steps"]["reads"] = reads

    # 3. 缓存逐出双通路对照（粗标度下构造定点并列、decimal 可分的条目）
    dual = {}
    for demo_scale in (config.fixed_scale, 10):
        demo_cache = MetadataCache(capacity=2, scale=demo_scale)
        demo_cache.put("alpha", "12", clock=10)   # 4B -> 0.333.. 单位
        demo_cache.put("zeta", "1", clock=10)     # 3B -> 0.25   单位
        fixed_seq, decimal_seq, divergence, cache_mode = demo_cache.dual_path_order()
        dual[f"scale={demo_scale}"] = {
            "fixed_sequence": fixed_seq,
            "decimal_sequence": decimal_seq,
            "mode": cache_mode,
            "divergence": divergence.to_dict(),
        }
    record["steps"]["cache_dual_path"] = dual

    # 4. 强制终止恢复：制造“分片已落盘、元数据半提交”现场
    from .samples import ObjectManifest
    from .samples import ChunkDecl
    import hashlib
    crash_chunks = []
    for seq, payload in enumerate([b"crash-window-payload-a", b"crash-window-payload-b"]):
        crash_chunks.append(ChunkDecl(seq=seq, content=payload, size=len(payload),
                                      sha256=hashlib.sha256(payload).hexdigest()))
    crashed = ObjectManifest(
        object_key="crash/victim.bin", version=1, chunks=crash_chunks)
    for c in crashed.chunks:
        svc.chunks.put(c.content)
    with svc.db.tx() as conn:  # 模拟提交中途被 kill：留下 committing 行
        conn.execute(
            "INSERT INTO objects(bucket, object_key, version, state, commit_clock,"
            " chunk_count, total_bytes) VALUES (?, ?, ?, 'committing', NULL, ?, ?)",
            (bucket.bucket, crashed.object_key, crashed.version,
             len(crashed.chunks), sum(c.size for c in crashed.chunks)))
        conn.execute(
            "INSERT INTO bucket_meta(bucket, mkey, mvalue, mtype, clock, meta_version, state)"
            " VALUES (?, 'half.meta', '\"x\"', 'str', 0, 1, 'committing')",
            (bucket.bucket,))
    files_before = svc.chunks.count_files()
    report = svc.recover()
    files_after = svc.chunks.count_files()
    # 恢复后重新提交并整读，与不中断运行逐字节一致
    svc.ingest(crashed, bucket)
    reread = svc.read(bucket.bucket, crashed.object_key)
    expect = b"".join(c.content for c in crashed.chunks)
    record["steps"]["recovery"] = {
        "report": report.to_dict(),
        "chunk_files_before": files_before,
        "chunk_files_after": files_after,
        "read_after_recovery_match": reread.data == expect,
    }

    record["steps"]["clock_watermark"] = svc.db.watermark()
    svc.close()
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", default="samples")
    parser.add_argument("--workdir", default="/tmp/chunkmeta-demo")
    parser.add_argument("--record", default=None)
    args = parser.parse_args()
    record = run_demo(args.samples, args.workdir)
    text = json.dumps(record, indent=2, ensure_ascii=False)
    print(text)
    out = args.record or str(Path(args.workdir) / "run_record.json")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
