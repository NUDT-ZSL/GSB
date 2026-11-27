import os
import tempfile
import unittest

import numpy as np

from subpixel.app import create_app
from subpixel.engine import CoverageEngine
from subpixel.generator import SEED, generate_project, write_project
from subpixel.incremental import CurveAdjustment
from subpixel.persistence import StoredCommit
from subpixel.validation import validate_project


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_path = os.path.join(self.tmp.name, "project.json")
        self.db_path = os.path.join(self.tmp.name, "coverage.sqlite3")
        project = validate_project(generate_project(SEED))
        write_project(project, self.project_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_commit_resume_and_version_drift(self):
        engine = CoverageEngine.from_project_file(
            self.project_path, self.db_path)
        engine.run_full()
        commit = engine.commit("baseline")
        self.assertTrue(os.path.exists(self.db_path))

        coverage, thumbnail, contributions = engine.store.load_commit_artifacts(
            commit.commit_id)
        np.testing.assert_array_equal(coverage, engine.state.full.coverage)
        self.assertEqual(len(contributions), len(engine.state.full.contributions))

        resumed = engine.resume()
        self.assertIsInstance(resumed, StoredCommit)
        np.testing.assert_array_equal(
            engine.state.full.coverage, coverage)

        tampered = StoredCommit(**{**commit.__dict__, "content_hash": "0" * 64})
        with self.assertRaises(ValueError):
            engine.store.assert_version_current(engine.project, tampered)
        engine.close()

    def test_incremental_commit_chain(self):
        engine = CoverageEngine.from_project_file(
            self.project_path, self.db_path)
        engine.run_full()
        first = engine.commit("v1")
        engine.adjust([CurveAdjustment("FB-0002", radius_px=1.1)])
        second = engine.commit("v2")
        self.assertEqual(second.parent_commit_id, first.commit_id)
        self.assertNotEqual(second.content_hash, first.content_hash)
        engine.close()


class FlaskAppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_path = os.path.join(self.tmp.name, "project.json")
        self.db_path = os.path.join(self.tmp.name, "coverage.sqlite3")
        write_project(
            validate_project(generate_project(SEED)), self.project_path)
        self.app = create_app(self.project_path, self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.config["ENGINE"].close()
        self.tmp.cleanup()

    def test_dashboard_and_images(self):
        for path in ("/", "/healthz", "/fibers", "/thumbnail", "/contour",
                     "/image/coverage.png", "/image/reference.png",
                     "/image/contour.png", "/image/thumbnail.png",
                     "/api/summary"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
        png = self.client.get("/image/coverage.png").data
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_fiber_api(self):
        response = self.client.get("/api/fibers/FB-0001")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertGreater(payload["pixel_count"], 0)

    def test_recompute_endpoint_returns_range(self):
        response = self.client.get(
            "/api/recompute?curve=FB-0001:1.2:0.9")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["recompute"]["equal_to_full"])
        self.assertGreater(payload["recompute"]["affected_pixel_count"], 0)
        self.assertEqual(payload["version"], "1.0.1")

    def test_unknown_fiber_404(self):
        response = self.client.get("/api/fibers/NOPE")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
