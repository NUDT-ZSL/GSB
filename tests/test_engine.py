from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from rope_arbiter.cache import ContentAddressedCache
from rope_arbiter.diagnostics import run_self_check
from rope_arbiter.engine import ArbiterEngine, EngineLimits
from rope_arbiter.errors import DomainError
from rope_arbiter.models import VariantSnapshot
from rope_arbiter.sample import build_sample_cases
from rope_arbiter.variants import MappingRegistry, build_builtin_mapping_registry
from rope_arbiter.workflow import expected_recomputed_nodes


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cases = build_sample_cases()
        self.base = VariantSnapshot.from_cases(self.cases, 1)

    def test_full_sample_self_check_and_equivalence_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = ContentAddressedCache(temporary, max_entries=1000)
            engine = ArbiterEngine(cache)
            report = engine.run(self.cases, self.base, parallelism=2)
            self.assertEqual(report["business_codes"], ["OK"])
            self.assertEqual(len(report["recomputed_nodes"]), 48)
            self.assertEqual(report["reuse_set"], [])
            check = run_self_check(report)
            self.assertTrue(check["ok"], check)
            for case in report["cases"]:
                self.assertEqual(case["angle_mapping"]["code"], "OK")
                self.assertLessEqual(case["angle_mapping"]["max_residual"], 1e-9)
                for normalization in case["normalization"].values():
                    self.assertEqual(normalization["code"], "OK")
                    self.assertLessEqual(
                        normalization["attention_output_max_abs_error"], 1e-9
                    )
                self.assertIn("ranking_consistency_ratio", case["cross_variant"])
                self.assertIn(
                    case["equivalence"]["conclusion"],
                    {"EQUIVALENT", "NOT_EQUIVALENT"},
                )

    def test_version_only_snapshot_reuses_every_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = ContentAddressedCache(temporary, max_entries=1000)
            engine = ArbiterEngine(cache)
            first = engine.run(self.cases, self.base, parallelism=1)
            same_content = VariantSnapshot.create(99, self.base.cases)
            second = engine.run(self.cases, same_content, parallelism=1)
            self.assertEqual(second["recompute_set"], [])
            self.assertEqual(len(second["reuse_set"]), 48)
            self.assertEqual(first["node_hashes"], second["node_hashes"])
            self.assertEqual(first["node_keys"], second["node_keys"])

    def test_directional_invalidation_and_full_recompute_equivalence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = ContentAddressedCache(temporary, max_entries=1000)
            engine = ArbiterEngine(cache)
            base_run = engine.run(self.cases, self.base, parallelism=1)
            base_hashes = base_run["node_hashes"]

            scenarios = [
                (
                    "scale-pi",
                    self.cases[0].case_id,
                    {"scale_ratio": self.cases[0].scale_ratio * 2.0},
                    2,
                ),
                (
                    "scale-ntk",
                    self.cases[1].case_id,
                    {"scale_ratio": self.cases[1].scale_ratio * 2.0},
                    3,
                ),
                (
                    "freq-block",
                    self.cases[2].case_id,
                    {
                        "freq_block": tuple(
                            value * 0.8 for value in self.cases[2].freq_block
                        )
                    },
                    4,
                ),
                (
                    "block-size",
                    self.cases[0].case_id,
                    {"block_size": self.cases[0].block_size + 2},
                    5,
                ),
                (
                    "base-theta",
                    self.cases[0].case_id,
                    {"base_theta": self.cases[0].base_theta * 2.0},
                    6,
                ),
            ]
            for name, case_id, changes, version in scenarios:
                with self.subTest(name=name):
                    snapshot = VariantSnapshot.with_overrides(
                        self.base, version, {case_id: changes}
                    )
                    run = engine.run(self.cases, snapshot, parallelism=1)
                    expected = expected_recomputed_nodes(
                        self.cases, {case_id: changes}
                    )
                    self.assertEqual(set(run["recompute_set"]), expected)
                    for node_id in run["reuse_set"]:
                        if node_id not in expected:
                            self.assertEqual(
                                run["node_hashes"][node_id], base_hashes[node_id]
                            )
                    with tempfile.TemporaryDirectory() as fresh_temporary:
                        fresh_cache = ContentAddressedCache(
                            fresh_temporary, max_entries=1000
                        )
                        fresh = ArbiterEngine(fresh_cache).run(
                            self.cases, snapshot, parallelism=1
                        )
                        self.assertEqual(run["node_hashes"], fresh["node_hashes"])
                        self.assertTrue(run_self_check(run)["ok"])

    def test_parallel_degradation_keeps_identical_hashes(self) -> None:
        limits = EngineLimits(max_parallel_recompute_nodes=2)
        selected = self.cases[:2]
        with tempfile.TemporaryDirectory() as temporary1, tempfile.TemporaryDirectory() as temporary2:
            parallel = ArbiterEngine(
                ContentAddressedCache(temporary1, max_entries=1000), limits=limits
            ).run(selected, self.base, parallelism=8)
            serial = ArbiterEngine(
                ContentAddressedCache(temporary2, max_entries=1000), limits=limits
            ).run(selected, self.base, parallelism=1)
            self.assertEqual(
                parallel["execution"]["degradations"][0]["category"],
                "PARALLEL_RECOMPUTE_REDUCED",
            )
            self.assertEqual(parallel["node_hashes"], serial["node_hashes"])

    def test_memory_and_cache_resource_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tiny_memory = ArbiterEngine(
                ContentAddressedCache(temporary, max_entries=1000),
                limits=EngineLimits(max_attention_memory_bytes=1),
            )
            with self.assertRaises(DomainError) as memory_error:
                tiny_memory.run(self.cases[:1], self.base, parallelism=1)
            self.assertEqual(memory_error.exception.code, "E_RESOURCE_LIMIT")
            self.assertIn("required_bytes", memory_error.exception.context)

        with tempfile.TemporaryDirectory() as temporary:
            tiny_cache = ArbiterEngine(
                ContentAddressedCache(temporary, max_entries=1)
            )
            with self.assertRaises(DomainError) as cache_error:
                tiny_cache.run(self.cases[:1], self.base, parallelism=1)
            self.assertEqual(cache_error.exception.code, "E_RESOURCE_LIMIT")
            self.assertEqual(
                cache_error.exception.context["degraded_category"],
                "CACHE_ENTRY_LIMIT",
            )

    def test_undeclared_mapping_surfaces_business_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine = ArbiterEngine(
                ContentAddressedCache(temporary, max_entries=100),
                mapping_registry=MappingRegistry(),
            )
            report = engine.run(self.cases[:1], self.base, parallelism=1)
            self.assertEqual(report["business_codes"], ["E_MAPPING_UNDECLARED"])
            self.assertEqual(report["cases"][0]["business_code"], "E_MAPPING_UNDECLARED")
            self.assertEqual(report["errors"][0]["code"], "E_MAPPING_UNDECLARED")
            self.assertIn("dimension_index", report["errors"][0]["context"])

    def test_concurrent_adjudication_calls_share_cache_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = ContentAddressedCache(temporary, max_entries=16)
            engine = ArbiterEngine(cache)
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(engine.run, self.cases[:2], self.base, 1)
                    for _ in range(2)
                ]
                reports = [future.result() for future in futures]
            self.assertEqual(reports[0]["node_hashes"], reports[1]["node_hashes"])
            self.assertEqual(cache.entry_count, 16)
            for report in reports:
                self.assertEqual(report["business_codes"], ["OK"])


if __name__ == "__main__":
    unittest.main()
