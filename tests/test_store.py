from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from metering_store.database import ChunkStore
from metering_store.generator import generate_dataset
from metering_store.verification import verify_store


class ChunkStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "index.sqlite3"
        self.dataset = generate_dataset(42)

    def open_store(self) -> ChunkStore:
        return ChunkStore(
            self.db_path,
            initial_target_size=384,
            initial_mask_bits=7,
        )

    def test_exact_duplicate_deduplicates_and_delete_reclaims(self) -> None:
        data = self.dataset.files["f-0001"].data
        with self.open_store() as store:
            first = store.write_file("f-a", data, logical_time=1)
            second = store.write_file("f-b", data, logical_time=2)
            self.assertGreater(first["chunk_count"], 1)
            self.assertEqual(first["dedup_hit_chunks"], 0)
            self.assertEqual(second["dedup_hit_chunks"], second["chunk_count"])
            self.assertEqual(second["new_chunks"], 0)

            occupancy = store.occupancy()
            self.assertEqual(occupancy["active_files"], 2)
            self.assertEqual(
                occupancy["logical_active_bytes"],
                len(data) * 2,
            )
            self.assertEqual(
                occupancy["physical_live_bytes"],
                len(data),
            )
            self.assertEqual(
                occupancy["deduplicated_saved_bytes"],
                len(data),
            )

            file_a = store.get_file("f-a")
            file_b = store.get_file("f-b")
            self.assertIsNotNone(file_a)
            self.assertIsNotNone(file_b)
            self.assertEqual(
                [chunk["digest"] for chunk in file_a["chunks"]],
                [chunk["digest"] for chunk in file_b["chunks"]],
            )
            for chunk in file_a["chunks"]:
                self.assertEqual(chunk["ref_count"], 2)
                refs = store.files_for_digest(chunk["digest"])
                self.assertEqual({row["file_id"] for row in refs}, {"f-a", "f-b"})

            deletion_one = store.delete_file("f-a", logical_time=3)
            self.assertEqual(deletion_one["reclaimed_chunk_count"], 0)
            self.assertEqual(
                len(store.search_records("A01", -(2**63), 2**63 - 1)),
                16,
            )
            deletion_two = store.delete_file("f-b", logical_time=4)
            self.assertEqual(
                deletion_two["reclaimed_chunk_count"],
                second["chunk_count"],
            )
            self.assertEqual(deletion_two["reclaimed_bytes"], len(data))
            self.assertEqual(
                deletion_two["occupancy_after"]["physical_live_bytes"],
                deletion_two["occupancy_before"]["physical_live_bytes"] - len(data),
            )
            self.assertEqual(store.search_records("A01", -(2**63), 2**63 - 1), [])
            self.assertTrue(store.consistency_report()["ok"])
            blocks = store.list_blocks()
            self.assertTrue(all(block["reclaimable"] for block in blocks))
            self.assertTrue(all(not block["stored"] for block in blocks))

    def test_near_duplicate_shares_prefix_chunks(self) -> None:
        with self.open_store() as store:
            store.write_file("f-source", self.dataset.files["f-0003"].data)
            near = store.write_file("f-near", self.dataset.files["f-0004"].data)
            self.assertGreater(near["dedup_hit_chunks"], 0)
            self.assertLess(near["dedup_hit_chunks"], near["chunk_count"])
            self.assertTrue(store.consistency_report()["ok"])

    def test_switch_policy_only_affects_new_writes(self) -> None:
        with self.open_store() as store:
            store.write_file("f-old", self.dataset.files["f-0001"].data, logical_time=1)
            old_chunks_before = store.get_file_chunks("f-old")
            old_file_before = store.get_file("f-old")
            new_policy = store.switch_policy(
                target_size=768,
                mask_bits=9,
                logical_time=2,
                label="new",
            )
            result = store.write_file(
                "f-new",
                self.dataset.files["f-0005"].data,
                logical_time=3,
            )
            old_chunks_after = store.get_file_chunks("f-old")
            old_file_after = store.get_file("f-old")

            self.assertEqual(old_chunks_before, old_chunks_after)
            self.assertEqual(old_file_before["policy_id"], 1)
            self.assertEqual(old_file_after["policy_id"], 1)
            self.assertEqual(result["policy_id"], new_policy["policy_id"])
            for chunk in old_chunks_after:
                self.assertEqual(chunk["policy_id"], 1)
                self.assertEqual(chunk["target_size"], 384)
                self.assertEqual(chunk["mask_bits"], 7)
            new_file_chunks = store.get_file_chunks("f-new")
            self.assertTrue(new_file_chunks)
            for chunk in new_file_chunks:
                self.assertEqual(chunk["policy_id"], new_policy["policy_id"])
                self.assertEqual(chunk["target_size"], 768)
                self.assertEqual(chunk["mask_bits"], 9)

            metrics = store.strategy_metrics()
            self.assertEqual(len(metrics), 2)
            self.assertEqual(metrics[0]["writes"], 1)
            self.assertEqual(metrics[1]["writes"], 1)
            self.assertTrue(metrics[1]["active"])

    def test_search_prefix_and_time_range_exclude_deleted_files(self) -> None:
        with self.open_store() as store:
            store.write_file("f-1", self.dataset.files["f-0001"].data)
            store.write_file("f-3", self.dataset.files["f-0003"].data)
            area_one = store.search_records("A01", -(2**63), 2**63 - 1)
            area_two = store.search_records("A02", -(2**63), 2**63 - 1)
            self.assertEqual(len(area_one), 16)
            self.assertEqual(len(area_two), 16)
            self.assertTrue(all(row["area_id"].startswith("A01") for row in area_one))

            selected_time = area_one[0]["ts_ms"]
            timed = store.search_records("A01", selected_time, selected_time)
            self.assertEqual(len(timed), 4)
            self.assertTrue(all(row["ts_ms"] == selected_time for row in timed))
            self.assertTrue(all(row["digest"] for row in timed))

            store.delete_file("f-1")
            self.assertEqual(store.search_records("A01", -(2**63), 2**63 - 1), [])
            self.assertEqual(len(store.search_records("A02", -(2**63), 2**63 - 1)), 16)
            self.assertTrue(store.consistency_report()["ok"])

    def test_full_generated_event_replay_is_consistent(self) -> None:
        with self.open_store() as store:
            policy_by_index = {0: store.active_policy()["policy_id"]}
            for event in self.dataset.events:
                if event.kind in {"write", "duplicate", "near_duplicate"}:
                    store.write_file(
                        event.file_id,
                        self.dataset.files[event.file_id].data,
                        logical_time=event.logical_time,
                    )
                elif event.kind == "switch_policy":
                    policy_spec = self.dataset.policies[event.policy_index]
                    policy = store.switch_policy(
                        target_size=policy_spec["target_size"],
                        mask_bits=policy_spec["mask_bits"],
                        logical_time=event.logical_time,
                        label=policy_spec["label"],
                    )
                    policy_by_index[event.policy_index] = policy["policy_id"]
                elif event.kind == "delete":
                    store.delete_file(event.file_id, logical_time=event.logical_time)
                else:
                    self.fail(f"unexpected event kind: {event.kind}")

            consistency = store.consistency_report()
            self.assertTrue(consistency["ok"], consistency)
            verification = verify_store(store)
            self.assertTrue(verification["ok"], verification)
            self.assertEqual(verification["checked_active_files"], 8)
            self.assertEqual(verification["checked_active_records"], 128)

            metrics = store.strategy_metrics()
            self.assertEqual(len(metrics), 2)
            self.assertGreater(metrics[0]["hit_refs"], 0)
            self.assertGreater(metrics[1]["hit_refs"], 0)
            self.assertEqual(sum(1 for metric in metrics if metric["active"]), 1)
            self.assertEqual(
                store.occupancy()["total_block_refs"],
                store.occupancy()["active_mappings"],
            )

            ledger = store.reclaim_ledger()
            self.assertGreater(ledger["total_reclaimed_chunks"], 0)
            self.assertGreater(ledger["total_reclaimed_bytes"], 0)
            self.assertEqual(
                sum(1 for entry in ledger["entries"] if entry["reclaimed"]),
                ledger["total_reclaimed_chunks"],
            )


if __name__ == "__main__":
    unittest.main()
