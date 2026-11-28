from __future__ import annotations

import unittest

from columnar_eval.acceptance import run_acceptance
from columnar_eval.evaluator import evaluate_batch, evaluate_model
from columnar_eval.generator import generate_sample
from columnar_eval.model import (
    BOOL,
    BatchBudget,
    BudgetRejected,
    ColumnVector,
)
from columnar_eval.normalize import (
    NormalizedModel,
    build_version_difference_manifest,
    normalize_all,
)
from columnar_eval.reference import reference_evaluate_model
from columnar_eval.verify import ReferenceMismatch, verify_against_reference


def _bool_trivalent_model() -> NormalizedModel:
    left = ColumnVector(
        dtype=BOOL,
        values=[False, False, False, True, True, True, None, None, None],
        null_mask=[False, False, False, False, False, False, True, True, True],
    )
    right = ColumnVector(
        dtype=BOOL,
        values=[False, True, None, False, True, None, False, True, None],
        null_mask=[False, False, True, False, False, True, False, False, True],
    )
    expressions = [
        {
            "expression_id": "and",
            "purpose": "AND truth table",
            "root": {
                "node_id": "and.root",
                "op": "and",
                "args": [
                    {"node_id": "and.a", "op": "column", "field": "a"},
                    {"node_id": "and.b", "op": "column", "field": "b"},
                ],
            },
        },
        {
            "expression_id": "or",
            "purpose": "OR truth table",
            "root": {
                "node_id": "or.root",
                "op": "or",
                "args": [
                    {"node_id": "or.a", "op": "column", "field": "a"},
                    {"node_id": "or.b", "op": "column", "field": "b"},
                ],
            },
        },
        {
            "expression_id": "not",
            "purpose": "NOT truth table",
            "root": {
                "node_id": "not.root",
                "op": "not",
                "args": [{"node_id": "not.a", "op": "column", "field": "a"}],
            },
        },
    ]
    return NormalizedModel(
        version="truth-table",
        schema_version="truth-table",
        table="truth_table",
        row_count=9,
        columns={"a": left, "b": right},
        original_to_canonical={"a": "a", "b": "b"},
        canonical_to_original={"a": "a", "b": "b"},
        scale_alignments=[],
        normalized_null_codec={
            "kind": "explicit_per_column_bitmap",
            "bit_order": "msb_first",
            "null_bit": 1,
        },
        expressions=expressions,
        expression_rewrites=[],
        version_manifest={},
        source_schema={},
    )


class GeneratorAndNormalizationTests(unittest.TestCase):
    def test_generator_is_deterministic(self) -> None:
        first = generate_sample(seed=77, row_count=31)
        second = generate_sample(seed=77, row_count=31)
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.schemas["v1"]["row_count"], 31)

    def test_three_schema_versions_use_different_encodings_and_names(self) -> None:
        sample = generate_sample(seed=99, row_count=41)
        names = {
            version: set(schema["field_map"])
            for version, schema in sample.schemas.items()
        }
        self.assertEqual(len({frozenset(value) for value in names.values()}), 3)
        self.assertEqual(
            [sample.schemas[version]["physical_decimal_scale"] for version in ("v1", "v2", "v3")],
            [1, 2, 3],
        )
        self.assertEqual(sample.schemas["v1"]["null_codec"]["kind"], "per_column_bitstring")
        self.assertEqual(sample.schemas["v2"]["null_codec"]["kind"], "global_cursor")
        self.assertEqual(sample.schemas["v2"]["null_codec"]["storage"], "bit_string")
        self.assertEqual(sample.schemas["v3"]["null_codec"]["kind"], "global_cursor")
        self.assertEqual(sample.schemas["v3"]["null_codec"]["storage"], "hex_bytes")

    def test_normalization_restores_fields_scales_and_masks(self) -> None:
        sample = generate_sample(seed=101, row_count=37)
        models = normalize_all(sample)
        base_signature = models["v1"].canonical_signature()
        for version, model in models.items():
            self.assertEqual(model.canonical_signature(), base_signature)
            self.assertEqual(
                set(model.original_to_canonical),
                set(sample.schemas[version]["field_map"]),
            )
            for alignment in model.scale_alignments:
                self.assertEqual(alignment["multiplier"] * alignment["divisor"], 10 ** abs(
                    alignment["target_scale"] - alignment["source_scale"]
                ))
            self.assertEqual(
                model.normalized_null_codec["kind"], "explicit_per_column_bitmap"
            )

    def test_version_difference_manifest_has_only_declared_aspects(self) -> None:
        sample = generate_sample(seed=103, row_count=29)
        models = normalize_all(sample)
        manifest = build_version_difference_manifest(models)
        distinct_aspects = {
            entry["aspect"] for entry in manifest["differences"] if entry["distinct"]
        }
        self.assertEqual(
            distinct_aspects,
            {
                "field_naming",
                "decimal_scale_representation",
                "null_bitmap_encoding",
            },
        )
        canonical = next(
            entry
            for entry in manifest["differences"]
            if entry["aspect"] == "canonical_column_set"
        )
        self.assertFalse(canonical["distinct"])
        self.assertEqual(manifest["unexpected_changes"], [])


class ThreeValuedLogicTests(unittest.TestCase):
    def test_and_or_not_truth_tables_and_reference(self) -> None:
        model = _bool_trivalent_model()
        budget = BatchBudget(
            max_rows=9,
            max_input_columns=2,
            max_working_columns=8,
            max_cells=72,
        )
        run = evaluate_model(model, batch_rows=9, budget=budget)
        reference = reference_evaluate_model(model, batch_rows=9, budget=budget)
        verification = verify_against_reference(run, reference)
        self.assertTrue(verification["ok"])

        expected = {
            "and": [False, False, False, False, True, None, False, None, None],
            "or": [False, True, None, True, True, True, None, True, None],
            "not": [True, True, True, False, False, False, None, None, None],
        }
        for expression_id, values in expected.items():
            vector = run.expression_results[expression_id]
            self.assertEqual(vector.values, values)
            self.assertEqual(vector.null_mask, [value is None for value in values])


class BudgetTests(unittest.TestCase):
    def test_oversized_batch_and_width_are_rejected(self) -> None:
        sample = generate_sample(seed=105, row_count=40)
        model = normalize_all(sample)["v1"]
        budget = BatchBudget(
            max_rows=8,
            max_input_columns=6,
            max_working_columns=32,
            max_cells=256,
        )
        normal = evaluate_model(model, batch_rows=8, budget=budget)
        self.assertEqual(normal.row_count, 40)
        self.assertEqual([batch.row_count for batch in normal.batches], [8, 8, 8, 8, 8])

        oversize = model.batch(batch_index=0, row_start=0, row_count=9)
        with self.assertRaises(BudgetRejected) as caught:
            evaluate_batch(model, oversize, budget=budget)
        rejected = caught.exception
        self.assertEqual(rejected.policy, "reject")
        self.assertEqual(rejected.stage, "batch_input")
        self.assertEqual(rejected.metric, "rows")
        self.assertEqual(rejected.observed, 9)
        self.assertEqual(rejected.limit, 8)
        self.assertEqual(oversize.row_count, 9)

        narrow_budget = BatchBudget(
            max_rows=8,
            max_input_columns=5,
            max_working_columns=32,
            max_cells=256,
        )
        one_row = model.batch(batch_index=0, row_start=0, row_count=1)
        with self.assertRaises(BudgetRejected) as caught_width:
            evaluate_batch(model, one_row, budget=narrow_budget)
        self.assertEqual(caught_width.exception.metric, "input_columns")

        with self.assertRaises(BudgetRejected) as caught_plan:
            evaluate_model(model, batch_rows=9, budget=budget)
        self.assertEqual(caught_plan.exception.metric, "requested_batch_rows")


class PromotionAndLedgerTests(unittest.TestCase):
    def test_promotion_records_are_explicit(self) -> None:
        sample = generate_sample(seed=107, row_count=40)
        model = normalize_all(sample)["v1"]
        budget = BatchBudget(
            max_rows=10,
            max_input_columns=6,
            max_working_columns=48,
            max_cells=480,
        )
        run = evaluate_model(model, batch_rows=10, budget=budget)
        entries = [entry for batch in run.batches for entry in batch.ledger]
        add_entry = next(entry for entry in entries if entry["node_id"] == "expr01.add")
        self.assertEqual(add_entry["promotion"]["result_type"], "decimal(scale=3)")
        self.assertEqual(
            add_entry["promotion"]["operand_targets"],
            ["decimal(scale=3)", "decimal(scale=3)"],
        )
        self.assertTrue(
            any(
                conversion["from"] == "int64"
                and conversion["to"] == "decimal(scale=3)"
                for conversion in add_entry["promotion"]["conversions"]
            )
        )
        compare_entries = [
            entry for entry in entries if entry["node_id"] == "expr03.gt"
        ]
        compare_entry = compare_entries[0]
        self.assertEqual(
            compare_entry["promotion"]["operand_targets"],
            ["int64", "int64"],
        )
        self.assertEqual(compare_entry["promotion"]["result_type"], "bool")
        self.assertGreater(
            sum(entry["null_input_count"] for entry in compare_entries), 0
        )
        self.assertIn("null_output_count", compare_entry)
        self.assertIn("evaluated_rows", compare_entry)
        aggregates = run.aggregate_node_ledger()["expr01_int_plus_decimal"]
        self.assertEqual(set(aggregates), {"expr01.add", "expr01.population", "expr01.avg_speed"})

    def test_reference_mismatch_reports_first_position_and_mask(self) -> None:
        model = _bool_trivalent_model()
        budget = BatchBudget(
            max_rows=9,
            max_input_columns=2,
            max_working_columns=8,
            max_cells=72,
        )
        run = evaluate_model(model, batch_rows=9, budget=budget)
        reference = reference_evaluate_model(model, batch_rows=9, budget=budget)
        reference.batches[0].node_results["and"]["and.a"].vector.null_mask[0] = True
        with self.assertRaises(ReferenceMismatch) as caught:
            verify_against_reference(run, reference)
        mismatch = caught.exception.report["first_mismatch"]
        self.assertEqual(mismatch["batch_index"], 0)
        self.assertEqual(mismatch["expression_id"], "and")
        self.assertEqual(mismatch["node_id"], "and.a")
        self.assertEqual(mismatch["position"], 0)
        self.assertEqual(mismatch["column_position"], 0)
        self.assertGreater(
            mismatch["mask_difference"]["difference_position_count"], 0
        )
        self.assertIn("main_bitstring", mismatch["mask_difference"])
        self.assertIn("reference_bitstring", mismatch["mask_difference"])


class ConditionalAndAcceptanceTests(unittest.TestCase):
    def test_unselected_conditional_branch_nulls_do_not_pollute(self) -> None:
        sample = generate_sample(seed=109, row_count=257)
        model = normalize_all(sample)["v1"]
        budget = BatchBudget(
            max_rows=32,
            max_input_columns=6,
            max_working_columns=64,
            max_cells=2048,
        )
        run = evaluate_model(model, batch_rows=32, budget=budget)
        result = run.expression_results["expr06_conditional_branch"]
        self.assertFalse(result.null_mask[7])
        self.assertEqual(result.values[7], 20_000)
        self.assertFalse(result.null_mask[8])
        self.assertEqual(result.values[8], 5_000)
        self.assertTrue(result.null_mask[5])
        self.assertTrue(result.null_mask[6])
        self.assertTrue(result.null_mask[9])
        ledger = [
            entry
            for batch in run.batches
            for entry in batch.ledger
            if entry["op"] == "conditional"
        ]
        self.assertTrue(ledger)
        for entry in ledger:
            conditional = entry["conditional"]
            self.assertTrue(conditional["unselected_branch_nulls_isolated"])
            self.assertEqual(
                conditional["merged_result_nulls"],
                conditional["condition_null_rows"]
                + conditional["true_branch_result_nulls"]
                + conditional["false_branch_result_nulls"],
            )

    def test_full_acceptance_passes(self) -> None:
        report = run_acceptance(
            seed=111,
            row_count=47,
            batch_rows=16,
            include_run_details=False,
        )
        self.assertEqual(report["status"], "PASS")
        self.assertTrue(all(report["acceptance_criteria"].values()))
        self.assertTrue(report["version_consistency"]["ok"])
        self.assertTrue(report["conditional_isolation_audit"]["ok"])
        self.assertTrue(
            all(item["ok"] for item in report["budget_rejection_audit"].values())
        )


if __name__ == "__main__":
    unittest.main()
