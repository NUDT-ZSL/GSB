"""Child process that simulates a crash (SIGKILL-like os._exit) mid-operation.

Modes:
  torn-commit <db>: persist a versions row with committed=0 plus its objects
                    (a torn two-phase commit), then die without moving the
                    current-version pointer.
  mid-mirror <db>:  persist a mirror record in state 'started', then die
                    before completion.
Usage: python scripts/crash_child.py <mode> <db_path>
"""

import os
import sqlite3
import sys


def torn_commit(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT value FROM meta WHERE key='current_version'").fetchone()
    current = int(row[0]) if row else 0
    torn = current + 1
    conn.execute(
        "INSERT INTO versions(version, patch_id, committed, created_ms) VALUES (?,?,0,?)",
        (torn, "patch-torn-crash", 0))
    conn.execute(
        "INSERT INTO objects(version, kind, id, body) VALUES (?,?,?,?)",
        (torn, "upstream_member", "m-torn",
         '{"kind":"upstream_member","id":"m-torn","spec":{"host":"127.0.0.1","port":9999,"weight":1}}'))
    conn.execute("COMMIT")  # torn state is durable: committed=0, pointer not moved
    conn.close()
    os._exit(1)  # hard kill, no cleanup


def mid_mirror(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute(
        "INSERT INTO mirrors(request_seq, version, state, body) VALUES (?,?,?,?)",
        (999001, 1, "started",
         '{"request_seq":999001,"version":1,"policy_id":"mp-main",'
         '"shadow_member_id":"s-a","started_ms":0}'))
    conn.execute("COMMIT")
    conn.close()
    os._exit(1)  # killed before mirror_finish


if __name__ == "__main__":
    mode, db = sys.argv[1], sys.argv[2]
    if mode == "torn-commit":
        torn_commit(db)
    elif mode == "mid-mirror":
        mid_mirror(db)
    else:
        sys.exit("unknown mode %s" % mode)
