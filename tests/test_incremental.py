from __future__ import annotations

import copy
import tempfile
import threading
import unittest
from pathlib import Path

from layout_engine import LayoutEngine
from layout_engine.config import LayoutConfig
from tests.helpers import (
    SAMPLES_DIR,
    canonical_page_core,
    canonical_pages,
    copy_sample_workspace,
    read_json,
    write_json,
)


def _run(
    workspace: dict[str, Path],
    *,
    snapshot: Path,
    allow_config_change: bool = False,
    full_rebuild: bool = False,
    truth: bool = False,
):
    return LayoutEngine(
        workspace["images"],
        workspace["config"],
        workspace["metadata"],
        snapshot,
        truth_path=workspace["truth"] if truth else None,
        allow_config_change=allow_config_change,
        full_rebuild=full_rebuild,
    ).run()


class IncrementalConsistencyTests(unittest.TestCase):
    def test_edit_sequence_matches_clean_full_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = copy_sample_workspace(root / "working")
            snapshot = root / "incremental-snapshot"
            baseline = _run(workspace, snapshot=snapshot)
            self.assertEqual(baseline.exit_code, 0)

            config_payload = read_json(workspace["config"])
            config_payload["analysis"]["binary_threshold"] = 120
            write_json(workspace["config"], config_payload)

            strict = _run(workspace, snapshot=snapshot)
            self.assertEqual(strict.exit_code, 3)
            self.assertIn("E_CONFIG_MISMATCH", strict.report["business_codes"])

            threshold_run = _run(workspace, snapshot=snapshot, allow_config_change=True)
            self.assertEqual(threshold_run.exit_code, 0)
            self.assertTrue(
                all(
                    set(item["recomputed_stages"])
                    == {"detect", "roles", "columns", "order"}
                    for item in threshold_run.report["reuse_recompute"]
                )
            )

            config_payload["columns"]["gap_threshold"] = 64
            write_json(workspace["config"], config_payload)
            gap_run = _run(workspace, snapshot=snapshot, allow_config_change=True)
            for item in gap_run.report["reuse_recompute"]:
                self.assertTrue({"detect", "roles"}.issubset(item["reused_stages"]))
                self.assertEqual(set(item["recomputed_stages"]), {"columns", "order"})

            config_payload["reading"]["rule"] = "region_first"
            write_json(workspace["config"], config_payload)
            rule_run = _run(workspace, snapshot=snapshot, allow_config_change=True)
            for item in rule_run.report["reuse_recompute"]:
                self.assertTrue(
                    {"detect", "roles", "columns"}.issubset(item["reused_stages"])
                )
                self.assertEqual(item["recomputed_stages"], ["order"])
                self.assertFalse(item["detection_performed"])

            replacement = SAMPLES_DIR / "replacements" / "page_alt_mixed.png"
            (workspace["images"] / "page_0002.png").write_bytes(replacement.read_bytes())
            replace_run = _run(workspace, snapshot=snapshot, allow_config_change=True)
            by_page = {item["page"]: item for item in replace_run.report["reuse_recompute"]}
            self.assertEqual(
                set(by_page["page_0002.png"]["recomputed_stages"]),
                {"detect", "roles", "columns", "order"},
            )
            for page_name, item in by_page.items():
                if page_name != "page_0002.png":
                    self.assertEqual(
                        set(item["reused_stages"]),
                        {"detect", "roles", "columns", "order"},
                    )
                    self.assertFalse(item["detection_performed"])

            added = SAMPLES_DIR / "replacements" / "page_alt_double.png"
            (workspace["images"] / "page_9999.png").write_bytes(added.read_bytes())
            add_run = _run(workspace, snapshot=snapshot, allow_config_change=True)
            by_page = {item["page"]: item for item in add_run.report["reuse_recompute"]}
            self.assertIn("page_9999.png", by_page)
            self.assertEqual(
                set(by_page["page_9999.png"]["recomputed_stages"]),
                {"detect", "roles", "columns", "order"},
            )

            (workspace["images"] / "page_0001.png").unlink()
            removed_run = _run(workspace, snapshot=snapshot, allow_config_change=True)
            metadata_payload = read_json(workspace["metadata"])
            metadata_payload["expected_reading_order_entries"] = removed_run.report[
                "summary"
            ]["reading_order_entries"]
            write_json(workspace["metadata"], metadata_payload)
            incremental = _run(workspace, snapshot=snapshot, allow_config_change=True)
            self.assertEqual(incremental.exit_code, 0)
            self.assertNotIn(
                "page_0001.png",
                {item["page"] for item in incremental.report["reuse_recompute"]},
            )
            self.assertEqual(
                incremental.report["summary"]["reading_order_entries"],
                incremental.report["summary"]["expected_reading_order_entries"],
            )

            clean_root = root / "clean"
            clean_workspace = {
                "root": clean_root,
                "images": clean_root / "images",
                "config": clean_root / "config.json",
                "metadata": clean_root / "metadata.json",
                "truth": clean_root / "layout_truth.json",
            }
            clean_workspace["images"].mkdir(parents=True)
            for source in sorted(workspace["images"].glob("*.png")):
                (clean_workspace["images"] / source.name).write_bytes(source.read_bytes())
            write_json(clean_workspace["config"], read_json(workspace["config"]))
            write_json(clean_workspace["metadata"], read_json(workspace["metadata"]))
            full = _run(clean_workspace, snapshot=clean_root / "full-snapshot", full_rebuild=True)
            self.assertEqual(full.exit_code, 0)
            self.assertEqual(
                canonical_page_core(incremental.report),
                canonical_page_core(full.report),
            )

    def test_serial_and_parallel_results_and_failures_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = copy_sample_workspace(root / "workspace")
            (workspace["images"] / "page_0000_bad.png").write_bytes(b"not a png")
            (workspace["images"] / "page_9999_bad.png").write_bytes(b"still not a png")
            base_payload = read_json(workspace["config"])
            serial_config = copy.deepcopy(base_payload)
            serial_config["runtime"]["thread_pool_workers"] = 1
            parallel_config = copy.deepcopy(base_payload)
            parallel_config["runtime"]["thread_pool_workers"] = 4
            write_json(root / "serial.json", serial_config)
            write_json(root / "parallel.json", parallel_config)
            serial_workspace = dict(workspace)
            serial_workspace["config"] = root / "serial.json"
            parallel_workspace = dict(workspace)
            parallel_workspace["config"] = root / "parallel.json"
            serial = _run(serial_workspace, snapshot=root / "serial-snapshot", full_rebuild=True)
            parallel = _run(parallel_workspace, snapshot=root / "parallel-snapshot", full_rebuild=True)
            self.assertEqual(serial.exit_code, 4)
            self.assertEqual(parallel.exit_code, 4)
            self.assertIn("E_IMAGE_FORMAT", serial.report["business_codes"])
            self.assertEqual(canonical_pages(serial.report), canonical_pages(parallel.report))
            self.assertEqual(serial.report["failures"], parallel.report["failures"])
            self.assertEqual(
                [item["page"] for item in serial.report["failures"]],
                ["page_0000_bad.png", "page_9999_bad.png"],
            )

    def test_metadata_dimension_change_requires_new_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = copy_sample_workspace(root / "workspace")
            snapshot = root / "snapshot"
            self.assertEqual(_run(workspace, snapshot=snapshot).exit_code, 0)
            metadata = read_json(workspace["metadata"])
            metadata["page_height"] = 961
            write_json(workspace["metadata"], metadata)
            mismatch = _run(workspace, snapshot=snapshot, allow_config_change=True)
            self.assertEqual(mismatch.exit_code, 3)
            self.assertIn("E_CONFIG_MISMATCH", mismatch.report["business_codes"])

    def test_graceful_stop_commits_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = copy_sample_workspace(root / "workspace")
            payload = read_json(workspace["config"])
            payload["runtime"]["thread_pool_workers"] = 2
            payload["runtime"]["snapshot_commit_interval"] = 1
            write_json(workspace["config"], payload)
            snapshot = root / "snapshot"
            stop_event = threading.Event()

            def stop_after_first_chunk(engine, processed_count) -> None:  # noqa: ANN001, ARG001
                if processed_count >= 2:
                    stop_event.set()

            interrupted = LayoutEngine(
                workspace["images"],
                workspace["config"],
                workspace["metadata"],
                snapshot,
                stop_event=stop_event,
                page_commit_callback=stop_after_first_chunk,
            ).run()
            self.assertEqual(interrupted.exit_code, 0)
            self.assertEqual(interrupted.report["run"]["status"], "interrupted")
            self.assertIn("E_GRACEFUL_STOP", interrupted.report["business_codes"])
            self.assertGreater(interrupted.report["summary"]["pages_processed"], 0)
            self.assertTrue((snapshot / "manifest.json").exists())
            interrupted_pages = {
                item["page"] for item in interrupted.report["reuse_recompute"]
            }

            resumed = LayoutEngine(
                workspace["images"],
                workspace["config"],
                workspace["metadata"],
                snapshot,
            ).run()
            self.assertEqual(resumed.exit_code, 0)
            self.assertEqual(resumed.report["run"]["status"], "complete")
            self.assertEqual(resumed.report["summary"]["pages_processed"], 16)
            resumed_by_page = {
                item["page"]: item for item in resumed.report["reuse_recompute"]
            }
            for page_name in interrupted_pages:
                self.assertEqual(
                    set(resumed_by_page[page_name]["reused_stages"]),
                    {"detect", "roles", "columns", "order"},
                )


if __name__ == "__main__":
    unittest.main()
