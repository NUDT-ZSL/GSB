import unittest
from pathlib import Path

from drift_workbench.engine.errors import DeclarationError
from drift_workbench.engine.loader import (
    is_supported_value,
    load_declaration_directory,
    validate_declaration_document,
)


class LoaderValidationTests(unittest.TestCase):
    def test_supported_values_exclude_bool_and_float(self):
        self.assertTrue(is_supported_value(3))
        self.assertTrue(is_supported_value("ok"))
        self.assertFalse(is_supported_value(True))
        self.assertFalse(is_supported_value(1.5))
        self.assertFalse(is_supported_value(None))

    def test_sample_declarations_load(self):
        groups = load_declaration_directory(
            Path("drift_workbench/samples/declarations")
        )
        self.assertEqual(set(groups), {"app", "data", "network"})
        self.assertEqual([doc.version for doc in groups["network"]], [1])

    def test_bad_property_type_is_rejected_with_location(self):
        with self.assertRaises(DeclarationError) as catcher:
            load_declaration_directory(
                Path("drift_workbench/samples/invalid/decl_bad_type")
            )
        self.assertEqual(catcher.exception.code, "E_DECL")
        self.assertIn("resources[0].properties.mtu", catcher.exception.field_path)

    def test_missing_reference_target_is_rejected_globally(self):
        import tempfile
        import json
        from pathlib import Path as _Path

        with self.assertRaises(DeclarationError) as catcher:
            document = {
                "group": "g",
                "version": 1,
                "depends_on": {},
                "resources": [
                    {
                        "key": "a",
                        "properties": {"x": 1},
                        "references": ["ghost"],
                        "policy": {
                            "on_failure": "skip_downstream",
                            "rollback_on_failure": False,
                        },
                    }
                ],
            }
            with tempfile.TemporaryDirectory() as tmp:
                target = _Path(tmp) / "g.json"
                target.write_text(json.dumps(document), encoding="utf-8")
                from drift_workbench.engine.workspace import Workspace

                Workspace.from_directories(
                    tmp,
                    "drift_workbench/samples/actual/snapshot_v1.json",
                )
        self.assertEqual(catcher.exception.code, "E_DECL")
        self.assertIn("references", catcher.exception.field_path)

    def test_policy_must_be_complete(self):
        with self.assertRaises(DeclarationError) as catcher:
            validate_declaration_document(
                {
                    "group": "g",
                    "version": 1,
                    "resources": [
                        {
                            "key": "a",
                            "properties": {},
                            "references": [],
                            "policy": {"on_failure": "skip_downstream"},
                        }
                    ],
                },
                file="mem.json",
            )
        self.assertEqual(catcher.exception.code, "E_DECL")
        self.assertIn("rollback_on_failure", catcher.exception.field_path)


if __name__ == "__main__":
    unittest.main()
