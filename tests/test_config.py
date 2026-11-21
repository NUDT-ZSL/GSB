import unittest

from drift_monitor.config_migration import (
    assert_roundtrip,
    downgrade_to_v1,
    serialize_v1,
    serialize_v2,
    serialize_v3,
    upgrade_to_v2,
    upgrade_to_v3,
)
from drift_monitor.config_reader import read_config
from drift_monitor.errors import DriftError
from drift_monitor.config_model import FeatureSpec


V1 = """# DRIFT_MONITOR_CONFIG v1
income_score|numeric|1.2|4|true
device_class|categorical|0.18|4|true
"""


class ConfigReaderTests(unittest.TestCase):
    def test_three_dialects_share_feature_set_and_thresholds(self):
        v1 = read_config(V1)
        v3 = upgrade_to_v3(v1)
        v2_text = serialize_v2(v3)
        v3_text = serialize_v3(v3)
        parsed_v2 = read_config(v2_text)
        parsed_v3 = read_config(v3_text)
        self.assertEqual(v1.source_format, "v1")
        self.assertEqual(parsed_v2.source_format, "v2")
        self.assertEqual(parsed_v3.source_format, "v3")
        names = ["device_class", "income_score"]
        self.assertEqual(parsed_v2.feature_names(), names)
        self.assertEqual(parsed_v3.feature_names(), names)
        for name in names:
            self.assertEqual(
                parsed_v2.features[name].threshold,
                parsed_v3.features[name].threshold,
            )
            self.assertEqual(
                parsed_v2.features[name].window, parsed_v3.features[name].window
            )

    def test_unknown_version(self):
        with self.assertRaises(DriftError) as ctx:
            read_config("# DRIFT_MONITOR_CONFIG v9\n")
        self.assertEqual(ctx.exception.code, "E_VERSION_UNSUPPORTED")

    def test_truncated_header(self):
        with self.assertRaises(DriftError) as ctx:
            read_config("# DRIFT_MONITOR_CONFIG\nx|y\n")
        self.assertEqual(ctx.exception.code, "E_FORMAT_CORRUPT")
        self.assertEqual(ctx.exception.context["line"], 1)

    def test_pipe_field_count_reports_line_and_field(self):
        bad = "# DRIFT_MONITOR_CONFIG v1\nincome_score|numeric|1.2\n"
        with self.assertRaises(DriftError) as ctx:
            read_config(bad)
        self.assertEqual(ctx.exception.code, "E_FORMAT_CORRUPT")
        self.assertEqual(ctx.exception.context["line"], 2)
        self.assertEqual(ctx.exception.context["field"], "window")

    def test_missing_key_and_bad_threshold(self):
        bad = "# DRIFT_MONITOR_CONFIG v2\nname=x|type=numeric|window=4|enabled=true\n"
        with self.assertRaises(DriftError) as ctx:
            read_config(bad)
        self.assertEqual(ctx.exception.code, "E_FORMAT_CORRUPT")
        self.assertEqual(ctx.exception.context["field"], "threshold")

        bad2 = "# DRIFT_MONITOR_CONFIG v2\nname=x|type=numeric|threshold=abc|window=4|enabled=true\n"
        with self.assertRaises(DriftError) as ctx:
            read_config(bad2)
        self.assertEqual(ctx.exception.context["field"], "threshold")

    def test_upgrade_records_provenance_and_defaults(self):
        v1 = read_config(V1)
        v3 = upgrade_to_v3(v1)
        spec = v3.features["income_score"]
        self.assertEqual(spec.methods, ["mean_shift"])
        self.assertEqual(spec.quantiles, [0.25, 0.5, 0.75])
        self.assertEqual(spec.min_samples, 30)
        fields = {entry["field"] for entry in v3.provenance}
        self.assertIn("methods", fields)
        self.assertIn("quantiles", fields)

    def test_roundtrip_v3_v1_v3(self):
        v3 = upgrade_to_v3(read_config(V1))
        v1, discarded = downgrade_to_v1(v3, policy="freeze")
        recovered = upgrade_to_v3(v1)
        assert_roundtrip(v3, recovered)
        self.assertTrue(any(item["field"] == "quantiles" for item in discarded))

    def test_downgrade_drop_policy_emits_inventory(self):
        v3 = upgrade_to_v3(read_config(V1))
        v1, discarded = downgrade_to_v1(v3, policy="drop")
        self.assertEqual(v1.source_format, "v1")
        self.assertTrue(all(item["policy"] == "drop" for item in discarded))
        self.assertTrue(v1.features["income_score"].methods == ["mean_shift"])

    def test_unknown_method_is_config_invalid(self):
        bad_spec = FeatureSpec(
            name="x",
            column_type="numeric",
            threshold=1.0,
            window=4,
            methods=["mystery_method"],
            quantiles=[0.5],
        )
        with self.assertRaises(DriftError) as ctx:
            bad_spec.validate()
        self.assertEqual(ctx.exception.code, "E_CONFIG_INVALID")
        self.assertEqual(ctx.exception.context["field"], "methods")


if __name__ == "__main__":
    unittest.main()
