from __future__ import annotations

import sqlite3
from pathlib import Path

from patrol_stats.db import initialize_schema
from patrol_stats.statistics import build_statistics


def make_manual_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    initialize_schema(conn)

    conn.executemany(
        """
        INSERT INTO regions
            (region_id, region_code, region_name, terrain, remoteness, risk_level)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "R1", "区域一", "山地", 2, "高"),
            (2, "R2", "区域二", "丘陵", 5, "中"),
            (3, "R3", "区域三", "湿地", 9, "高"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO bands
            (band_id, band_code, band_name, freq_low_mhz, freq_high_mhz)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (1, "B1", "频段一", 136, 174),
            (2, "B2", "频段二", 400, 470),
        ],
    )
    conn.executemany(
        """
        INSERT INTO rangers
            (ranger_id, ranger_code, ranger_name, home_region_id, active)
        VALUES (?, ?, ?, ?, 1)
        """,
        [(1, "RG1", "巡护员一", 1), (2, "RG2", "巡护员二", 2), (3, "RG3", "巡护员三", 3)],
    )
    conn.executemany(
        """
        INSERT INTO terminals
            (terminal_id, serial_no, ranger_id, home_region_id, band_id,
             model, active)
        VALUES (?, ?, ?, ?, ?, 'T', 1)
        """,
        [
            (1, "T1", 1, 1, 1),
            (2, "T2", 1, 1, 2),
            (3, "T3", 2, 2, 1),
            (4, "T4", 2, 2, 2),
            (5, "T5", 3, 3, 1),
            (6, "T6", 3, 3, 2),
        ],
    )
    conn.executemany(
        """
        INSERT INTO routes
            (route_id, route_code, route_name, region_id, patrol_km)
        VALUES (?, ?, ?, ?, 10.0)
        """,
        [(1, "RT1", "路线一", 1), (2, "RT2", "路线二", 2), (3, "RT3", "路线三", 3)],
    )
    conn.executemany(
        """
        INSERT INTO base_stations
            (station_id, station_code, station_name, region_id, band_id, coverage_km)
        VALUES (?, ?, ?, ?, ?, 10.0)
        """,
        [
            (1, "BS1", "基站一", 1, 1),
            (2, "BS2", "基站二", 1, 2),
            (3, "BS3", "基站三", 2, 1),
            (4, "BS4", "基站四", 2, 2),
            (5, "BS5", "基站五", 3, 1),
            (6, "BS6", "基站六", 3, 2),
        ],
    )

    assignments = (
        [(1, 1, 1, 1, 1, 1)] * 5
        + [(1, 2, 2, 1, 1, 2)] * 1
        + [(2, 1, 3, 2, 2, 3)] * 1
        + [(2, 2, 4, 2, 2, 4)] * 3
        + [(3, 1, 5, 3, 3, 5)] * 1
        + [(3, 2, 6, 3, 3, 6)] * 1
    )
    log_rows = []
    for index, (region_id, band_id, terminal_id, ranger_id, route_id, station_id) in enumerate(
        assignments, start=1
    ):
        log_rows.append(
            (
                index,
                ranger_id,
                terminal_id,
                route_id,
                region_id,
                band_id,
                station_id,
                f"2026-01-01T{index - 1:02d}:00:00Z",
                0.5,
                -80.0,
                600,
            )
        )
    conn.executemany(
        """
        INSERT INTO patrol_logs
            (log_id, ranger_id, terminal_id, route_id, region_id, band_id,
             station_id, logged_at, fire_risk, signal_dbm, duration_s)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        log_rows,
    )
    conn.commit()
    build_statistics(
        conn,
        "v_manual",
        sample_size=0,
        combo_sample_size=0,
        bucket_count=8,
        combo_source="full",
        note="hand-built fixture",
    )
    return conn
