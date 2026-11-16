def test_upsert_idempotent_and_keeps_latest(store):
    tid = store.create_task({})
    store.upsert_item(tid, "TK-1", "v1", "ok", {"a": 1}, {"a": 1}, "run-1")
    store.upsert_item(tid, "TK-1", "v1", "ok", {"a": 2}, {"a": 2}, "run-2")
    s = store.summary(tid)
    assert s["stored_items"] == 1 and s["distinct_items"] == 1
    with store._conn() as c:
        row = c.execute("SELECT source, raw_json FROM items").fetchone()
    assert row["source"] == "run-2" and '"a": 2' in row["raw_json"]


def test_business_key_includes_version(store):
    tid = store.create_task({})
    store.upsert_item(tid, "TK-1", "v1", "ok", {}, {}, "r")
    store.upsert_item(tid, "TK-1", "v2", "ok", {}, {}, "r")
    assert store.summary(tid)["stored_items"] == 2
    assert store.summary(tid)["distinct_items"] == 1


def test_checkpoints_and_cursor(store):
    tid = store.create_task({})
    store.add_checkpoint(tid, "TK-1", "v1")
    store.add_checkpoint(tid, "TK-1", "v1")
    store.add_checkpoint(tid, "TK-2", "v2")
    assert store.load_checkpoints(tid) == {"TK-1", "TK-2"}
    assert store.get_cursor(tid) == 1
    store.set_cursor(tid, 3)
    assert store.get_cursor(tid) == 3
