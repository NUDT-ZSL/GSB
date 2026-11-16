"""Deterministic workbench test suite (stdlib unittest, no extra deps)."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from workbench import v1io, v2io
from workbench.classify import classify_region
from workbench.convert import (downgrade_v2_to_v1, ir_from_v1, ir_from_v2,
                               roundtrip_v1_v2_v1, roundtrip_v2_v1_v2,
                               upgrade_v1_to_v2)
from workbench.errors import (E_BOUNDARY_SENSITIVE, E_CONFIDENCE_RANGE,
                              E_PARSE_INVALID, E_REGION_INVALID,
                              E_RELATION_DANGLING)
from workbench.features import RegionFeatures
from workbench.pipeline import run_pipeline
from workbench.samples import generate_samples
from workbench.snapshot import SNAPSHOT_GLOBAL_VERSION, SnapshotManager

BOUNDS = (256, 256)


class TestParsers(unittest.TestCase):
    def test_v1_text_roundtrip(self):
        recs = [{"inst_id": "INST0001", "category": "rectangle",
                 "bbox": [20, 20, 90, 80], "conf_permille": 920}]
        parsed, errors = v1io.parse_v1(v1io.serialize_v1(recs))
        self.assertEqual(errors, [])
        self.assertEqual(parsed, recs)

    def test_v1_missing_field_line(self):
        text = v1io.serialize_v1([{"inst_id": "A", "category": "circle",
                                   "bbox": [1, 2, 3, 4], "conf_permille": 5}])
        _recs, errors = v1io.parse_v1(text[:20] + "\n")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["code"], E_PARSE_INVALID)
        self.assertEqual(errors[0]["line"], 1)

    def test_v2_invalid_json(self):
        _i, _c, errors = v2io.parse_v2('{"broken"', BOUNDS)
        self.assertEqual(errors[0]["code"], E_PARSE_INVALID)
        self.assertIsNone(errors[0]["index"])

    def test_v2_entry_validation_codes(self):
        doc = [
            {"kind": "instance", "id": "X1", "category": "circle",
             "bbox": [1.0, 1.0, 9.0, 9.0], "confidence": 1.7},
            {"kind": "instance", "id": "X2", "category": "circle",
             "bbox": [1.0, 1.0, 999.0, 9.0], "confidence": 0.5},
            {"kind": "instance", "id": "X3", "category": "circle",
             "bbox": [1.0, 1.0, 9.0, 9.0]},
        ]
        _i, _c, errors = v2io.parse_v2(json.dumps(doc), BOUNDS)
        codes = {e["index"]: e["code"] for e in errors}
        self.assertEqual(codes[0], E_CONFIDENCE_RANGE)
        self.assertEqual(codes[1], E_REGION_INVALID)
        self.assertEqual(codes[2], E_PARSE_INVALID)


class TestConversion(unittest.TestCase):
    def test_half_pixel_rounding_and_loss_ledger(self):
        ir = ir_from_v2({"inst_id": "I", "category": "rectangle",
                         "bbox": [196.5, 150.5, 236.5, 196.5],
                         "confidence": 0.76, "relations": ["R1"],
                         "color_name": "cyan"})
        self.assertEqual(ir.bbox, (197, 151, 237, 197))
        self.assertEqual(ir.conf_permille, 760)
        _recs, ledger = downgrade_v2_to_v1([ir])
        self.assertEqual(ledger[0]["coord_residuals"], [-0.5] * 4)
        self.assertEqual(ledger[0]["dropped_relations"], ["R1"])
        self.assertFalse(ledger[0]["lossless"])

    def test_roundtrips_restore_key_fields(self):
        v1 = [ir_from_v1({"inst_id": "A", "category": "circle",
                          "bbox": [1, 2, 30, 40], "conf_permille": 875})]
        self.assertTrue(roundtrip_v1_v2_v1(v1))
        v2 = [ir_from_v2({"inst_id": "B", "category": "triangle",
                          "bbox": [10.5, 2.0, 30.0, 40.25],
                          "confidence": 0.333, "relations": ["R9"],
                          "color_name": "blue"})]
        self.assertTrue(roundtrip_v2_v1_v2(v2))
        insts, colors, sidecar = upgrade_v1_to_v2(v1)
        self.assertEqual(insts[0]["confidence"], 0.875)
        self.assertTrue(sidecar["integer_origin"]["A"]["bbox_was_integer"])
        self.assertIn(insts[0]["category"], colors)
        self.assertEqual(insts[0]["color_name"], "green")

    def test_confidence_clipped_to_permille(self):
        cfg = {"image": {"width": 4, "height": 4},
               "tolerance": {"compactness": 0.5},
               "categories": {"circle": {"weights": {"compactness": 1.0},
                                         "targets": {"compactness": 0.0}}}}
        feat = RegionFeatures("i.png", 0, 16, (0.0, 0.0, 0.0), (0, 0, 4, 4), 1.0)
        _cat, conf, _bd = classify_region(feat, cfg)
        self.assertEqual(conf, 0)  # clipped at lower bound


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="wb_test_"))
        cls.data = cls.tmp / "data"
        generate_samples(cls.data)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, name, **kw):
        code, res = run_pipeline(self.data, self.tmp / name, **kw)
        self.assertEqual(code, 0)
        return res

    def test_serial_parallel_consistency(self):
        a = self._run("serial", serial=True)["canonical"]
        b = self._run("parallel", serial=False)["canonical"]
        for key in ("regions", "classified", "gen_v1_text", "gen_v2_text",
                    "downgrade_loss", "failures", "boundary_sensitive",
                    "render_diff", "round_trip"):
            self.assertEqual(a[key], b[key], f"mismatch in {key}")

    def test_intermediate_representation_and_roundtrip(self):
        canon = self._run("rt")["canonical"]
        self.assertEqual(canon["round_trip"],
                         {"v1_v2_v1": True, "v2_v1_v2": True})
        self.assertFalse(canon["interrupted"])

    def test_render_diff_and_boundary_sensitive(self):
        canon = self._run("render")["canonical"]
        self.assertGreater(canon["render_diff"]["diff_pixels"], 0)
        by_inst = {b["inst_id"]: b for b in canon["boundary_sensitive"]}
        self.assertEqual(by_inst["INST0006"]["code"], E_BOUNDARY_SENSITIVE)
        self.assertIn("half_pixel_rounding", by_inst["INST0006"]["reasons"])
        self.assertIn("confidence_at_threshold",
                      by_inst["INST0007"]["reasons"])
        self.assertEqual(by_inst["INST0007"]["double_precision_ref"]
                         ["confidence"], "0.5")

    def test_injections_all_hit(self):
        report = self._run("inj")["report"]
        hits = {i["id"]: i for i in report["injections"]}
        self.assertEqual(len(hits), 5)
        for inj_id, entry in hits.items():
            self.assertTrue(entry["hit"], f"{inj_id} not hit: {entry}")
        types = {i["type"]: i for i in report["injections"]}
        self.assertEqual(types["missing_field_line"]["expected_code"],
                         E_PARSE_INVALID)
        self.assertEqual(types["dangling_relation"]["expected_code"],
                         E_RELATION_DANGLING)

    def test_snapshot_hit_and_version_rebuild(self):
        out = self.tmp / "snap"
        run_pipeline(self.data, out)
        _c, hit = run_pipeline(self.data, out)
        self.assertTrue(hit["report"]["snapshot"]["hit"])
        _c, rebuilt = run_pipeline(self.data, out,
                                   global_version=SNAPSHOT_GLOBAL_VERSION + 1)
        self.assertFalse(rebuilt["report"]["snapshot"]["hit"])
        mgr = SnapshotManager(out / "snapshot")
        self.assertEqual(mgr.load_manifest()["global_version"],
                         SNAPSHOT_GLOBAL_VERSION + 1)

    def test_graceful_stop_exit_zero(self):
        stop = threading.Event()
        count = {"n": 0}

        def hook(_key):
            count["n"] += 1
            if count["n"] >= 5:
                stop.set()

        out = self.tmp / "stop"
        code, res = run_pipeline(self.data, out, stop_event=stop,
                                 progress_hook=hook)
        self.assertEqual(code, 0)
        self.assertTrue(res["canonical"]["interrupted"])
        manifest = SnapshotManager(out / "snapshot").load_manifest()
        self.assertEqual(manifest["status"], "interrupted")
        self.assertLess(len(manifest["progress"]["completed_regions"]),
                        manifest["progress"]["total_regions"])


if __name__ == "__main__":
    unittest.main()
