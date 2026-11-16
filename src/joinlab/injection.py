"""File-only anomaly injection used by the CLI and acceptance harness."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .catalog import load_catalog
from .cost import CostParams
from .errors import JoinLabError, SchemaError, TypeMismatchError
from .queries import load_query_file
from .session import ExperimentSession
from .snapshot import restore_snapshot, save_snapshot


@dataclass(frozen=True)
class InjectionArtifact:
    kind: str
    output_dir: str
    expected_code: str
    detected_code: str
    detected_path: str
    detected_message: str
    mutated_files: tuple[str, ...]
    catalog_path: str
    queries_path: str | None = None
    snapshot_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "output_dir": self.output_dir,
            "expected_code": self.expected_code,
            "detected_code": self.detected_code,
            "detected_path": self.detected_path,
            "detected_message": self.detected_message,
            "mutated_files": list(self.mutated_files),
            "catalog_path": self.catalog_path,
            "queries_path": self.queries_path,
            "snapshot_path": self.snapshot_path,
            "input_only": True,
        }


def _copy_fixture(source_root: Path, output_dir: Path) -> None:
    if source_root.resolve() == output_dir.resolve():
        raise SchemaError("injection output must differ from the source fixture", "injection.output")
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in ("catalog.json", "queries.json"):
        source = source_root / name
        if source.exists():
            shutil.copy2(source, output_dir / name)
    data_dir = source_root / "data"
    if data_dir.exists():
        shutil.copytree(data_dir, output_dir / "data")


def _detect_skew(catalog_path: Path) -> JoinLabError:
    try:
        load_catalog(catalog_path)
    except JoinLabError as exc:
        return exc
    raise SchemaError("skew injection was not detected by the catalog loader", "injection.skew")


def _create_skew(source_root: Path, output_dir: Path) -> tuple[list[str], JoinLabError]:
    catalog_path = output_dir / "catalog.json"
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    mutated: list[str] = []
    table = next((item for item in raw["tables"] if item.get("skew_column")), None)
    if table is None:
        # Fall back to the first table with at least two rows.
        table = next((item for item in raw["tables"] if item.get("row_count", 0) > 1), None)
    if table is None:
        raise SchemaError("cannot inject skew into an empty fixture", "injection.skew")
    data_path = output_dir / table["data_file"]
    data = json.loads(data_path.read_text(encoding="utf-8"))
    rows = data["rows"] if isinstance(data, dict) else data
    if len(rows) < 2:
        raise SchemaError("cannot inject skew into a table with fewer than two rows", "injection.skew")
    column = table.get("skew_column") or next(iter(rows[0]))
    dominant = rows[0][column]
    for row in rows[:-1]:
        row[column] = dominant
    data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    mutated.append(str(data_path))
    return mutated, _detect_skew(catalog_path)


def _create_type_mismatch(source_root: Path, output_dir: Path) -> tuple[list[str], JoinLabError]:
    catalog = load_catalog(output_dir / "catalog.json")
    numeric_table = None
    numeric_column = None
    other_table = None
    other_column = None
    for table in catalog.tables:
        for column in table.columns:
            if column.type in {"integer", "real"} and numeric_table is None:
                numeric_table = table
                numeric_column = column
                break
        if numeric_table is not None:
            break
    for table in catalog.tables:
        if table.key == (numeric_table.key if numeric_table else None):
            continue
        for column in table.columns:
            if column.type not in {"integer", "real"}:
                other_table = table
                other_column = column
                break
        if other_table is not None:
            break
    if numeric_table is None or numeric_column is None or other_table is None or other_column is None:
        raise SchemaError(
            "fixture has no pair of numeric/non-numeric columns for type-mismatch injection",
            "injection.type",
        )
    sql = (
        f"SELECT {numeric_table.key}.{numeric_column.key} "
        f"FROM {numeric_table.key} "
        f"JOIN {other_table.key} ON {numeric_table.key}.{numeric_column.key} = {other_table.key}.{other_column.key}"
    )
    queries_path = output_dir / "queries.json"
    document = {
        "injected": "type-mismatch",
        "queries": [{"name": "injected_type_mismatch", "sql": sql}],
    }
    queries_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    catalog = load_catalog(output_dir / "catalog.json")
    try:
        load_query_file(queries_path, catalog)
    except JoinLabError as exc:
        return [str(queries_path)], exc
    raise SchemaError("type-mismatch injection was not detected", "injection.type")


def _create_missing_snapshot_version(
    output_dir: Path,
    source_root: Path,
    baseline_snapshot: str | Path | None,
) -> tuple[list[str], JoinLabError]:
    snapshot_path = output_dir / "snapshot.json"
    if baseline_snapshot is not None:
        shutil.copy2(Path(baseline_snapshot).expanduser().resolve(), snapshot_path)
    else:
        catalog = load_catalog(output_dir / "catalog.json")
        session = ExperimentSession(catalog, params=CostParams())
        queries_path = output_dir / "queries.json"
        for item in load_query_file(queries_path, catalog):
            session.evaluate(item.sql, name=item.name)
        save_snapshot(session, snapshot_path)
    document = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(document.get("versions"), dict):
        raise SchemaError("baseline snapshot has no versions object", "injection.snapshot")
    document["versions"].pop("param_version", None)
    snapshot_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        restore_snapshot(snapshot_path)
    except JoinLabError as exc:
        return [str(snapshot_path)], exc
    raise SchemaError("missing-version snapshot injection was not detected", "injection.snapshot")


def create_injection(
    kind: str,
    source_root: str | Path,
    output_dir: str | Path,
    *,
    baseline_snapshot: str | Path | None = None,
) -> InjectionArtifact:
    if kind not in {"skew", "type", "snapshot-version"}:
        raise SchemaError(f"unknown injection kind {kind!r}", "injection.kind")
    source = Path(source_root).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    _copy_fixture(source, output)

    if kind == "skew":
        mutated, error = _create_skew(source, output)
        expected = "E_SKEW"
        query_path = output / "queries.json" if (output / "queries.json").exists() else None
        snapshot_path = None
    elif kind == "type":
        mutated, error = _create_type_mismatch(source, output)
        expected = "E_TYPE"
        query_path = output / "queries.json"
        snapshot_path = None
    else:
        mutated, error = _create_missing_snapshot_version(output, source, baseline_snapshot)
        expected = "E_SNAP"
        query_path = output / "queries.json" if (output / "queries.json").exists() else None
        snapshot_path = output / "snapshot.json"

    if error.code != expected:
        raise SchemaError(
            f"injection produced {error.code}, expected {expected}",
            "injection.detection",
            details=error.to_dict(),
        )
    return InjectionArtifact(
        kind=kind,
        output_dir=str(output),
        expected_code=expected,
        detected_code=error.code,
        detected_path=error.path,
        detected_message=error.message,
        mutated_files=tuple(sorted(mutated)),
        catalog_path=str(output / "catalog.json"),
        queries_path=str(query_path) if query_path else None,
        snapshot_path=str(snapshot_path) if snapshot_path else None,
    )
