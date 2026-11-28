"""SQLite persistence for extraction versions (requirement 7).

Every successful pipeline run is stored as an immutable version row:
parameters, full report JSON and mesh file paths.  Versions are only
inserted after the pipeline completes, so failures never corrupt or
partially overwrite history.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    params_json TEXT NOT NULL,
    report_json TEXT NOT NULL,
    mesh_path TEXT NOT NULL,
    UNIQUE (asset, version)
);
"""


class Store:
    def __init__(self, db_path):
        self.db_path = db_path
        self.mesh_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)),
                                     "meshes")
        os.makedirs(self.mesh_dir, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def next_version(self, asset):
        row = self.conn.execute(
            "SELECT MAX(version) AS v FROM versions WHERE asset = ?",
            (asset,)).fetchone()
        return (row["v"] or 0) + 1

    def save_version(self, asset, params, report, meshes):
        """Persist a completed run; returns the new version number."""
        with self._lock:
            version = self.next_version(asset)
            mesh_path = os.path.join(self.mesh_dir, asset, f"v{version}.npz")
            os.makedirs(os.path.dirname(mesh_path), exist_ok=True)
            np.savez_compressed(
                mesh_path,
                vertices=meshes["vertices"], faces=meshes["faces"],
                simplified_vertices=meshes["simplified_vertices"],
                simplified_faces=meshes["simplified_faces"])
            self.conn.execute(
                "INSERT INTO versions (asset, version, created_at,"
                " params_json, report_json, mesh_path)"
                " VALUES (?,?,?,?,?,?)",
                (asset, version,
                 datetime.now(timezone.utc).isoformat(),
                 json.dumps(params), json.dumps(report), mesh_path))
            self.conn.commit()
            return version

    def list_versions(self, asset=None):
        sql = ("SELECT asset, version, created_at, report_json FROM versions"
               + (" WHERE asset = ?" if asset else "")
               + " ORDER BY asset, version")
        rows = self.conn.execute(sql, (asset,) if asset else ()).fetchall()
        out = []
        for r in rows:
            report = json.loads(r["report_json"])
            out.append({
                "asset": r["asset"],
                "version": r["version"],
                "created_at": r["created_at"],
                "vertex_count": report["extraction"]["vertex_count"],
                "face_count": report["extraction"]["face_count"],
                "defects": report["mesh_check"]["defects"],
                "thin_region_count":
                    len(report["wall_thickness"]["regions"]),
                "printable": report["printable"],
            })
        return out

    def get_version(self, asset, version):
        row = self.conn.execute(
            "SELECT * FROM versions WHERE asset = ? AND version = ?",
            (asset, version)).fetchone()
        if row is None:
            from .errors import WorkbenchError, E_VERSION_NOT_FOUND
            raise WorkbenchError(
                E_VERSION_NOT_FOUND,
                f"asset {asset!r} has no version {version}", asset=asset)
        return {
            "asset": row["asset"],
            "version": row["version"],
            "created_at": row["created_at"],
            "params": json.loads(row["params_json"]),
            "report": json.loads(row["report_json"]),
            "mesh_path": row["mesh_path"],
        }

    def diff_versions(self, asset, va, vb):
        """Compare two versions: vertex counts, defect items, thin-wall
        regions."""
        a = self.get_version(asset, va)
        b = self.get_version(asset, vb)
        ra, rb = a["report"], b["report"]

        def thin_zones(r):
            return len(r["wall_thickness"]["regions"])

        return {
            "asset": asset,
            "version_a": va,
            "version_b": vb,
            "vertex_count": {
                "a": ra["extraction"]["vertex_count"],
                "b": rb["extraction"]["vertex_count"],
                "delta": rb["extraction"]["vertex_count"]
                - ra["extraction"]["vertex_count"],
            },
            "face_count": {
                "a": ra["extraction"]["face_count"],
                "b": rb["extraction"]["face_count"],
                "delta": rb["extraction"]["face_count"]
                - ra["extraction"]["face_count"],
            },
            "defects": {
                "a": ra["mesh_check"]["defects"],
                "b": rb["mesh_check"]["defects"],
                "added": sorted(set(rb["mesh_check"]["defects"])
                                - set(ra["mesh_check"]["defects"])),
                "resolved": sorted(set(ra["mesh_check"]["defects"])
                                   - set(rb["mesh_check"]["defects"])),
            },
            "thin_wall_regions": {
                "a": thin_zones(ra),
                "b": thin_zones(rb),
                "delta": thin_zones(rb) - thin_zones(ra),
            },
            "volume": {
                "a": ra["mesh_check"]["signed_volume"],
                "b": rb["mesh_check"]["signed_volume"],
            },
            "printable": {"a": ra["printable"], "b": rb["printable"]},
        }
