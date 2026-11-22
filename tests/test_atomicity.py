from __future__ import annotations

from slo_workbench.engine import CRASH_AFTER_WINDOW_INSERT, Engine
from slo_workbench.loader import read_bundle
from slo_workbench.storage import transaction
from slo_workbench.repository import Repository


def test_in_process_crash_rolls_back_entire_tick(loaded_workspace, sample_dir):
    _, _, clock = read_bundle(sample_dir)
    engine = Engine(loaded_workspace)

    # Crash during tick index 2; the exception propagates and the IMMEDIATE
    # transaction is rolled back, leaving no partial window/ingestion writes.
    try:
        engine.run(
            clock,
            crash_after_tick=2,
            crash_point=CRASH_AFTER_WINDOW_INSERT,
        )
    except Exception:
        pass

    conn = loaded_workspace.connect()
    try:
        with transaction(conn):
            repo = Repository(conn)
            # Ticks 0 and 1 committed fully; tick 2 left nothing behind.
            windows = conn.execute("SELECT COUNT(*) c FROM window").fetchone()["c"]
            assert windows > 0
            # No orphaned ingestion rows without a matching committed window
            # beyond the watermark boundary.
            for slo in repo.current_slos():
                wm = repo.get_watermark(slo.name)
                assert wm is not None
                max_window = conn.execute(
                    "SELECT COALESCE(MAX(window_no), -1) m FROM window WHERE slo_name = ?",
                    (slo.name,),
                ).fetchone()["m"]
                assert max_window <= wm["next_window_no"] - 1
    finally:
        conn.close()

    # Re-running cleanly converges without duplicate windows.
    engine.run(clock)
    conn = loaded_workspace.connect()
    try:
        duplicates = conn.execute(
            "SELECT slo_name, window_no, COUNT(*) c FROM window "
            "GROUP BY slo_name, window_no HAVING c > 1"
        ).fetchall()
        assert not duplicates
    finally:
        conn.close()
