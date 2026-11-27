from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ticket_gate.generator import generate_project
from ticket_gate.parser import load_project


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class GenerationAndValidationTests(unittest.TestCase):
    def test_fixed_seed_generation_is_byte_for_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            generate_project(first, seed=42)
            generate_project(second, seed=42)
            self.assertEqual(tree_hashes(first), tree_hashes(second))

            project = load_project(first)
            self.assertTrue(project.valid)
            self.assertEqual(project.seed, 42)
            self.assertEqual(len(project.events), 12)
            self.assertEqual(len(project.gates), 48)
            self.assertEqual(len(project.requests), 126)
            for event in project.events.values():
                for rule in event.versions.values():
                    self.assertEqual(
                        set(rule.sources),
                        {
                            "ticket_whitelist",
                            "entry_window.start",
                            "entry_window.end",
                            "reentry_policy",
                            "child_policy.requires_adult",
                        },
                    )
                    self.assertTrue(all(source.startswith("config/events.json#") for source in rule.sources.values()))

    def test_missing_rule_field_is_located_and_event_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "project"
            shutil.copytree(ROOT / "sample_project", copied)
            events_path = copied / "config" / "events.json"
            document = json.loads(events_path.read_text(encoding="utf-8"))
            del document["events"]["EVT-01"]["versions"]["v2"]["ticket_whitelist"]
            events_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

            project = load_project(copied)
            self.assertFalse(project.valid)
            self.assertNotIn("EVT-01", project.events)
            locations = {(issue.code, issue.path) for issue in project.issues}
            self.assertIn(
                (
                    "TICKET_WHITELIST_MISSING",
                    "config/events.json#/events/EVT-01/versions/v2/rules/ticket_whitelist",
                ),
                locations,
            )

    def test_inverted_entry_window_is_rejected_with_original_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "project"
            shutil.copytree(ROOT / "sample_project", copied)
            events_path = copied / "config" / "events.json"
            document = json.loads(events_path.read_text(encoding="utf-8"))
            window = document["events"]["EVT-04"]["versions"]["v2"]["entry_window"]
            window["start"] = "2026-12-13T23:00:00+08:00"
            window["end"] = "2026-12-13T20:00:00+08:00"
            events_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

            project = load_project(copied)
            self.assertFalse(project.valid)
            self.assertNotIn("EVT-04", project.events)
            inverted = [
                issue for issue in project.issues if issue.code == "ENTRY_WINDOW_INVERTED"
            ]
            self.assertEqual(len(inverted), 1)
            self.assertEqual(
                inverted[0].path,
                "config/events.json#/events/EVT-04/versions/v2/rules/entry_window",
            )
            self.assertEqual(inverted[0].event_id, "EVT-04")
            self.assertEqual(inverted[0].version, "v2")


if __name__ == "__main__":
    unittest.main()
