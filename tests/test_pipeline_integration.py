from __future__ import annotations

import json
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from soundfield import errors as E
from soundfield.persistence import BLOCK_DIR, VERSION_FILE
from soundfield.pipeline import run_pipeline
from soundfield.scheduler import InterruptFlag

from .conftest import make_workspace


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    return data, rate


class EndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.ws = make_workspace(self.tmp)

    def test_full_run_outputs_and_report(self) -> None:
        artifacts = run_pipeline(
            str(self.ws["scene"]), self.ws["state"], self.ws["out"], clean=True
        )
        report = artifacts.report
        self.assertEqual(report["business_code"], E.E_OK)
        self.assertEqual(report["block_count"], 90)
        wav_path = self.ws["out"] / "soundfield_multichannel.wav"
        data, rate = _read_wav(wav_path)
        self.assertEqual(rate, 48000)
        self.assertEqual(data.size % 6, 0)
        meta = json.loads((self.ws["out"] / (wav_path.name + ".meta.json")).read_text("utf-8"))
        self.assertEqual(meta["version_id"], artifacts.version_id)
        self.assertTrue((self.ws["state"] / VERSION_FILE).exists())
        self.assertEqual(len(list((self.ws["state"] / BLOCK_DIR).glob("*.npz"))), 90)

    def test_resume_produces_identical_samples(self) -> None:
        baseline_out = self.tmp / "baseline"
        run_pipeline(str(self.ws["scene"]), self.ws["state"], baseline_out, clean=True)
        base_bytes, base_rate = _read_wav(baseline_out / "soundfield_multichannel.wav")

        # 第二套工作区：跑到第 5 个分块时在分块边界中断，再续算到完成。
        ws2 = make_workspace(self.tmp / "resume", seed=20261125)
        interrupt = InterruptFlag()
        counter = {"n": 0}

        def observer(_block) -> None:
            counter["n"] += 1
            if counter["n"] == 5:
                interrupt.set("test_interrupt")

        with self.assertRaises(E.BusinessError) as ctx:
            run_pipeline(
                str(ws2["scene"]),
                ws2["state"],
                ws2["out"],
                clean=True,
                interrupt=interrupt,
                block_observer=observer,
            )
        self.assertEqual(ctx.exception.code, E.E_INTERRUPTED)
        marker = json.loads((ws2["state"] / "interrupted.json").read_text("utf-8"))
        self.assertLess(len(marker["completed_blocks"]), 90)
        # 中断时不允许任何“半成品”伪装成完成块。
        completed_npz = set(p.stem for p in (ws2["state"] / BLOCK_DIR).glob("*.npz"))
        self.assertEqual(set(marker["completed_blocks"]), completed_npz)

        resumed = run_pipeline(str(ws2["scene"]), ws2["state"], ws2["out"], clean=False)
        resumed_bytes, resumed_rate = _read_wav(ws2["out"] / "soundfield_multichannel.wav")
        self.assertEqual(base_rate, resumed_rate)
        np.testing.assert_array_equal(base_bytes, resumed_bytes)
        self.assertEqual(resumed.version_id, marker["version_id"])

    def test_version_mismatch_forces_rebuild(self) -> None:
        first = run_pipeline(str(self.ws["scene"]), self.ws["state"], self.ws["out"], clean=True)
        old_blocks = sorted(p.name for p in (self.ws["state"] / BLOCK_DIR).glob("*.npz"))
        scene = json.loads(self.ws["scene"].read_text("utf-8"))
        scene["absorption"]["x-"][0] = 0.13
        self.ws["scene"].write_text(json.dumps(scene), encoding="utf-8")
        second = run_pipeline(str(self.ws["scene"]), self.ws["state"], self.ws["out"], clean=False)
        self.assertNotEqual(first.version_id, second.version_id)
        new_blocks = sorted(p.name for p in (self.ws["state"] / BLOCK_DIR).glob("*.npz"))
        self.assertEqual(old_blocks, new_blocks)  # 块名相同但内容已整体重做
        saved = json.loads((self.ws["state"] / VERSION_FILE).read_text("utf-8"))
        self.assertEqual(saved["version_id"], second.version_id)

    def test_concurrency_and_partition_independence(self) -> None:
        scene_path = self.ws["scene"]
        run_pipeline(scene_path, self.tmp / "s1" / "state", self.tmp / "s1" / "out", clean=True)
        scene = json.loads(Path(scene_path).read_text("utf-8"))
        scene["max_workers"] = 1
        single_path = self.ws["data"] / "scene_single.json"
        single_path.write_text(json.dumps(scene), encoding="utf-8")
        run_pipeline(str(single_path), self.tmp / "s2" / "state", self.tmp / "s2" / "out", clean=True)
        a, _ = _read_wav(self.tmp / "s1" / "out" / "soundfield_multichannel.wav")
        b, _ = _read_wav(self.tmp / "s2" / "out" / "soundfield_multichannel.wav")
        np.testing.assert_array_equal(a, b)

    def test_output_not_replaced_when_version_unchanged(self) -> None:
        artifacts = run_pipeline(str(self.ws["scene"]), self.ws["state"], self.ws["out"], clean=True)
        wav_path = self.ws["out"] / "soundfield_multichannel.wav"
        before = wav_path.stat().st_mtime_ns
        run_pipeline(str(self.ws["scene"]), self.ws["state"], self.ws["out"], clean=False)
        after = wav_path.stat().st_mtime_ns
        self.assertEqual(before, after)
        self.assertTrue(artifacts.report_path.exists())

    def test_fixed_float_paths_both_present(self) -> None:
        run_pipeline(str(self.ws["scene"]), self.ws["state"], self.ws["out"], clean=True)
        fixed = self.ws["out"] / "soundfield_multichannel.wav"
        ref = self.ws["out"] / "soundfield_float64_reference.wav"
        fixed_data, _ = _read_wav(fixed)
        ref_data, _ = _read_wav(ref)
        self.assertEqual(fixed_data.shape, ref_data.shape)
        # 双路径应当接近但允许 Q24 量化差，不得逐点完全相同（否则参考路径无意义）。
        self.assertGreater(np.count_nonzero(fixed_data != ref_data), 0)


if __name__ == "__main__":
    unittest.main()
