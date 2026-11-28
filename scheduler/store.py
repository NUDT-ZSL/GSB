"""SQLite 单文件持久化：归一模型、基线快照、反馈事件与增量重排记录。"""
import json
import sqlite3
from datetime import datetime, timezone

from .model import Activity, Project
from .resources import daily_curve

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    schema TEXT NOT NULL,
    planned_start_ms INTEGER NOT NULL,
    caps_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activities (
    project_id TEXT NOT NULL,
    activity_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    name TEXT NOT NULL,
    seq INTEGER NOT NULL,
    duration_hours REAL NOT NULL,
    resources_json TEXT NOT NULL,
    predecessors_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    PRIMARY KEY (project_id, activity_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    label TEXT NOT NULL,
    caps_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_activities (
    snapshot_id INTEGER NOT NULL,
    activity_id TEXT NOT NULL,
    es_ms INTEGER NOT NULL,
    ef_ms INTEGER NOT NULL,
    ls_ms INTEGER NOT NULL,
    lf_ms INTEGER NOT NULL,
    float_ms INTEGER NOT NULL,
    critical INTEGER NOT NULL,
    actual_finish_ms INTEGER,
    trace_predecessors_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_resources (
    snapshot_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    trade TEXT NOT NULL,
    demand REAL NOT NULL,
    cap REAL,
    over INTEGER NOT NULL,
    activities_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    affected_json TEXT NOT NULL,
    edges_json TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    over_json TEXT NOT NULL
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    return conn


def save_project(conn, project):
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO projects VALUES (?,?,?,?,?,?)",
            (project.project_id, project.name, project.schema,
             project.planned_start_ms, json.dumps(project.caps),
             json.dumps(project.provenance, ensure_ascii=False)))
        conn.execute("DELETE FROM activities WHERE project_id=?",
                     (project.project_id,))
        for a in project.activities.values():
            conn.execute(
                "INSERT INTO activities VALUES (?,?,?,?,?,?,?,?,?)",
                (project.project_id, a.activity_id, a.segment_id, a.name,
                 a.order, a.duration_hours, json.dumps(a.resources),
                 json.dumps(a.predecessors),
                 json.dumps(a.provenance, ensure_ascii=False)))


def load_project(conn, project_id, snapshot_id=None):
    """重建归一模型；给定快照时叠加该快照的计算结果与实际完成时刻。"""
    row = conn.execute(
        "SELECT name, schema, planned_start_ms, caps_json, provenance_json "
        "FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if row is None:
        from .errors import BusinessError, E_NOT_FOUND
        raise BusinessError(E_NOT_FOUND, f"工程不存在: {project_id}")
    name, schema, planned, caps_json, prov_json = row
    activities = {}
    for r in conn.execute(
            "SELECT activity_id, segment_id, name, seq, duration_hours,"
            " resources_json, predecessors_json, provenance_json"
            " FROM activities WHERE project_id=?", (project_id,)):
        activities[r[0]] = Activity(
            activity_id=r[0], segment_id=r[1], name=r[2], order=r[3],
            duration_hours=r[4], resources=json.loads(r[5]),
            predecessors=json.loads(r[6]), provenance=json.loads(r[7]))
    project = Project(project_id=project_id, name=name, schema=schema,
                      planned_start_ms=planned, activities=activities,
                      caps=json.loads(caps_json),
                      provenance=json.loads(prov_json))
    if snapshot_id is not None:
        row = conn.execute(
            "SELECT caps_json FROM snapshots WHERE snapshot_id=?",
            (snapshot_id,)).fetchone()
        if row is not None:
            project.caps = json.loads(row[0])
        for r in conn.execute(
                "SELECT activity_id, es_ms, ef_ms, ls_ms, lf_ms, float_ms,"
                " critical, actual_finish_ms, trace_predecessors_json"
                " FROM snapshot_activities WHERE snapshot_id=?",
                (snapshot_id,)):
            a = activities[r[0]]
            a.es_ms, a.ef_ms, a.ls_ms, a.lf_ms, a.float_ms = r[1:6]
            a.critical = bool(r[6])
            a.actual_finish_ms = r[7]
            a.trace_predecessors = json.loads(r[8])
    return project


def save_snapshot(conn, project, label):
    created = datetime.now(timezone.utc).isoformat()
    with conn:
        cur = conn.execute(
            "INSERT INTO snapshots (project_id, label, caps_json, created_at)"
            " VALUES (?,?,?,?)",
            (project.project_id, label, json.dumps(project.caps), created))
        sid = cur.lastrowid
        for a in project.activities.values():
            conn.execute(
                "INSERT INTO snapshot_activities VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, a.activity_id, a.es_ms, a.ef_ms, a.ls_ms, a.lf_ms,
                 a.float_ms, int(a.critical), a.actual_finish_ms,
                 json.dumps(a.trace_predecessors)))
        for (day, trade), slot in daily_curve(project).items():
            conn.execute(
                "INSERT INTO snapshot_resources VALUES (?,?,?,?,?,?,?)",
                (sid, day, trade, slot["demand"], slot["cap"],
                 int(slot["over"]), json.dumps(slot["activities"])))
    return sid


def latest_snapshot_id(conn, project_id):
    row = conn.execute(
        "SELECT snapshot_id FROM snapshots WHERE project_id=?"
        " ORDER BY snapshot_id DESC LIMIT 1", (project_id,)).fetchone()
    return row[0] if row else None


def record_event(conn, project_id, snapshot_id, result):
    with conn:
        conn.execute(
            "INSERT INTO events"
            " (project_id, snapshot_id, payload_json, affected_json,"
            "  edges_json, changes_json, over_json) VALUES (?,?,?,?,?,?,?)",
            (project_id, snapshot_id,
             json.dumps(result["event"], ensure_ascii=False),
             json.dumps(result["affected"], ensure_ascii=False),
             json.dumps(result["edges"], ensure_ascii=False),
             json.dumps(result["changes"], ensure_ascii=False),
             json.dumps(result["over_capacity"], ensure_ascii=False)))


def load_event_payloads(conn, project_id):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload_json FROM events WHERE project_id=?"
        " ORDER BY event_id", (project_id,))]


def list_history(conn, project_id):
    snapshots = [{"snapshot_id": r[0], "label": r[1], "created_at": r[2]}
                 for r in conn.execute(
                     "SELECT snapshot_id, label, created_at FROM snapshots"
                     " WHERE project_id=? ORDER BY snapshot_id",
                     (project_id,))]
    events = [{"event_id": r[0], "snapshot_id": r[1],
               "event": json.loads(r[2]),
               "affected": json.loads(r[3]),
               "changes": json.loads(r[4])}
              for r in conn.execute(
                  "SELECT event_id, snapshot_id, payload_json,"
                  " affected_json, changes_json FROM events"
                  " WHERE project_id=? ORDER BY event_id", (project_id,))]
    return {"snapshots": snapshots, "events": events}
