"""可重复演示: 覆盖验收项的端到端脚本.

演示内容:
  1. 中心区域块优先预览 + 首块实际观测耗时;
  2. 分块生成与全局一次生成逐像素一致;
  3. 切换块网格重跑, 两次完整图像逐像素一致;
  4. 快照版本化提交 / 重启加载 / 损坏检测;
  5. 并发计划提交冲突 (E_STAGE_BUSY) 与并发预览;
  6. 图生图锚定: 偏差谱与主导偏差块.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from typing import Dict, List, Tuple

from .config import GenerationConfig
from .errors import E_STAGE_BUSY, GenError
from .service import GenerationService
from .snapshot import KIND_BLOCK_PLAN, KIND_READY_MANIFEST, SnapshotStore


def load_samples(sample_dir: str):
    cfg = GenerationConfig.load(os.path.join(sample_dir, "config.json"))
    with open(os.path.join(sample_dir, "descriptions.json"), encoding="utf-8") as fh:
        descriptions = json.load(fh)["descriptions"]
    tokens = list(descriptions[0]["tokens"])
    with open(os.path.join(sample_dir, "reference.json"), encoding="utf-8") as fh:
        ref_meta = json.load(fh)
    with open(os.path.join(sample_dir, ref_meta["file"]), "rb") as fh:
        ref_bytes = fh.read()
    anchor_ref = (ref_bytes, tuple(ref_meta["shape"]))
    return cfg, tokens, anchor_ref, float(cfg.anchor_strength)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def run_demo(sample_dir: str, workspace: str) -> Dict[str, object]:
    if os.path.exists(workspace):
        shutil.rmtree(workspace)
    os.makedirs(workspace, exist_ok=True)
    cfg, tokens, anchor_ref, anchor_strength = load_samples(sample_dir)
    store = SnapshotStore(os.path.join(workspace, "snapshots"))
    service = GenerationService(cfg, store, tokens)
    report: Dict[str, object] = {"checks": []}

    def check(name: str, ok: bool, detail: str) -> None:
        report["checks"].append({"name": name, "ok": ok, "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    # -- 运行 1: 裁决网格, 中心块优先, 图生图锚定 --
    result1 = service.run(anchor_ref=anchor_ref, anchor_strength=anchor_strength)
    plan1 = result1.plan
    ready = service.ready_blocks()
    center_bid = int(ready["ready"][0]["block_id"])
    center_preview = service.preview_block(center_bid)
    check(
        "首块耗时观测",
        0.0 < result1.first_block_ms <= cfg.latency_budget_ms,
        f"observed={result1.first_block_ms:.3f}ms budget={cfg.latency_budget_ms}ms "
        f"est={plan1['est_first_block_ms']}ms grid={plan1['grid']}",
    )
    check(
        "中心块优先预览",
        len(center_preview) > 0,
        f"center_block={center_bid} preview_bytes={len(center_preview)}",
    )
    check(
        "分块与全局一致",
        result1.consistency["verdict"] == "identical",
        f"tier={result1.consistency['tier']} mode={result1.consistency['mode']}",
    )
    anchor_rep = result1.anchor_report or {}
    dominant = (anchor_rep.get("dominant_block") or {}).get("block_id")
    check(
        "锚定偏差谱",
        anchor_rep.get("dominant_block") is not None,
        f"blocks={len(anchor_rep.get('spectrum', []))} dominant_block={dominant}",
    )

    # -- 运行 2: 切换块网格重跑, 两次完整图像逐像素一致 --
    grids = [g for g in cfg.grid_candidates if g != plan1["grid"]]
    grid2 = grids[-1]
    result2 = service.run(grid=grid2, anchor_ref=anchor_ref,
                          anchor_strength=anchor_strength)
    check(
        "切网格重跑逐像素一致",
        result1.image == result2.image,
        f"grid1={plan1['grid']} grid2={grid2} "
        f"sha1={_digest(result1.image)} sha2={_digest(result2.image)}",
    )

    # -- 快照: 重启加载与版本识别 --
    versions_before = dict(result2.versions)
    store2 = SnapshotStore(os.path.join(workspace, "snapshots"))
    loaded = store2.load_all()
    check(
        "快照重启加载",
        all(rec is not None for rec in loaded.values()),
        f"versions={versions_before}",
    )
    stale_rejected = False
    try:
        store2.submit(KIND_BLOCK_PLAN, 1, {"stale": True})
    except GenError:
        stale_rejected = True
    check("旧版本拒绝覆盖", stale_rejected, "submit(version=1) rejected")
    corrupt_detected = False
    corrupt_info = ""
    manifest_path = os.path.join(workspace, "snapshots", f"{KIND_READY_MANIFEST}.json")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        record = json.load(fh)
    record["checksum"] = "0" * 64
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    try:
        SnapshotStore(os.path.join(workspace, "snapshots")).load(KIND_READY_MANIFEST)
    except GenError as exc:
        corrupt_detected = exc.code == "E_SNAPSHOT_CORRUPT"
        corrupt_info = f"kind={exc.details.get('kind')} version={exc.details.get('version')}"
    check("快照损坏检测", corrupt_detected, corrupt_info)

    # -- 并发: 计划提交冲突 E_STAGE_BUSY + 并发预览 --
    import time as _time

    from .service import STAGE_DONE, STAGE_ERROR, STAGE_ITERATE

    service3 = GenerationService(
        cfg, SnapshotStore(os.path.join(workspace, "snap2")), tokens
    )
    errors: List[str] = []

    def worker(results: list) -> None:
        try:
            results.append(service3.run())
        except GenError as exc:
            errors.append(exc.code)

    r1: list = []
    t1 = threading.Thread(target=worker, args=(r1,))
    t1.start()
    deadline = _time.time() + 10.0
    while service3.stage != STAGE_ITERATE and _time.time() < deadline:
        _time.sleep(0.001)
    r2: list = []
    try:
        r2.append(service3.run())
    except GenError as exc:
        errors.append(exc.code)
    t1.join()
    assert service3.stage in (STAGE_DONE, STAGE_ERROR)
    check(
        "并发计划提交冲突",
        E_STAGE_BUSY in errors and len(r1) + len(r2) == 1,
        f"busy_errors={errors.count(E_STAGE_BUSY)} completed={len(r1) + len(r2)}",
    )
    done_result = (r1 + r2)[0]
    previews: List[bytes] = []

    def preview_worker(bid: int) -> None:
        previews.append(service3.preview_block(bid))

    threads = [
        threading.Thread(target=preview_worker, args=(i,))
        for i in range(int(done_result.plan["blocks"]))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(
        "并发块预览",
        len(previews) == int(done_result.plan["blocks"]),
        f"previews={len(previews)}",
    )

    report["image_sha256"] = _digest(result1.image)
    report["plan"] = plan1
    report["ok"] = all(c["ok"] for c in report["checks"])
    return report


if __name__ == "__main__":
    import sys

    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _samples = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_root, "samples")
    _workspace = (
        sys.argv[2] if len(sys.argv) > 2 else os.path.join(_root, "workspace", "demo")
    )
    _report = run_demo(_samples, _workspace)
    print(f"overall ok = {_report['ok']}")
    sys.exit(0 if _report["ok"] else 1)
