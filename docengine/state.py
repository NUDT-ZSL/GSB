"""Persistent engine state in SQLite (WAL mode).

Everything that must survive a hard kill lives here and is written in single
atomic transactions: the index generation pointer (swapped wholesale via a
staging table plus a versioned pointer row), the committed query batch
watermark and per-batch result digests, fragment commit state per
generation, and the logical clock watermark. Recovery distinguishes
committed from half-committed steps by status flags and only replays
incomplete ones."""

import os
import sqlite3

from .errors import BusinessError, ErrorCode

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS index_pointer (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    generation INTEGER NOT NULL,
    snapshot_digest TEXT NOT NULL,
    clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS index_generations (
    generation INTEGER PRIMARY KEY,
    snapshot_digest TEXT NOT NULL,
    merge_digest TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_staging (
    generation INTEGER PRIMARY KEY,
    snapshot_digest TEXT NOT NULL,
    merge_digest TEXT NOT NULL,
    clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fragment_commits (
    checksum TEXT NOT NULL,
    generation INTEGER NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (checksum, generation)
);
CREATE TABLE IF NOT EXISTS batches (
    batch_no INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    status TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    templates_json TEXT NOT NULL,
    scan_digest TEXT,
    fetched INTEGER,
    failed INTEGER,
    result_digest TEXT,
    results_json TEXT,
    clock_ms INTEGER NOT NULL,
    PRIMARY KEY (batch_no, generation)
);
"""

CLOCK_KEY = "clock_watermark_ms"
BATCH_KEY = "batch_watermark"


class FileLock:
    """Advisory inter-process file lock (fcntl on POSIX, msvcrt on Windows)."""

    def __init__(self, path):
        self.path = path
        self._fh = None

    def __enter__(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        self._fh = open(self.path, "a+b")
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except ImportError:
            import msvcrt

            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
        return self

    def __exit__(self, *exc):
        try:
            try:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except ImportError:
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._fh.close()
        return False


class StateStore:
    def __init__(self, db_path):
        directory = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(directory, exist_ok=True)
        self.con = sqlite3.connect(db_path, isolation_level=None)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=FULL")
        self.con.executescript(SCHEMA)

    def close(self):
        self.con.close()

    def check_and_advance_clock(self, clock_ms, tolerance_ms):
        """Reject logical clock regression beyond tolerance; otherwise raise
        the watermark. Must be called inside a write transaction."""
        row = self.con.execute(
            "SELECT value FROM state_meta WHERE key = ?", (CLOCK_KEY,)
        ).fetchone()
        watermark = int(row[0]) if row else 0
        if clock_ms < watermark - tolerance_ms:
            raise BusinessError(
                ErrorCode.E_CLOCK_REGRESSION,
                "logical clock regressed beyond tolerance",
                provided_clock_ms=clock_ms,
                watermark_ms=watermark,
                tolerance_ms=tolerance_ms,
            )
        self.con.execute(
            "INSERT INTO state_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (CLOCK_KEY, str(max(clock_ms, watermark))),
        )
        return watermark

    def clock_watermark(self):
        row = self.con.execute(
            "SELECT value FROM state_meta WHERE key = ?", (CLOCK_KEY,)
        ).fetchone()
        return int(row[0]) if row else 0

    def current_pointer(self):
        row = self.con.execute(
            "SELECT generation, snapshot_digest, clock_ms "
            "FROM index_pointer WHERE id = 1"
        ).fetchone()
        if not row:
            return None
        return {
            "generation": row[0],
            "snapshot_digest": row[1],
            "clock_ms": row[2],
        }

    def committed_generation_for(self, snapshot_digest):
        row = self.con.execute(
            "SELECT generation FROM index_generations "
            "WHERE snapshot_digest = ? AND status = 'committed'",
            (snapshot_digest,),
        ).fetchone()
        return row[0] if row else None

    def commit_generation(
        self, snapshot_digest, merge_digest, fragment_checksums, clock_ms,
        tolerance_ms,
    ):
        """Atomically stage and swap the index pointer to a new generation.
        Idempotent: the same snapshot digest is only ever committed once."""
        self.con.execute("BEGIN IMMEDIATE")
        try:
            existing = self.committed_generation_for(snapshot_digest)
            if existing is not None:
                self.con.execute("ROLLBACK")
                return {"generation": existing, "idempotent": True}
            self.check_and_advance_clock(clock_ms, tolerance_ms)
            row = self.con.execute(
                "SELECT COALESCE(MAX(generation), 0) + 1 FROM index_generations"
            ).fetchone()
            generation = row[0]
            self.con.execute(
                "INSERT INTO merge_staging(generation, snapshot_digest, "
                "merge_digest, clock_ms) VALUES(?, ?, ?, ?)",
                (generation, snapshot_digest, merge_digest, clock_ms),
            )
            self.con.execute(
                "INSERT INTO index_generations(generation, snapshot_digest, "
                "merge_digest, clock_ms, status) "
                "VALUES(?, ?, ?, ?, 'committed')",
                (generation, snapshot_digest, merge_digest, clock_ms),
            )
            for checksum in sorted(fragment_checksums):
                self.con.execute(
                    "INSERT OR REPLACE INTO fragment_commits(checksum, "
                    "generation, status) VALUES(?, ?, 'committed')",
                    (checksum, generation),
                )
            self.con.execute(
                "INSERT INTO index_pointer(id, generation, snapshot_digest, "
                "clock_ms) VALUES(1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "generation = excluded.generation,"
                " snapshot_digest = excluded.snapshot_digest,"
                " clock_ms = excluded.clock_ms",
                (generation, snapshot_digest, clock_ms),
            )
            self.con.execute(
                "DELETE FROM merge_staging WHERE generation = ?", (generation,)
            )
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        return {"generation": generation, "idempotent": False}

    def stage_generation_crash(self, snapshot_digest, merge_digest, clock_ms):
        """Test hook: persist a half-committed merge (staging row plus a
        'merging' generation) without swapping the pointer, simulating a
        hard kill in the middle of a merge commit."""
        self.con.execute("BEGIN IMMEDIATE")
        try:
            row = self.con.execute(
                "SELECT COALESCE(MAX(generation), 0) + 1 FROM index_generations"
            ).fetchone()
            generation = row[0]
            self.con.execute(
                "INSERT INTO merge_staging(generation, snapshot_digest, "
                "merge_digest, clock_ms) VALUES(?, ?, ?, ?)",
                (generation, snapshot_digest, merge_digest, clock_ms),
            )
            self.con.execute(
                "INSERT INTO index_generations(generation, snapshot_digest, "
                "merge_digest, clock_ms, status) "
                "VALUES(?, ?, ?, ?, 'merging')",
                (generation, snapshot_digest, merge_digest, clock_ms),
            )
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        return generation

    def batch_watermark(self):
        row = self.con.execute(
            "SELECT value FROM state_meta WHERE key = ?", (BATCH_KEY,)
        ).fetchone()
        return int(row[0]) if row else 0

    def get_batch(self, batch_no, generation):
        row = self.con.execute(
            "SELECT status, request_digest, templates_json, results_json "
            "FROM batches WHERE batch_no = ? AND generation = ?",
            (batch_no, generation),
        ).fetchone()
        if not row:
            return None
        return {
            "status": row[0],
            "request_digest": row[1],
            "templates_json": row[2],
            "results_json": row[3],
        }

    def begin_batch(self, batch_no, generation, request_digest,
                    templates_json, clock_ms, tolerance_ms):
        """Persist batch intent. Returns 'started' or 'already_committed'."""
        self.con.execute("BEGIN IMMEDIATE")
        try:
            existing = self.get_batch(batch_no, generation)
            if existing is not None:
                if (
                    existing["status"] == "committed"
                    and existing["request_digest"] == request_digest
                ):
                    self.con.execute("ROLLBACK")
                    return "already_committed"
                if existing["status"] == "committed":
                    self.con.execute("ROLLBACK")
                    raise BusinessError(
                        ErrorCode.E_BATCH_CONFLICT,
                        "batch number already committed with a different "
                        "request",
                        batch_no=batch_no,
                        generation=generation,
                    )
                # a 'running' row from a crashed process: take it over
                self.con.execute(
                    "DELETE FROM batches WHERE batch_no = ? AND generation = ?",
                    (batch_no, generation),
                )
            self.check_and_advance_clock(clock_ms, tolerance_ms)
            self.con.execute(
                "INSERT INTO batches(batch_no, generation, status, "
                "request_digest, templates_json, clock_ms) "
                "VALUES(?, ?, 'running', ?, ?, ?)",
                (batch_no, generation, request_digest, templates_json,
                 clock_ms),
            )
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        return "started"

    def commit_batch(self, batch_no, generation, scan_digest, fetched, failed,
                     result_digest, results_json, clock_ms, tolerance_ms):
        """Atomically mark the batch committed and advance the watermark.
        Idempotent for an identical result digest."""
        self.con.execute("BEGIN IMMEDIATE")
        try:
            existing = self.get_batch(batch_no, generation)
            if existing is not None and existing["status"] == "committed":
                row = self.con.execute(
                    "SELECT result_digest FROM batches "
                    "WHERE batch_no = ? AND generation = ?",
                    (batch_no, generation),
                ).fetchone()
                self.con.execute("ROLLBACK")
                if row[0] == result_digest:
                    return "idempotent"
                raise BusinessError(
                    ErrorCode.E_BATCH_CONFLICT,
                    "batch already committed with a different result",
                    batch_no=batch_no,
                    generation=generation,
                )
            self.check_and_advance_clock(clock_ms, tolerance_ms)
            self.con.execute(
                "UPDATE batches SET status = 'committed', scan_digest = ?, "
                "fetched = ?, failed = ?, result_digest = ?, results_json = ? "
                "WHERE batch_no = ? AND generation = ?",
                (scan_digest, fetched, failed, result_digest, results_json,
                 batch_no, generation),
            )
            watermark = self.batch_watermark()
            if batch_no > watermark:
                self.con.execute(
                    "INSERT INTO state_meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (BATCH_KEY, str(batch_no)),
                )
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        return "committed"

    def incomplete_steps(self):
        merging = [
            row[0]
            for row in self.con.execute(
                "SELECT generation FROM index_generations "
                "WHERE status = 'merging' ORDER BY generation"
            )
        ]
        running = [
            {
                "batch_no": row[0],
                "generation": row[1],
                "templates_json": row[2],
                "clock_ms": row[3],
            }
            for row in self.con.execute(
                "SELECT batch_no, generation, templates_json, clock_ms "
                "FROM batches WHERE status = 'running' "
                "ORDER BY batch_no, generation"
            )
        ]
        return {"merging_generations": merging, "running_batches": running}

    def discard_half_merges(self):
        self.con.execute("BEGIN IMMEDIATE")
        try:
            self.con.execute("DELETE FROM merge_staging")
            self.con.execute(
                "DELETE FROM fragment_commits WHERE generation IN "
                "(SELECT generation FROM index_generations "
                "WHERE status = 'merging')"
            )
            self.con.execute(
                "DELETE FROM index_generations WHERE status = 'merging'"
            )
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise

    def fragment_stats(self):
        rows = self.con.execute(
            "SELECT status, COUNT(*) FROM fragment_commits GROUP BY status "
            "ORDER BY status"
        ).fetchall()
        return {status: count for status, count in rows}

    def fragments_for_generation(self, generation):
        return [
            row[0]
            for row in self.con.execute(
                "SELECT checksum FROM fragment_commits "
                "WHERE generation = ? AND status = 'committed' "
                "ORDER BY checksum",
                (generation,),
            )
        ]

    def status_report(self):
        pointer = self.current_pointer()
        generations = [
            {
                "generation": row[0],
                "snapshot_digest": row[1],
                "status": row[2],
                "clock_ms": row[3],
            }
            for row in self.con.execute(
                "SELECT generation, snapshot_digest, status, clock_ms "
                "FROM index_generations ORDER BY generation"
            )
        ]
        batches = [
            {
                "batch_no": row[0],
                "generation": row[1],
                "status": row[2],
                "fetched": row[3],
                "failed": row[4],
                "result_digest": row[5],
            }
            for row in self.con.execute(
                "SELECT batch_no, generation, status, fetched, failed, "
                "result_digest FROM batches ORDER BY batch_no, generation"
            )
        ]
        return {
            "pointer": pointer,
            "batch_watermark": self.batch_watermark(),
            "clock_watermark_ms": self.clock_watermark(),
            "generations": generations,
            "batches": batches,
            "fragment_stats": self.fragment_stats(),
        }
