from __future__ import annotations

import unittest

from meeting_post.config import (
    MeetingConfig,
    default_config,
    migrate_config,
    parse_config,
    render_config,
)
from meeting_post.demo import stress_config
from meeting_post.errors import MeetingError


class ConfigTests(unittest.TestCase):
    def test_three_versions_align_semantically(self) -> None:
        canonical = default_config("v3")
        digests = []
        for version in ("v1", "v2", "v3"):
            parsed = parse_config(render_config(canonical, version))
            self.assertEqual(parsed.sample_rate, canonical.sample_rate)
            self.assertEqual(parsed.duration_ms, canonical.duration_ms)
            self.assertEqual(parsed.max_speakers, canonical.max_speakers)
            self.assertEqual(parsed.frame_ms, 20)
            self.assertEqual(parsed.hop_ms, 20)
            self.assertEqual(parsed.reid_threshold, 0.55)
            digests.append(parsed.semantic_digest())
        self.assertEqual(len(set(digests)), 1)

    def test_unknown_version_has_code_and_line(self) -> None:
        with self.assertRaises(MeetingError) as context:
            parse_config("v9|16000|4000|3|1.8\n")
        self.assertEqual(context.exception.code, "E_VERSION_UNSUPPORTED")
        self.assertEqual(context.exception.details["line"], 1)

    def test_v1_column_count_error_has_line_and_field(self) -> None:
        with self.assertRaises(MeetingError) as context:
            parse_config("v1|16000|4000|3\n")
        error = context.exception
        self.assertEqual(error.code, "E_FORMAT_CORRUPT")
        self.assertEqual(error.details["line"], 1)
        self.assertEqual(error.details["field"], "threshold_multiplier")

    def test_v2_missing_field_has_name(self) -> None:
        text = render_config(default_config("v3"), "v2").replace("frame_ms|20\n", "")
        with self.assertRaises(MeetingError) as context:
            parse_config(text)
        error = context.exception
        self.assertEqual(error.code, "E_FORMAT_CORRUPT")
        self.assertEqual(error.details["field"], "frame_ms")
        self.assertIn("line", error.details)

    def test_v2_bad_number_has_line_and_field(self) -> None:
        text = render_config(default_config("v3"), "v2").replace(
            "sample_rate|16000", "sample_rate|not-a-number"
        )
        with self.assertRaises(MeetingError) as context:
            parse_config(text)
        error = context.exception
        self.assertEqual(error.code, "E_FORMAT_CORRUPT")
        self.assertEqual(error.details["field"], "sample_rate")
        self.assertEqual(error.details["line"], 2)

    def test_v3_missing_field_has_line_and_field(self) -> None:
        text = render_config(default_config("v3"), "v3").replace(
            '    "frame_ms": 20,\n', ""
        )
        with self.assertRaises(MeetingError) as context:
            parse_config(text)
        error = context.exception
        self.assertEqual(error.code, "E_FORMAT_CORRUPT")
        self.assertEqual(error.details["field"], "segmentation.frame_ms")
        self.assertGreaterEqual(error.details["line"], 1)

    def test_field_set_dispatch_without_explicit_v2_header(self) -> None:
        text = render_config(default_config("v3"), "v2").replace("version|v2\n", "")
        parsed = parse_config(text)
        self.assertEqual(parsed.version, "v2")

    def test_upgrade_records_completion_sources(self) -> None:
        source = parse_config("v1|16000|4000|3|1.8\n")
        migrated, audit = migrate_config(source, "v3")
        self.assertEqual(migrated.version, "v3")
        fields = {item["field"] for item in audit.additions}
        self.assertTrue(
            {
                "frame_ms",
                "hop_ms",
                "normalization",
                "variant",
                "reid_threshold",
                "speaker_gains",
                "mask_mode",
                "precision",
            }.issubset(fields)
        )
        for item in audit.additions:
            self.assertIn("implied_default", item["source"])

    def test_downgrade_semantic_loss_lists_fields(self) -> None:
        stress = stress_config()
        with self.assertRaises(MeetingError) as context:
            migrate_config(stress, "v1")
        error = context.exception
        self.assertEqual(error.code, "E_SEMANTIC_LOSS")
        self.assertEqual(
            set(error.details["fields"]),
            {"speaker_gains", "mask_mode", "precision"},
        )

    def test_lossy_downgrade_records_flatten_and_discard(self) -> None:
        stress = stress_config()
        migrated, audit = migrate_config(stress, "v1", lossy=True)
        self.assertEqual(migrated.gain_map(), {1: 1.0, 2: 1.0, 3: 1.0})
        drops = {item["field"]: item["rule"] for item in audit.drops}
        self.assertEqual(drops["speaker_gains"], "flatten_to_unit_gain")
        self.assertEqual(drops["mask_mode"], "discard_use_target_default")
        self.assertEqual(drops["precision"], "discard_use_target_default")

    def test_canonical_defaults_downgrade_losslessly(self) -> None:
        canonical = default_config("v3")
        migrated, audit = migrate_config(canonical, "v1")
        self.assertEqual(migrated.semantic_digest(), canonical.semantic_digest())
        self.assertTrue(audit.implicit)


if __name__ == "__main__":
    unittest.main()
