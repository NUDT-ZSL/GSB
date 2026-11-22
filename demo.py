"""可重复演示：合成、跨版本迁移、增量失效传播与追溯重放。"""

import copy
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scripts.gen_samples import generate, load_data
from tts_app.adjudicate import adjudicate, cross_dialect_pcm_check
from tts_app.cache import ContentCache
from tts_app.config import load_config_file
from tts_app.dag import Engine, Limits
from tts_app.errors import TtsError
from tts_app.lineage import verify_lineage
from tts_app.migrate import migrate
from tts_app.snapshot import SnapshotStore

WS = "workspace_demo"


def section(title):
    print("\n" + "=" * 64)
    print(" " + title)
    print("=" * 64)


def main():
    shutil.rmtree(WS, ignore_errors=True)
    os.makedirs(WS)
    data_dir = os.path.join(WS, "data")
    generate(data_dir)
    speakers, tasks = load_data(data_dir)
    report = {"checks": []}

    def ok(name, detail=""):
        report["checks"].append({"name": name, "ok": True, "detail": detail})
        print("  [OK] %s %s" % (name, detail))

    section("1. 确定性四阶段合成（同 seed 同配置逐字节一致）")
    cfg = load_config_file("configs/config_v2.txt")
    cache = ContentCache(os.path.join(WS, "cache"))
    lineage_dir = os.path.join(WS, "lineage")
    engine = Engine(cache, speakers, cfg, lineage_dir=lineage_dir)
    run1 = engine.run_all(tasks)
    pcm_a = {u: r["pcm_sha256"] for u, r in run1["results"].items()}
    engine2 = Engine(ContentCache(os.path.join(WS, "cache2")), speakers, cfg)
    run2 = engine2.run_all(tasks)
    pcm_b = {u: r["pcm_sha256"] for u, r in run2["results"].items()}
    assert pcm_a == pcm_b, "确定性校验失败"
    ok("determinism", "%d 条 utterance PCM 哈希一致" % len(pcm_a))
    ok("prosody_check", str(all(
        r["prosody_check"]["ok"] for r in run1["results"].values())))

    section("2. v1/v2/v3 读取与双向迁移")
    c1 = load_config_file("configs/config_v1.txt")
    c2 = load_config_file("configs/config_v2.txt")
    c3 = load_config_file("configs/config_v3.json")
    ok("readers", "来源版本: %s/%s/%s"
       % (c1["source_version"], c2["source_version"], c3["source_version"]))
    sparse = load_config_file("configs/config_v1_sparse.txt")
    upgraded, rec = migrate(sparse, "v3")
    ok("upgrade_fill", json.dumps(rec["filled_defaults"], ensure_ascii=False))
    drift_cfg = load_config_file("configs/config_v3_drift.json")
    _, drec = migrate(drift_cfg, "v1", rules={
        "style.inherit": "drop", "prosody.duration_curve": "flatten"})
    ok("downgrade_drop", json.dumps(drec["dropped"], ensure_ascii=False))

    section("3. 跨方言裁决（同一语义三种书写 -> 相同 PCM）")
    texts = {}
    for version, path in (("v1", "configs/config_v1.txt"),
                          ("v2", "configs/config_v2.txt"),
                          ("v3", "configs/config_v3.json")):
        with open(path, encoding="utf-8") as fh:
            texts[version] = fh.read()
    _, res = cross_dialect_pcm_check(texts, tasks[0], speakers)
    ok("cross_dialect_pcm", "utt_001 pcm_sha256=" + res["pcm_sha256"][:16])
    try:
        bad = dict(texts)
        with open("configs/config_v3_drift.json", encoding="utf-8") as fh:
            bad["v3"] = fh.read()
        adjudicate(bad)
        ok("semantic_drift", "未触发（异常）")
    except TtsError as exc:
        ok("semantic_drift", "%s unrecoverable=%s"
           % (exc.code, exc.details.get("unrecoverable")))

    section("4. 增量更新与依赖失效传播")
    spk2 = copy.deepcopy(speakers)
    spk2["spk_alpha"]["f0_base"] += 20.0
    spk2["spk_alpha"]["formants"][0] += 50.0
    engine_inc = Engine(cache, spk2, cfg, lineage_dir=lineage_dir)
    run_inc = engine_inc.run_all(tasks)
    affected = run_inc["affected_utterances"]
    ok("speaker_param_change", "受影响=%s" % ",".join(affected))
    ok("reuse", "复用节点 %d 个, 重算节点 %d 个"
       % (len(run_inc["reused_nodes"]), len(run_inc["recomputed_nodes"])))
    engine_full = Engine(ContentCache(os.path.join(WS, "cache3")), spk2, cfg)
    run_full = engine_full.run_all(tasks)
    same = all(run_inc["results"][u]["pcm"] == run_full["results"][u]["pcm"]
               for u in pcm_a)
    ok("incremental_equals_full", str(same))
    assert same
    cfg_v2 = copy.deepcopy(cfg)
    cfg_v2["vocoder"] = dict(cfg["vocoder"], version="griffin_v2",
                             params={"n_fft": 1024, "hop": 128})
    run_voc = Engine(cache, spk2, cfg_v2).run_all(tasks)
    ok("vocoder_bump", "受影响=%d 条（全部）"
       % len(run_voc["affected_utterances"]))

    section("5. 谱系追溯与过期结果拒绝")
    engine_ok = Engine(cache, spk2, cfg, lineage_dir=lineage_dir)
    engine_ok.run_all(tasks)
    v = verify_lineage(lineage_dir, "utt_003", engine_ok, tasks[2], cache)
    ok("lineage_replay", "utt_003 pcm_sha256=" + v["pcm_sha256"][:16])
    spk3 = copy.deepcopy(spk2)
    spk3["spk_alpha"]["f0_base"] += 5.0
    engine_stale = Engine(cache, spk3, cfg, lineage_dir=lineage_dir)
    try:
        verify_lineage(lineage_dir, "utt_003", engine_stale, tasks[2], cache)
        ok("lineage_broken", "未触发（异常）")
    except TtsError as exc:
        changed = [d["dependency"] for d in exc.details["changed_dependencies"]]
        ok("lineage_broken", "%s utt=%s 变化依赖=%s"
           % (exc.code, exc.details["utterance_id"], changed))

    section("6. 快照版本化替换 / 并发提交 / 资源上限")
    store = SnapshotStore(os.path.join(WS, "snapshots"))
    store.commit({"config": cfg, "stats": cache.stats()}, 1)
    store.commit({"config": cfg_v2, "stats": cache.stats()}, 2)
    ok("snapshot_replace", "当前版本=%d 校验和有效=%s"
       % (store.current()["version"], store.current()["checksum_valid"]))
    try:
        store.commit({"config": cfg}, 1)
    except TtsError as exc:
        ok("snapshot_rollback", exc.code)
    with store.stage_lock:
        try:
            with store.stage_lock:
                pass
        except TtsError as exc:
            ok("stage_busy", exc.code)
    limits = Limits(max_utterances_per_recompute=2)
    try:
        Engine(ContentCache(os.path.join(WS, "cache4")), speakers, cfg,
               limits=limits).run_all(tasks)
    except TtsError as exc:
        ok("resource_limit", "%s limit=%s" % (exc.code, exc.details["limit"]))

    section("演示完成")
    passed = sum(1 for c in report["checks"] if c["ok"])
    print(" 共 %d 项检查全部通过" % passed)
    report_path = os.path.join(WS, "demo_report.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print(" 报告已写入 %s" % report_path)


if __name__ == "__main__":
    main()
