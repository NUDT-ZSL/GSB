import os
import tempfile
import unittest

from ranker.atomicio import atomic_write_json, read_json


class AtomicIOTest(unittest.TestCase):
    def test_atomic_replace_and_no_temp_leftover(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "data.json")
            payload = {"a": 1, "b": [1, 2, 3]}
            atomic_write_json(path, payload)
            self.assertEqual(read_json(path), payload)
            atomic_write_json(path, {"a": 2})
            self.assertEqual(read_json(path)["a"], 2)
            leftovers = [n for n in os.listdir(os.path.dirname(path))
                         if n.startswith(".tmp-")]
            self.assertEqual(leftovers, [])

    def test_failure_does_not_corrupt_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data.json")
            atomic_write_json(path, {"ok": True})
            with self.assertRaises(TypeError):
                atomic_write_json(path, {"bad": {object()}})
            self.assertEqual(read_json(path), {"ok": True})


if __name__ == "__main__":
    unittest.main()
