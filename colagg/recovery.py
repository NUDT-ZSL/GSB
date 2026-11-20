from __future__ import annotations

import sqlite3
from typing import Any

from .models import Plan
from .spill import SpillManager
from .storage import Store, compute_state_digest


def recover_orphans(store: Store, plan: Plan) -> dict[str, Any]:
    spill = SpillManager(store.root, plan.config.spill_dir)
    with store.transaction():
        referenced_rows = store.conn.execute(
            """
            SELECT s.spill_id, s.rel_path
            FROM spill_files AS s
            JOIN group_states AS g ON g.spill_id = s.spill_id
            WHERE g.mode = ? AND g.status = 'active'
            ORDER BY s.spill_id
            """,
            (plan.config.numeric_mode,),
        ).fetchall()
        referenced_paths = {str(row["rel_path"]) for row in referenced_rows}
        referenced_ids = {str(row["spill_id"]) for row in referenced_rows}
        cleanup = spill.cleanup_orphans(referenced_paths)
        removed_rows = 0
        rows = store.conn.execute(
            "SELECT spill_id FROM spill_files WHERE mode = ?",
            (plan.config.numeric_mode,),
        ).fetchall()
        for row in rows:
            if str(row["spill_id"]) not in referenced_ids:
                store.conn.execute(
                    "DELETE FROM spill_files WHERE spill_id = ?",
                    (row["spill_id"],),
                )
                removed_rows += 1
        store.set_meta("state_digest", compute_state_digest(store.conn))
    return {
        "referenced_spill_files": len(referenced_ids),
        "removed_spill_manifest_rows": removed_rows,
        **cleanup,
    }
