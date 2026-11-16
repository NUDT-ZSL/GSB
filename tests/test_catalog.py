from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from joinlab.catalog import load_catalog, load_catalog_document
from joinlab.errors import JoinLabError


class CatalogValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog_path = ROOT / "samples" / "catalog.json"
        cls.catalog = load_catalog(cls.catalog_path)
        cls.document = json.loads(cls.catalog_path.read_text(encoding="utf-8"))

    def test_valid_fixture_is_atomic_and_complete(self) -> None:
        self.assertEqual(len(self.catalog.tables), 4)
        self.assertEqual(sum(len(item.rows) for item in self.catalog.data), 140)
        for table, data in zip(self.catalog.tables, self.catalog.data):
            self.assertEqual(table.row_count, len(data.rows))

    def test_duplicate_table_name_is_e_schema_with_path(self) -> None:
        document = copy.deepcopy(self.document)
        document["tables"][1]["name"] = document["tables"][0]["name"].upper()
        with self.assertRaises(JoinLabError) as raised:
            load_catalog_document(document, base_dir=self.catalog_path.parent)
        self.assertEqual(raised.exception.code, "E_SCHEMA")
        self.assertIn("tables[1].name", raised.exception.path)

    def test_declared_row_count_mismatch_is_e_schema(self) -> None:
        document = copy.deepcopy(self.document)
        document["tables"][0]["row_count"] += 1
        with self.assertRaises(JoinLabError) as raised:
            load_catalog_document(document, base_dir=self.catalog_path.parent)
        self.assertEqual(raised.exception.code, "E_SCHEMA")
        self.assertIn("tables[0].row_count", raised.exception.path)

    def test_bad_column_type_is_e_schema(self) -> None:
        document = copy.deepcopy(self.document)
        document["tables"][0]["columns"][0]["type"] = "blob"
        with self.assertRaises(JoinLabError) as raised:
            load_catalog_document(document, base_dir=self.catalog_path.parent)
        self.assertEqual(raised.exception.code, "E_SCHEMA")
        self.assertIn("tables[0].columns[0].type", raised.exception.path)

    def test_bad_row_value_is_e_schema_with_row_and_field_path(self) -> None:
        document = copy.deepcopy(self.document)
        document["tables"][0]["rows"] = [
            {column["name"]: None for column in document["tables"][0]["columns"]}
            for _ in range(document["tables"][0]["row_count"])
        ]
        with self.assertRaises(JoinLabError) as raised:
            load_catalog_document(document, base_dir=self.catalog_path.parent)
        self.assertEqual(raised.exception.code, "E_SCHEMA")
        self.assertIn("tables[0].rows[0].id", raised.exception.path)

    def test_skew_injection_is_e_skew(self) -> None:
        document = copy.deepcopy(self.document)
        table = document["tables"][0]
        rows = document.get("tables")[0].get("rows")
        # Easier: use a temporary copy of the sample tree.
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir)
            (target / "data").mkdir()
            for item in self.document["tables"]:
                source = self.catalog_path.parent / item["data_file"]
                (target / item["data_file"]).write_bytes(source.read_bytes())
            mutated = copy.deepcopy(self.document)
            data_path = target / mutated["tables"][0]["data_file"]
            data = json.loads(data_path.read_text(encoding="utf-8"))
            dominant = data["rows"][0]["region"]
            for row in data["rows"][:-1]:
                row["region"] = dominant
            data_path.write_text(json.dumps(data), encoding="utf-8")
            (target / "catalog.json").write_text(json.dumps(mutated), encoding="utf-8")
            with self.assertRaises(JoinLabError) as raised:
                load_catalog(target / "catalog.json")
            self.assertEqual(raised.exception.code, "E_SKEW")
            self.assertTrue(raised.exception.path.endswith(".rows"))


if __name__ == "__main__":
    unittest.main()
