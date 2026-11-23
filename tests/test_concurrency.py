"""业务要求8: 多进程并发一致性与异常链路隔离。"""

import multiprocessing as mp

from alertcorr.concurrency import FileLock, run_isolated
from alertcorr.engine import Engine


def _worker_ingest(db, lock_path, topo, events, queue, label):
    def action():
        eng = Engine(db, lock=FileLock(lock_path))
        try:
            if eng.store.get_meta("topology_version") is None:
                eng.load_topology(topo)
            return eng.ingest(events)
        finally:
            eng.close()

    queue.put(run_isolated(action, label))


def _worker_patch(db, lock_path, patch, queue, label):
    def action():
        eng = Engine(db, lock=FileLock(lock_path))
        try:
            return eng.apply_patch(patch)
        finally:
            eng.close()

    queue.put(run_isolated(action, label))


def test_parallel_ingest_and_patch_idempotent(tmp_path, topo_dict,
                                              events_list, patches):
    db = str(tmp_path / "mp.db")
    lock_path = str(tmp_path / "mp.lock")
    setup = Engine(db)
    setup.load_topology(topo_dict)
    setup.close()

    core_events = [e for e in events_list
                   if e["instance_id"].split("-")[0] in ("db", "cache",
                                                         "api")]
    edge_events = [e for e in events_list
                   if e["instance_id"].split("-")[0] in ("web", "gw",
                                                         "batch")]
    poisoned = [dict(e, seq=9000, instance_id="ghost-9")
                for e in events_list[:1]]

    ctx = mp.get_context("fork")
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=_worker_ingest,
                    args=(db, lock_path, topo_dict, core_events, queue,
                          "core")),
        ctx.Process(target=_worker_ingest,
                    args=(db, lock_path, topo_dict, edge_events, queue,
                          "edge")),
        ctx.Process(target=_worker_ingest,
                    args=(db, lock_path, topo_dict, poisoned, queue,
                          "poison")),
        ctx.Process(target=_worker_patch,
                    args=(db, lock_path, patches[0], queue, "patch-a")),
        ctx.Process(target=_worker_patch,
                    args=(db, lock_path, patches[0], queue, "patch-b")),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    results = [queue.get() for _ in procs]
    by_label = {r["label"]: r for r in results}

    assert by_label["core"]["ok"]
    assert by_label["edge"]["ok"]
    assert not by_label["poison"]["ok"]
    assert by_label["poison"]["error"]["code"] == "E2006"

    patch_results = [by_label["patch-a"], by_label["patch-b"]]
    assert all(r["ok"] for r in patch_results)
    replays = [r["result"]["idempotent_replay"] for r in patch_results]
    assert sorted(replays) == [False, True]

    check = Engine(db)
    try:
        assert check.store.get_meta("topology_version") == "2"
        seqs = [ev.seq for ev in check.store.load_events()]
        assert len(seqs) == len(set(seqs)) == len(events_list)
        check.ingest(events_list)
        final = check.run()
        assert final["clusters"]
    finally:
        check.close()


def test_same_cluster_version_committed_once(engine, topo_dict,
                                             events_list):
    engine.load_topology(topo_dict)
    engine.ingest(events_list)
    engine.run()
    first = engine.dump()
    engine.run()
    second = engine.dump()
    assert first["sha"] == second["sha"]
    rows = engine.store.conn.execute(
        "SELECT COUNT(*) FROM journal WHERE kind='results'").fetchone()
    assert rows[0] == 1
