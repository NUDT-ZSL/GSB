from __future__ import annotations

import unittest

from layout_order.annotations import (
    AnnotationDocument,
    AnnotationRecord,
    arbitrate_annotations,
    migrate_annotations,
    parse_annotations,
    render_annotations,
    semantic_signature,
    write_annotations,
)
from layout_order.errors import LayoutError
from layout_order.samples import generate_pages, pages_to_annotation_document


class AnnotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = pages_to_annotation_document(generate_pages()[1])

    def test_all_versions_roundtrip_same_sequence(self) -> None:
        reference = semantic_signature(self.document)
        for version in ("v1", "v2", "v3"):
            text = render_annotations(self.document, version)
            loaded = parse_annotations(text)
            self.assertEqual(loaded.version, version)
            signature = semantic_signature(loaded)
            self.assertEqual(signature["sequence"], reference["sequence"])
            self.assertEqual(signature["block_ids"], reference["block_ids"])

    def test_v1_upgrade_records_each_completion(self) -> None:
        v1 = parse_annotations(render_annotations(self.document, "v1"))
        upgraded = migrate_annotations(v1, "v3")
        self.assertEqual(len(upgraded.completions), len(v1.records) * 7)
        fields = {item["field"] for item in upgraded.completions}
        self.assertEqual(
            fields,
            {
                "confidence_low",
                "confidence_high",
                "paragraph_role",
                "order_weight",
                "column_index",
                "zone_id",
                "nesting_path",
            },
        )
        self.assertEqual(
            semantic_signature(upgraded)["sequence"],
            semantic_signature(v1)["sequence"],
        )

    def test_v3_downgrade_strict_and_lossy(self) -> None:
        with self.assertRaises(LayoutError) as context:
            migrate_annotations(self.document, "v1")
        self.assertEqual(context.exception.code, "E_SEMANTIC_DRIFT")
        self.assertEqual(context.exception.block_id, 1)
        lossy = migrate_annotations(self.document, "v1", allow_loss=True)
        self.assertTrue(lossy.losses)
        self.assertTrue(
            all("rule" in item and "source_block_id" in item for item in lossy.losses)
        )
        roundtrip = parse_annotations(
            write_annotations(lossy, "v1", allow_loss=True)
        )
        self.assertEqual(
            semantic_signature(roundtrip)["sequence"],
            semantic_signature(self.document)["sequence"],
        )
        self.assertEqual(
            semantic_signature(roundtrip)["order_weights"],
            semantic_signature(self.document)["order_weights"],
        )

    def test_cross_format_arbitration(self) -> None:
        documents = [
            parse_annotations(render_annotations(self.document, version))
            for version in ("v1", "v2", "v3")
        ]
        result = arbitrate_annotations(documents)
        self.assertEqual(result.winner_version, "v3")
        self.assertEqual(
            semantic_signature(result.document)["sequence"],
            semantic_signature(self.document)["sequence"],
        )
        changed = AnnotationDocument(
            version="v3",
            records=[
                AnnotationRecord(
                    **{
                        **record.to_dict(),
                        "reading_order": (
                            len(self.document.records) - 1 - record.reading_order
                        ),
                    }
                )
                for record in self.document.records
            ],
        )
        with self.assertRaises(LayoutError) as context:
            arbitrate_annotations([documents[0], documents[1], changed])
        self.assertEqual(context.exception.code, "E_SEMANTIC_DRIFT")
        self.assertIsNotNone(context.exception.block_id)

    def test_parse_errors_identify_line_and_version(self) -> None:
        with self.assertRaises(LayoutError) as context:
            parse_annotations("#LAYOUT_RO_V1\n1|0|body\n")
        self.assertEqual(context.exception.code, "E_FORMAT_INVALID")
        self.assertEqual(context.exception.line, 2)
        with self.assertRaises(LayoutError) as context:
            parse_annotations("#LAYOUT_RO_V8\n1|0|body|3\n")
        self.assertEqual(context.exception.code, "E_VERSION_UNSUPPORTED")
        with self.assertRaises(LayoutError) as context:
            parse_annotations("#LAYOUT_RO_V1\nx|0|body|3\n")
        self.assertEqual(context.exception.code, "E_FORMAT_INVALID")
        self.assertEqual(context.exception.line, 2)

    def test_field_set_dispatch_without_header(self) -> None:
        lines = render_annotations(self.document, "v2").splitlines()
        parsed = parse_annotations("\n".join(lines[1:]))
        self.assertEqual(parsed.version, "v2")
        self.assertEqual(
            semantic_signature(parsed)["sequence"],
            semantic_signature(self.document)["sequence"],
        )


if __name__ == "__main__":
    unittest.main()

