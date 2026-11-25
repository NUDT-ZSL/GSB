from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional

from .errors import ErrorCode, LayerForgeError


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS build_pointer (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  current_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
  version INTEGER PRIMARY KEY,
  status TEXT NOT NULL,
  clock_ms INTEGER NOT NULL,
  report_json TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT 'layerforge'
);
CREATE TABLE IF NOT EXISTS clock_watermark (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  watermark_ms INTEGER NOT NULL,
  tolerance_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
  step_id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL,
  phase TEXT NOT NULL,
  parent_digest TEXT,
  layer_digest TEXT,
  cache_key TEXT,
  selected_source TEXT,
  generation INTEGER,
  object_reused INTEGER,
  owner TEXT,
  updated_clock_ms INTEGER NOT NULL,
  outcome_json TEXT
);
CREATE TABLE IF NOT EXISTS materializations (
  step_id TEXT NOT NULL,
  parent_digest TEXT NOT NULL,
  layer_digest TEXT NOT NULL,
  status TEXT NOT NULL,
  object_path TEXT,
  object_reused INTEGER,
  owner TEXT,
  updated_clock_ms INTEGER NOT NULL,
  PRIMARY KEY (step_id, parent_digest)
);
CREATE TABLE IF NOT EXISTS layer_objects (
  digest TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  size INTEGER NOT NULL,
  committed_clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cache_entries (
  cache_key TEXT PRIMARY KEY,
  step_id TEXT NOT NULL,
  layer_digest TEXT NOT NULL,
  source_version TEXT NOT NULL,
  generation INTEGER NOT NULL,
  verdict TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  committed_clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invalidations (
  chain_id TEXT PRIMARY KEY,
  trigger_source TEXT NOT NULL,
  root_steps_json TEXT NOT NULL,
  affected_steps_json TEXT NOT NULL,
  generations_json TEXT NOT NULL,
  clock_ms INTEGER NOT NULL,
  report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  round_no INTEGER NOT NULL,
  state_json TEXT NOT NULL,
  clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_no INTEGER NOT NULL,
  step_id TEXT,
  category TEXT NOT NULL,
  reason TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category TEXT NOT NULL,
  subject TEXT,
  payload_json TEXT NOT NULL,
  clock_ms INTEGER NOT NULL
);
"""


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def loads(text: str) -> Any:
    return json.loads(text)


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO build_pointer(id, current_version) VALUES (1, 0)"
            )
        finally:
            conn.close()

    def initialize_clock(self, watermark_ms: int, tolerance_ms: int) -> None:
        with self.transaction() as conn:
            row = conn.execute("SELECT watermark_ms FROM clock_watermark WHERE id=1").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO clock_watermark(id, watermark_ms, tolerance_ms) VALUES (1, ?, ?)",
                    (int(watermark_ms), int(tolerance_ms)),
                )
            else:
                conn.execute(
                    "UPDATE clock_watermark SET tolerance_ms=? WHERE id=1",
                    (int(tolerance_ms),),
                )

    def clock_row(self) -> Optional[Dict[str, int]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT watermark_ms, tolerance_ms FROM clock_watermark WHERE id=1"
            ).fetchone()
            return dict(row) if row else None

    def save_clock(self, watermark_ms: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE clock_watermark SET watermark_ms=? WHERE id=1",
                (int(watermark_ms),),
            )

    def pointer_version(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT current_version FROM build_pointer WHERE id=1").fetchone()
            return int(row[0])

    def commit_snapshot(
        self,
        *,
        expected_version: int,
        status: str,
        clock_ms: int,
        report: Mapping[str, Any],
        mutation: Optional[Callable[[sqlite3.Connection], None]] = None,
    ) -> int:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT current_version FROM build_pointer WHERE id=1"
            ).fetchone()
            current = int(row[0])
            if current != expected_version:
                raise LayerForgeError(
                    "build snapshot version changed before commit",
                    code=ErrorCode.VERSION_CONFLICT,
                    details={"expected_version": expected_version, "current_version": current},
                )
            if mutation is not None:
                mutation(conn)
            new_version = current + 1
            conn.execute(
                "INSERT INTO snapshots(version, status, clock_ms, report_json) VALUES (?, ?, ?, ?)",
                (new_version, status, int(clock_ms), json.dumps(report, sort_keys=True)),
            )
            conn.execute(
                "UPDATE build_pointer SET current_version=? WHERE id=1",
                (new_version,),
            )
            return new_version

    def upsert_step(self, conn: sqlite3.Connection, step: Mapping[str, Any]) -> None:
        outcome = step.get("outcome")
        conn.execute(
            """
            INSERT INTO steps(step_id, seq, phase, parent_digest, layer_digest, cache_key,
                              selected_source, generation, object_reused, owner,
                              updated_clock_ms, outcome_json)
            VALUES (:step_id,:seq,:phase,:parent_digest,:layer_digest,:cache_key,
                    :selected_source,:generation,:object_reused,:owner,
                    :updated_clock_ms,:outcome_json)
            ON CONFLICT(step_id) DO UPDATE SET
              phase=excluded.phase,
              parent_digest=excluded.parent_digest,
              layer_digest=excluded.layer_digest,
              cache_key=excluded.cache_key,
              selected_source=excluded.selected_source,
              generation=excluded.generation,
              object_reused=excluded.object_reused,
              owner=excluded.owner,
              updated_clock_ms=excluded.updated_clock_ms,
              outcome_json=excluded.outcome_json
            """,
            {
                "step_id": step["step_id"],
                "seq": int(step["seq"]),
                "phase": step["phase"],
                "parent_digest": step.get("parent_digest"),
                "layer_digest": step.get("layer_digest"),
                "cache_key": step.get("cache_key"),
                "selected_source": step.get("selected_source"),
                "generation": step.get("generation"),
                "object_reused": None
                if step.get("object_reused") is None
                else int(bool(step["object_reused"])),
                "owner": step.get("owner", "local"),
                "updated_clock_ms": int(step["clock_ms"]),
                "outcome_json": json.dumps(outcome, sort_keys=True) if outcome is not None else None,
            },
        )

    def claim_materialization(
        self,
        *,
        step_id: str,
        parent_digest: str,
        layer_digest: str,
        owner: str,
        clock_ms: int,
    ) -> str:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM materializations WHERE step_id=? AND parent_digest=?",
                (step_id, parent_digest),
            ).fetchone()
            if row is not None and row["status"] == "committed":
                return "already_committed"
            conn.execute(
                """
                INSERT INTO materializations(step_id, parent_digest, layer_digest, status, owner, updated_clock_ms)
                VALUES (?, ?, ?, 'materializing', ?, ?)
                ON CONFLICT(step_id, parent_digest) DO UPDATE SET
                  layer_digest=excluded.layer_digest,
                  status='materializing',
                  owner=excluded.owner,
                  updated_clock_ms=excluded.updated_clock_ms
                """,
                (step_id, parent_digest, layer_digest, owner, int(clock_ms)),
            )
            return "claimed"

    def commit_materialization(
        self,
        *,
        step_id: str,
        parent_digest: str,
        layer_digest: str,
        object_path: str,
        object_reused: bool,
        size: int,
        clock_ms: int,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE materializations
                SET status='committed', layer_digest=?, object_path=?, object_reused=?, updated_clock_ms=?
                WHERE step_id=? AND parent_digest=?
                """,
                (
                    layer_digest,
                    object_path,
                    int(object_reused),
                    int(clock_ms),
                    step_id,
                    parent_digest,
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO layer_objects(digest, path, size, committed_clock_ms)
                VALUES (?, ?, ?, ?)
                """,
                (layer_digest, object_path, int(size), int(clock_ms)),
            )

    def has_committed_materialization(self, step_id: str, parent_digest: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM materializations WHERE step_id=? AND parent_digest=? AND status='committed'",
                (step_id, parent_digest),
            ).fetchone()
            return row is not None

    def cache_key_exists(self, cache_key: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM cache_entries WHERE cache_key=?", (cache_key,)
            ).fetchone()
            return row is not None

    def commit_cache_entry(
        self,
        *,
        cache_key: str,
        step_id: str,
        layer_digest: str,
        source_version: str,
        generation: int,
        verdict: str,
        evidence: Mapping[str, Any],
        clock_ms: int,
    ) -> bool:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM cache_entries WHERE cache_key=?", (cache_key,)
            ).fetchone()
            if row is not None:
                return False
            conn.execute(
                """
                INSERT INTO cache_entries(cache_key, step_id, layer_digest, source_version,
                                          generation, verdict, evidence_json, committed_clock_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    step_id,
                    layer_digest,
                    source_version,
                    int(generation),
                    verdict,
                    json.dumps(evidence, sort_keys=True),
                    int(clock_ms),
                ),
            )
            return True

    def save_invalidation(self, chain: Mapping[str, Any]) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO invalidations(chain_id, trigger_source, root_steps_json,
                  affected_steps_json, generations_json, clock_ms, report_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chain["chain_id"],
                    json.dumps(chain["trigger_source"], sort_keys=True),
                    json.dumps(chain["root_steps"], sort_keys=True),
                    json.dumps(chain["affected_steps"], sort_keys=True),
                    json.dumps(chain["step_generations"], sort_keys=True),
                    int(chain["clock_ms"]),
                    json.dumps(chain, sort_keys=True),
                ),
            )

    def save_schedule_state(self, state: Mapping[str, Any]) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO schedule_state(id, round_no, state_json, clock_ms)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  round_no=excluded.round_no,
                  state_json=excluded.state_json,
                  clock_ms=excluded.clock_ms
                """,
                (
                    int(state["round_no"]),
                    json.dumps(state, sort_keys=True),
                    int(state["clock_ms"]),
                ),
            )
            for event in state.get("events", []):
                conn.execute(
                    """
                    INSERT INTO schedule_events(round_no, step_id, category, reason, detail_json, clock_ms)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(state["round_no"]),
                        event.get("step_id"),
                        event["category"],
                        event["reason"],
                        json.dumps(event.get("detail", {}), sort_keys=True),
                        int(event.get("clock_ms", state["clock_ms"])),
                    ),
                )

    def add_ledger(
        self,
        *,
        category: str,
        payload: Mapping[str, Any],
        clock_ms: int,
        subject: Optional[str] = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO ledger(category, subject, payload_json, clock_ms) VALUES (?, ?, ?, ?)",
                (category, subject, json.dumps(payload, sort_keys=True), int(clock_ms)),
            )

    def status(self) -> Dict[str, Any]:
        with self.connect() as conn:
            pointer = conn.execute("SELECT current_version FROM build_pointer WHERE id=1").fetchone()
            clock = conn.execute("SELECT * FROM clock_watermark WHERE id=1").fetchone()
            steps = [dict(row) for row in conn.execute("SELECT * FROM steps ORDER BY seq").fetchall()]
            for step in steps:
                if step.get("outcome_json"):
                    step["outcome"] = json.loads(step.pop("outcome_json"))
                else:
                    step.pop("outcome_json", None)
            schedule = conn.execute("SELECT * FROM schedule_state WHERE id=1").fetchone()
            invalidations = [
                json.loads(row["report_json"])
                for row in conn.execute("SELECT report_json FROM invalidations ORDER BY clock_ms").fetchall()
            ]
            cache_count = conn.execute("SELECT COUNT(*) AS n FROM cache_entries").fetchone()["n"]
            object_count = conn.execute("SELECT COUNT(*) AS n FROM layer_objects").fetchone()["n"]
            return {
                "snapshot_version": int(pointer["current_version"]) if pointer else 0,
                "clock": dict(clock) if clock else None,
                "steps": steps,
                "schedule": json.loads(schedule["state_json"]) if schedule else None,
                "invalidations": invalidations,
                "committed_cache_entry_count": int(cache_count),
                "committed_object_count": int(object_count),
            }

    def latest_snapshot_report(self) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT report_json FROM snapshots
                WHERE version=(SELECT current_version FROM build_pointer WHERE id=1)
                """
            ).fetchone()
            return json.loads(row["report_json"]) if row else None
