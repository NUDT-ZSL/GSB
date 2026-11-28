from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .db import json_dumps, reset_database


REGIONS: tuple[dict[str, Any], ...] = (
    {
        "region_id": 1,
        "region_code": "R-NORTH",
        "region_name": "北岭热点",
        "terrain": "山地",
        "remoteness": 1,
        "risk_level": "极高",
        "weight": 0.34,
    },
    {
        "region_id": 2,
        "region_code": "R-EAST",
        "region_name": "东沟热点",
        "terrain": "峡谷",
        "remoteness": 2,
        "risk_level": "高",
        "weight": 0.24,
    },
    {
        "region_id": 3,
        "region_code": "R-SOUTH",
        "region_name": "南坡林场",
        "terrain": "丘陵",
        "remoteness": 3,
        "risk_level": "中高",
        "weight": 0.14,
    },
    {
        "region_id": 4,
        "region_code": "R-WEST",
        "region_name": "西梁护区",
        "terrain": "山地",
        "remoteness": 4,
        "risk_level": "中",
        "weight": 0.10,
    },
    {
        "region_id": 5,
        "region_code": "R-PINE",
        "region_name": "松涛林场",
        "terrain": "针叶林",
        "remoteness": 4,
        "risk_level": "中高",
        "weight": 0.08,
    },
    {
        "region_id": 6,
        "region_code": "R-CLOUD",
        "region_name": "云谷偏远区",
        "terrain": "高山",
        "remoteness": 8,
        "risk_level": "高",
        "weight": 0.05,
    },
    {
        "region_id": 7,
        "region_code": "R-EAGLE",
        "region_name": "鹰嘴偏远区",
        "terrain": "断崖",
        "remoteness": 9,
        "risk_level": "中高",
        "weight": 0.03,
    },
    {
        "region_id": 8,
        "region_code": "R-BLACK",
        "region_name": "黑水无人区",
        "terrain": "湿地",
        "remoteness": 10,
        "risk_level": "高",
        "weight": 0.02,
    },
)

BANDS: tuple[dict[str, Any], ...] = (
    {
        "band_id": 1,
        "band_code": "VHF-L",
        "band_name": "VHF 低段",
        "freq_low_mhz": 136,
        "freq_high_mhz": 174,
    },
    {
        "band_id": 2,
        "band_code": "VHF-H",
        "band_name": "VHF 高段",
        "freq_low_mhz": 174,
        "freq_high_mhz": 230,
    },
    {
        "band_id": 3,
        "band_code": "UHF",
        "band_name": "UHF 巡护段",
        "freq_low_mhz": 400,
        "freq_high_mhz": 470,
    },
    {
        "band_id": 4,
        "band_code": "SAT",
        "band_name": "卫星应急段",
        "freq_low_mhz": 1600,
        "freq_high_mhz": 1660,
    },
)

# Each region has a deliberately different band mix.  Remote regions depend on
# satellite links while the two hotspots concentrate on terrestrial UHF/VHF.
REGION_BAND_WEIGHTS: dict[int, tuple[float, float, float, float]] = {
    1: (0.12, 0.46, 0.37, 0.05),
    2: (0.08, 0.58, 0.29, 0.05),
    3: (0.23, 0.34, 0.35, 0.08),
    4: (0.28, 0.27, 0.30, 0.15),
    5: (0.24, 0.31, 0.30, 0.15),
    6: (0.22, 0.13, 0.10, 0.55),
    7: (0.18, 0.10, 0.07, 0.65),
    8: (0.16, 0.08, 0.04, 0.72),
}

TERMINAL_MODELS = ("FT-200", "FT-310", "FT-420", "FT-550")
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
SIMULATION_DAYS = 90

TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "regions": (
        "region_id",
        "region_code",
        "region_name",
        "terrain",
        "remoteness",
        "risk_level",
    ),
    "bands": (
        "band_id",
        "band_code",
        "band_name",
        "freq_low_mhz",
        "freq_high_mhz",
    ),
    "rangers": (
        "ranger_id",
        "ranger_code",
        "ranger_name",
        "home_region_id",
        "active",
    ),
    "terminals": (
        "terminal_id",
        "serial_no",
        "ranger_id",
        "home_region_id",
        "band_id",
        "model",
        "active",
    ),
    "routes": (
        "route_id",
        "route_code",
        "route_name",
        "region_id",
        "patrol_km",
    ),
    "base_stations": (
        "station_id",
        "station_code",
        "station_name",
        "region_id",
        "band_id",
        "coverage_km",
    ),
    "patrol_logs": (
        "log_id",
        "ranger_id",
        "terminal_id",
        "route_id",
        "region_id",
        "band_id",
        "station_id",
        "logged_at",
        "fire_risk",
        "signal_dbm",
        "duration_s",
    ),
    "queries": (
        "query_id",
        "region_id",
        "band_id",
        "start_ts",
        "end_ts",
        "deviation_threshold",
        "workload_class",
    ),
}


def _weighted_index(rng: random.Random, weights: tuple[float, ...]) -> int:
    point = rng.random()
    cumulative = 0.0
    for index, weight in enumerate(weights):
        cumulative += weight
        if point <= cumulative:
            return index
    return len(weights) - 1


def _format_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _construct_rows(seed: int, log_count: int, query_count: int) -> dict[str, list[dict[str, Any]]]:
    rng = random.Random(seed)
    regions: list[dict[str, Any]] = []
    bands: list[dict[str, Any]] = []
    rangers: list[dict[str, Any]] = []
    terminals: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    stations: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []

    for region in REGIONS:
        regions.append(
            {
                key: region[key]
                for key in (
                    "region_id",
                    "region_code",
                    "region_name",
                    "terrain",
                    "remoteness",
                    "risk_level",
                )
            }
        )
    for band in BANDS:
        bands.append(dict(band))

    region_weights = tuple(float(region["weight"]) for region in REGIONS)
    rangers_by_region: dict[int, list[int]] = {}
    routes_by_region: dict[int, list[int]] = {}
    terminals_by_region: dict[int, list[int]] = {}
    terminals_by_region_band: dict[tuple[int, int], list[int]] = {}
    station_by_region_band: dict[tuple[int, int], int] = {}
    ranger_region: dict[int, int] = {}
    terminal_band: dict[int, int] = {}
    terminal_owner: dict[int, int] = {}

    ranger_id = 1
    for region in REGIONS:
        region_id = int(region["region_id"])
        rangers_by_region[region_id] = []
        for index in range(10):
            rangers.append(
                {
                    "ranger_id": ranger_id,
                    "ranger_code": f"RG-{region_id:02d}-{index + 1:02d}",
                    "ranger_name": f"{region['region_name']}巡护员{index + 1:02d}",
                    "home_region_id": region_id,
                    "active": 1 if index < 9 else 0,
                }
            )
            rangers_by_region[region_id].append(ranger_id)
            ranger_region[ranger_id] = region_id
            ranger_id += 1

    terminal_id = 1
    for region in REGIONS:
        region_id = int(region["region_id"])
        terminals_by_region[region_id] = []
        weights = REGION_BAND_WEIGHTS[region_id]
        for index in range(20):
            # Guarantee every band appears in every region; the remaining
            # assignments follow the regional skew.
            if index < len(BANDS):
                band_id = int(BANDS[index]["band_id"])
            else:
                band_id = int(BANDS[_weighted_index(rng, weights)]["band_id"])
            owner_id = rangers_by_region[region_id][index % len(rangers_by_region[region_id])]
            terminals.append(
                {
                    "terminal_id": terminal_id,
                    "serial_no": f"PT-{region_id:02d}-{index + 1:04d}",
                    "ranger_id": owner_id,
                    "home_region_id": region_id,
                    "band_id": band_id,
                    "model": TERMINAL_MODELS[(terminal_id - 1) % len(TERMINAL_MODELS)],
                    "active": 1 if index < 18 else 0,
                }
            )
            terminals_by_region[region_id].append(terminal_id)
            terminals_by_region_band.setdefault((region_id, band_id), []).append(
                terminal_id
            )
            terminal_band[terminal_id] = band_id
            terminal_owner[terminal_id] = owner_id
            terminal_id += 1

    route_id = 1
    for region in REGIONS:
        region_id = int(region["region_id"])
        routes_by_region[region_id] = []
        for index in range(6):
            routes.append(
                {
                    "route_id": route_id,
                    "route_code": f"RT-{region_id:02d}-{index + 1:02d}",
                    "route_name": f"{region['region_name']}巡护线{index + 1:02d}",
                    "region_id": region_id,
                    "patrol_km": round(4.0 + region_id * 0.7 + index * 1.9, 2),
                }
            )
            routes_by_region[region_id].append(route_id)
            route_id += 1

    station_id = 1
    for region in REGIONS:
        region_id = int(region["region_id"])
        for band in BANDS:
            band_id = int(band["band_id"])
            stations.append(
                {
                    "station_id": station_id,
                    "station_code": f"BS-{region_id:02d}-{band_id:02d}",
                    "station_name": f"{region['region_name']}-{band['band_name']}基站",
                    "region_id": region_id,
                    "band_id": band_id,
                    "coverage_km": round(8.0 + (11 - region["remoteness"]) * 1.8 + band_id, 2),
                }
            )
            station_by_region_band[(region_id, band_id)] = station_id
            station_id += 1

    day_seconds = SIMULATION_DAYS * 24 * 3600

    def append_log(new_log_id: int, region_id: int, band_id: int) -> None:
        terminal = rng.choice(terminals_by_region_band[(region_id, band_id)])
        ranger = terminal_owner[terminal]
        route = rng.choice(routes_by_region[region_id])
        station = station_by_region_band[(region_id, band_id)]
        region = REGIONS[region_id - 1]
        offset = rng.randrange(day_seconds)
        logged_at = _format_ts(EPOCH + timedelta(seconds=offset))
        remote_penalty = float(region["remoteness"]) * 0.018
        fire_risk = min(1.0, max(0.0, 0.18 + rng.random() * 0.62 + remote_penalty))
        signal = -68.0 - float(region["remoteness"]) * 3.7 + (rng.random() - 0.5) * 15.0
        logs.append(
            {
                "log_id": new_log_id,
                "ranger_id": ranger,
                "terminal_id": terminal,
                "route_id": route,
                "region_id": region_id,
                "band_id": band_id,
                "station_id": station,
                "logged_at": logged_at,
                "fire_risk": round(fire_risk, 3),
                "signal_dbm": round(signal, 1),
                "duration_s": rng.randrange(180, 10800),
            }
        )

    # Eight deterministic coverage records per region-band pair keep every
    # sparse combination measurable, while the remaining records preserve the
    # strong hotspot / remote skew.
    coverage_per_pair = 8
    pairs = [
        (int(region["region_id"]), int(band["band_id"]))
        for region in REGIONS
        for band in BANDS
    ]
    seeded_log_count = min(log_count, len(pairs) * coverage_per_pair)
    for new_log_id in range(1, seeded_log_count + 1):
        region_id, band_id = pairs[(new_log_id - 1) // coverage_per_pair]
        append_log(new_log_id, region_id, band_id)

    for new_log_id in range(seeded_log_count + 1, log_count + 1):
        region = REGIONS[_weighted_index(rng, region_weights)]
        region_id = int(region["region_id"])
        terminal = rng.choice(terminals_by_region[region_id])
        append_log(new_log_id, region_id, terminal_band[terminal])

    queries = _make_queries(rng, logs, query_count)
    return {
        "regions": regions,
        "bands": bands,
        "rangers": rangers,
        "terminals": terminals,
        "routes": routes,
        "base_stations": stations,
        "patrol_logs": logs,
        "queries": queries,
    }


def _make_queries(
    rng: random.Random, logs: list[dict[str, Any]], query_count: int
) -> list[dict[str, Any]]:
    if query_count <= 0:
        return []

    by_region: dict[int, list[dict[str, Any]]] = {rid: [] for rid in range(1, 9)}
    by_region_band: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for log in logs:
        by_region[int(log["region_id"])].append(log)
        by_region_band.setdefault(
            (int(log["region_id"]), int(log["band_id"])), []
        ).append(log)

    anchors: list[dict[str, Any]] = []
    region_order = list(by_region)
    rng.shuffle(region_order)
    for region_id in region_order:
        candidates = by_region[region_id]
        if candidates:
            anchors.append(rng.choice(candidates))
    while len(anchors) < query_count:
        anchors.append(rng.choice(logs))
    anchors = anchors[:query_count]
    anchors.sort(key=lambda row: (str(row["logged_at"]), int(row["log_id"])))

    widths = (1800, 21600, 86400, 604800, 259200)
    queries: list[dict[str, Any]] = []
    for index, anchor in enumerate(anchors, start=1):
        half_width = widths[(index - 1) % len(widths)]
        anchor_time = parse_ts(str(anchor["logged_at"]))
        pair_logs = by_region_band[(int(anchor["region_id"]), int(anchor["band_id"]))]
        max_half_width = SIMULATION_DAYS * 24 * 3600
        while True:
            start_time = anchor_time - timedelta(seconds=half_width)
            end_time = anchor_time + timedelta(seconds=half_width)
            matching = sum(
                1
                for log in pair_logs
                if start_time <= parse_ts(str(log["logged_at"])) <= end_time
            )
            if matching >= 8 or half_width >= max_half_width:
                break
            half_width = min(max_half_width, half_width * 2)
        start = _format_ts(start_time)
        end = _format_ts(end_time)
        region = REGIONS[int(anchor["region_id"]) - 1]
        if int(region["remoteness"]) >= 8:
            workload_class = "remote_sparse_wide" if half_width >= 86400 else "remote_sparse_point"
        elif float(region["weight"]) >= 0.20:
            workload_class = "hotspot_dense_wide" if half_width >= 86400 else "hotspot_dense_point"
        else:
            workload_class = "mixed_window"
        queries.append(
            {
                "query_id": f"Q{index:03d}",
                "region_id": int(anchor["region_id"]),
                "band_id": int(anchor["band_id"]),
                "start_ts": start,
                "end_ts": end,
                "deviation_threshold": 0.25 if index % 2 else 0.35,
                "workload_class": workload_class,
            }
        )
    return queries


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(columns),
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row[column] for column in columns})
    data = buffer.getvalue().encode("utf-8")
    path.write_bytes(data)
    return data


def _insert_rows(
    conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]]
) -> None:
    columns = TABLE_COLUMNS[table]
    placeholders = ",".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
    conn.executemany(sql, ([row[column] for column in columns] for row in rows))


def generate_dataset(
    db_path: str | Path,
    output_dir: str | Path,
    *,
    seed: int = 20261128,
    log_count: int = 5000,
    query_count: int = 16,
    overwrite: bool = True,
) -> dict[str, Any]:
    if log_count <= 0:
        raise ValueError("log_count must be positive")
    if query_count <= 0 or query_count > log_count:
        raise ValueError("query_count must be between 1 and log_count")

    db_file = Path(db_path)
    if db_file.exists() and not overwrite:
        raise FileExistsError(f"database already exists: {db_file}")

    data = _construct_rows(seed, log_count, query_count)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    file_hashes: dict[str, str] = {}
    rows_meta: dict[str, int] = {}
    for table in TABLE_COLUMNS:
        rows = data[table]
        content = _write_csv(out_dir / f"{table}.csv", TABLE_COLUMNS[table], rows)
        file_hashes[f"{table}.csv"] = hashlib.sha256(content).hexdigest()
        rows_meta[table] = len(rows)

    combined = hashlib.sha256()
    for name in sorted(file_hashes):
        combined.update(name.encode("ascii"))
        combined.update(b"\0")
        combined.update(file_hashes[name].encode("ascii"))
        combined.update(b"\n")
    data_sha256 = combined.hexdigest()
    manifest = {
        "dataset_version": f"seed-{seed}",
        "seed": seed,
        "row_counts": rows_meta,
        "files": dict(sorted(file_hashes.items())),
        "data_sha256": data_sha256,
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    (out_dir / "manifest.json").write_bytes(manifest_bytes)

    if overwrite and db_file.exists():
        db_file.unlink()
    conn = reset_database(db_file)
    try:
        for table in TABLE_COLUMNS:
            _insert_rows(conn, table, data[table])
        conn.execute(
            """
            INSERT INTO generation_meta
                (singleton, seed, log_count, query_count, data_sha256, manifest_json)
            VALUES (1, ?, ?, ?, ?, ?)
            """,
            (
                seed,
                log_count,
                query_count,
                data_sha256,
                json_dumps(manifest),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return manifest


def load_queries(conn: sqlite3.Connection, query_ids: list[str] | None = None):
    from .model import QuerySpec

    sql = """
        SELECT query_id, region_id, band_id, start_ts, end_ts,
               deviation_threshold, workload_class
        FROM queries
    """
    params: list[Any] = []
    if query_ids:
        placeholders = ",".join("?" for _ in query_ids)
        sql += f" WHERE query_id IN ({placeholders})"
        params.extend(query_ids)
    sql += " ORDER BY query_id"
    return [
        QuerySpec(
            query_id=str(row["query_id"]),
            region_id=int(row["region_id"]),
            band_id=int(row["band_id"]),
            start_ts=str(row["start_ts"]),
            end_ts=str(row["end_ts"]),
            deviation_threshold=float(row["deviation_threshold"]),
            workload_class=str(row["workload_class"]),
        )
        for row in conn.execute(sql, params)
    ]
