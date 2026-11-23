"""业务要求6: 静默窗口与事件簇状态演进; 状态只由持久化状态裁决。"""

from alertcorr.engine import Engine


def _web_cluster(dump, ordinal=0):
    return dump["clusters"]["web~http_5xx~%04d" % ordinal]


def test_add_window_suppresses_and_changes_representative(
        loaded_engine, patches):
    before = loaded_engine.dump()
    assert _web_cluster(before)["representative"] == 8
    loaded_engine.apply_patch(patches[0])
    loaded_engine.apply_patch(patches[1])
    after = loaded_engine.dump()
    cluster = _web_cluster(after)
    assert cluster["suppressed"] == [8, 9]
    assert cluster["status"] == "suppressed"
    assert cluster["representative"] is None


def test_remove_window_readjudicates_by_logical_clock(
        loaded_engine, patches):
    loaded_engine.apply_patch(patches[0])
    loaded_engine.apply_patch(patches[1])
    suppressed = _web_cluster(loaded_engine.dump())
    assert suppressed["status"] == "suppressed"
    loaded_engine.apply_patch(patches[2])
    restored = _web_cluster(loaded_engine.dump())
    assert restored["status"] == "active"
    assert restored["suppressed"] == [9]
    assert restored["representative"] == 8


def test_state_from_store_not_memory(engine, topo_dict, events_list,
                                     patches):
    engine.load_topology(topo_dict)
    engine.ingest(events_list)
    engine.run()
    engine.apply_patch(patches[0])
    engine.apply_patch(patches[1])
    db_path = engine.store.db_path
    expected = engine.dump()
    engine.close()

    restarted = Engine(db_path)
    try:
        assert restarted.dump()["sha"] == expected["sha"]
        restarted.apply_patch(patches[2])
        cluster = _web_cluster(restarted.dump())
        assert cluster["status"] == "active"
        assert cluster["representative"] == 8
    finally:
        restarted.close()
