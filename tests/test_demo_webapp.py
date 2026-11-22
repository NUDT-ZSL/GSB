from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from meeting_post.app_state import DashboardState
from meeting_post.demo import build_demo
from meeting_post.webapp import render_dashboard_html


class DemoWebAppTests(unittest.TestCase):
    def test_demo_artifacts_and_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = build_demo(
                artifact_dir=root / "demo",
                config_dir=root / "configs",
                runtime_dir=root / "runtime",
            )
            self.assertTrue(report["config_interop"]["equal"])
            self.assertTrue(report["self_check"]["ok"])
            self.assertTrue(report["snapshot_lab"]["rollback_blocked"])
            self.assertTrue(report["snapshot_lab"]["corruption_detected"])
            for name in (
                "mixed.pcm",
                "session.wav",
                "truth.json",
                "features.json",
                "masks.json",
                "segments.json",
                "identities.json",
                "demo_report.json",
            ):
                self.assertTrue((root / "demo" / name).exists(), name)
            for version in ("v1", "v2", "v3"):
                config_file = (
                    root / "configs" / ("meeting.v3.json" if version == "v3" else f"meeting.{version}.txt")
                )
                self.assertTrue(config_file.exists())

    def test_dashboard_state_is_json_serializable_and_replace_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = DashboardState(
                artifact_dir=root / "demo",
                config_dir=root / "configs",
                runtime_dir=root / "runtime",
            )
            payload = state.state_payload()
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
            self.assertTrue(payload["status"]["self_check_ok"])
            switched = state.switch_config("v1")
            self.assertTrue(switched["ok"])
            self.assertEqual(state.state_payload()["status"]["current_version"], "v1")
            loaded_stress = state.switch_config("v3-stress")
            self.assertTrue(loaded_stress["ok"])
            self.assertEqual(state.state_payload()["status"]["sample_key"], "v3-stress")
            migration = state.migrate_to("v1", lossy=False)
            self.assertFalse(migration["ok"])
            self.assertEqual(migration["error"]["code"], "E_SEMANTIC_LOSS")
            replaced = state.replace_snapshots()
            self.assertTrue(replaced["ok"])
            self.assertEqual(replaced["stale_guard"]["code"], "E_RESULT_STALE")
            rollback = state.probe_rollback()
            self.assertEqual(
                rollback["expected_error"]["code"], "E_SNAPSHOT_ROLLBACK"
            )
            corruption = state.probe_corruption()
            self.assertEqual(
                corruption["expected_error"]["code"], "E_SNAPSHOT_CORRUPT"
            )

    def test_dashboard_html_has_required_sections(self) -> None:
        html = render_dashboard_html()
        for needle in (
            "会话波形与时间轴",
            "当前配置与三视图",
            "迁移补全与丢弃明细",
            "毫秒 ↔ 帧 ↔ 样本对照",
            "快照版本与内容校验和",
            "最近业务错误码",
        ):
            self.assertIn(needle, html)


if __name__ == "__main__":
    unittest.main()
