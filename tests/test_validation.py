import json
import sqlite3
import unittest

from gsb.errors import GsbError
from gsb.model import load_bundle
from tests._util import fresh_sample_copy


def edit_json(path, fn):
    obj = json.loads(path.read_text(encoding="utf-8"))
    fn(obj)
    path.write_text(json.dumps(obj), encoding="utf-8")


class ValidationErrorCodesTests(unittest.TestCase):
    def _expect(self, name, code, mutate):
        sample_dir = fresh_sample_copy(name)
        mutate(sample_dir)
        with self.assertRaises(GsbError) as ctx:
            load_bundle(sample_dir)
        self.assertEqual(ctx.exception.code, code, ctx.exception.message)
        self.assertIsInstance(ctx.exception.details, dict)
        self.assertTrue(ctx.exception.details)

    def test_master_missing_field(self):
        def m(d):
            edit_json(d / "master_stream.json",
                      lambda o: o["transactions"][0].pop("txn_id"))
        self._expect("missing_field", "E_BAD_STREAM", m)

    def test_master_wrong_type(self):
        def m(d):
            edit_json(d / "master_stream.json",
                      lambda o: o["transactions"][0].__setitem__("bytes", "lots"))
        self._expect("bad_type", "E_BAD_STREAM", m)

    def test_txn_not_strictly_increasing(self):
        def m(d):
            edit_json(d / "master_stream.json",
                      lambda o: o["transactions"][1].__setitem__("txn_id", 9999))
        self._expect("order", "E_BAD_STREAM", m)

    def test_segment_gap(self):
        def m(d):
            edit_json(d / "master_stream.json",
                      lambda o: o["segments"][2].__setitem__("segment_id", 9))
        self._expect("seg_gap", "E_BAD_STREAM", m)

    def test_duplicate_replay_id(self):
        def m(d):
            edit_json(d / "replica_stream.json",
                      lambda o: o["records"][1].__setitem__(
                          "replay_id", o["records"][0]["replay_id"]))
        self._expect("dup_replay", "E_BAD_STREAM", m)

    def test_duplicate_instance_id(self):
        def m(d):
            edit_json(d / "replica_stream.json",
                      lambda o: o["instances"][1].__setitem__(
                          "instance_id", o["instances"][0]["instance_id"]))
        self._expect("dup_inst", "E_BAD_STREAM", m)

    def test_per_instance_txn_order(self):
        def m(d):
            edit_json(d / "replica_stream.json",
                      lambda o: o["records"][3].__setitem__("txn_id", 1001))
        self._expect("replica_order", "E_BAD_STREAM", m)

    def test_negative_relative_seconds(self):
        def m(d):
            edit_json(d / "policy_v1.json",
                      lambda o: o.__setitem__("relative_retention_seconds", -1))
        self._expect("neg_rel", "E_BAD_POLICY", m)

    def test_relative_seconds_float(self):
        def m(d):
            edit_json(d / "policy_v1.json",
                      lambda o: o["instances"]["rep-rho"].__setitem__(
                          "relative_retention_seconds", 1.5))
        self._expect("float_rel", "E_BAD_POLICY", m)

    def test_absolute_timestamp_must_be_int(self):
        def m(d):
            edit_json(d / "policy_v2" / "shard_00.json",
                      lambda o: o["nodes"][4].__setitem__(
                          "retention_until_ms", "soon"))
        self._expect("abs_ts", "E_BAD_POLICY", m)

    def test_missing_checkpoint(self):
        def m(d):
            edit_json(d / "policy_v2" / "shard_00.json",
                      lambda o: o["nodes"][4].__setitem__(
                          "checkpoint_refs", ["cp-nope"]))
        self._expect("missing_cp", "E_CHECKPOINT_WATERMARK", m)

    def test_checkpoint_regression(self):
        def m(d):
            conn = sqlite3.connect(d / "policy_v2" / "meta.sqlite")
            conn.execute("UPDATE checkpoints SET watermark_ms=1 WHERE checkpoint_id='cp-2'")
            conn.commit()
            conn.close()
        self._expect("cp_regress", "E_CHECKPOINT_WATERMARK", m)

    def test_shard_sqlite_divergence(self):
        def m(d):
            conn = sqlite3.connect(d / "policy_v2" / "meta.sqlite")
            conn.execute(
                "UPDATE policy_nodes SET retention_until_ms=1 WHERE instance_id='rep-eta'")
            conn.commit()
            conn.close()
        self._expect("diverge", "E_BAD_POLICY", m)

    def test_good_samples_load(self):
        bundle = load_bundle(fresh_sample_copy("good"))
        self.assertEqual(len(bundle.segments), 6)
        self.assertEqual(len(bundle.v2_nodes), 10)


if __name__ == "__main__":
    unittest.main()
