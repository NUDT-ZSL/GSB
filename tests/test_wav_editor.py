"""wav_editor 验收测试：覆盖任务要求的 9 项验收标准。

运行方式（仓库根目录）::

    python -m unittest -v
"""

from __future__ import annotations

import array
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import wave

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from tools import make_samples  # noqa: E402
from wav_editor.cli import CliError, validate_segment  # noqa: E402


def run_cli(*args: str) -> subprocess.CompletedProcess:
    """以隔离模式(-I)启动真实 CLI 子进程，返回完整结果。

    -I 会将当前工作目录移出 sys.path，因此通过 -c 显式注入仓库根路径，
    既保留隔离模式对环境变量/用户 site 包的屏蔽，又能定位 wav_editor 包。
    """
    bootstrap = (
        "import sys; "
        "sys.stdout.reconfigure(encoding='utf-8'); "
        "sys.stderr.reconfigure(encoding='utf-8'); "
        "sys.path.insert(0, {root!r}); "
        "from wav_editor.cli import main; "
        "sys.exit(main())"
    ).format(root=REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-I", "-c", bootstrap, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def read_wav(path: str):
    with wave.open(path, "rb") as wf:
        params = wf.getparams()
        raw = wf.readframes(wf.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    return params, samples


class WavEditorAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_root = tempfile.mkdtemp(prefix="wav_editor_test_")
        cls.data_dir = os.path.join(cls.temp_root, "data")
        make_samples.generate_all(cls.data_dir)
        cls.mono_src = os.path.join(cls.data_dir, "tone_16bit_mono_1sec.wav")
        cls.stereo_src = os.path.join(cls.data_dir, "tone_16bit_stereo_1sec.wav")
        cls.long_src = os.path.join(cls.data_dir, "long_16bit_mono_2sec.wav")
        cls.eight_bit_src = os.path.join(cls.data_dir, "tone_8bit_mono_1sec.wav")

    @classmethod
    def tearDownClass(cls) -> None:
        def _make_writable(func, path, _exc_info):
            os.chmod(path, stat.S_IWRITE)
            func(path)

        shutil.rmtree(cls.temp_root, onerror=_make_writable)

    def _out(self, name: str) -> str:
        return os.path.join(self.temp_root, name)

    def _plan(self, name: str) -> str:
        return os.path.join(self.data_dir, name)

    # ---- 验收 1：参数与文件可读性 -------------------------------------------------

    def test_01_missing_argument_names_missing_item(self) -> None:
        result = run_cli(self.mono_src, self._plan("plan_gain_minus6.json"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("output_wav", result.stderr)
        self.assertIn("required", result.stderr.lower())

    def test_01_nonexistent_file_path_in_error(self) -> None:
        missing = os.path.join(self.temp_root, "no_such_source.wav")
        result = run_cli(missing, self._plan("plan_gain_minus6.json"), self._out("x.wav"))
        self.assertEqual(result.returncode, 2)
        self.assertIn(missing, result.stderr)

    # ---- 验收 2：方案字段校验 -----------------------------------------------------

    def test_02_missing_gain_field_reports_index_and_field(self) -> None:
        result = run_cli(
            self.mono_src, self._plan("plan_missing_gain.json"), self._out("x.wav")
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("片段 0", result.stderr)
        self.assertIn("gain_db", result.stderr)

    def test_02_wrong_field_type_reports_index_and_field(self) -> None:
        result = run_cli(
            self.mono_src, self._plan("plan_wrong_type.json"), self._out("x.wav")
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("片段 0", result.stderr)
        self.assertIn("gain_db", result.stderr)

    def test_02_validate_segment_unit(self) -> None:
        with self.assertRaises(CliError) as ctx:
            validate_segment({"start": 0, "end": 1, "fade_in": 0, "fade_out": 0}, 7)
        self.assertIn("片段 7", str(ctx.exception))
        self.assertIn("gain_db", str(ctx.exception))
        with self.assertRaises(CliError):
            validate_segment(
                {"start": 0, "end": 1, "fade_in": 0, "fade_out": 0, "gain_db": True}, 0
            )

    # ---- 验收 3：非 16-bit PCM 拒绝且不产生输出 -----------------------------------

    def test_03_eight_bit_rejected_without_output(self) -> None:
        out = self._out("eight_bit_out.wav")
        self.assertFalse(os.path.exists(out))
        result = run_cli(
            self.eight_bit_src, self._plan("plan_gain_minus6.json"), out
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("16-bit", result.stderr)
        self.assertIn("8-bit", result.stderr)
        self.assertFalse(os.path.exists(out), "拒绝处理时不得产生输出文件")

    # ---- 验收 4：拼接顺序、线性淡变、增益、采样率一致 ------------------------------

    def test_04_minus_6db_gain_halves_peak(self) -> None:
        out = self._out("gain_out.wav")
        result = run_cli(
            self.mono_src, self._plan("plan_gain_minus6.json"), out
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)

        src_params, src_samples = read_wav(self.mono_src)
        out_params, out_samples = read_wav(out)
        src_peak = max(abs(s) for s in src_samples)
        out_peak = max(abs(s) for s in out_samples)
        expected_peak = src_peak * 0.5
        relative_error = abs(out_peak - expected_peak) / expected_peak
        self.assertLessEqual(relative_error, 0.01, f"相对误差 {relative_error:.4%} 超过 1%")
        self.assertEqual(out_params.framerate, src_params.framerate)
        self.assertEqual(report["sample_rate"], src_params.framerate)
        self.assertEqual(out_params.nframes, src_params.nframes)

    def test_04_linear_fades_shape_envelope(self) -> None:
        out = self._out("fades_out.wav")
        result = run_cli(self.mono_src, self._plan("plan_fades.json"), out)
        self.assertEqual(result.returncode, 0, result.stderr)
        _, samples = read_wav(out)
        n = len(samples)
        # 淡入段：首帧幅值最小，整体单调上升到接近全幅
        self.assertLess(abs(samples[0]), abs(samples[100]))
        self.assertLess(abs(samples[100]), abs(samples[2000]))
        # 中段保持原幅；淡出末段衰减
        interior_peak = max(abs(s) for s in samples[n // 2 - 100 : n // 2 + 100])
        self.assertGreater(interior_peak, 19000)
        self.assertGreater(abs(samples[n - 200]), abs(samples[n - 2]))
        self.assertLess(abs(samples[n - 2]), 200)

    # ---- 验收 5：非法片段跳过且不影响其他片段 --------------------------------------

    def test_05_mixed_plan_skips_bad_segment_keeps_valid(self) -> None:
        out = self._out("mixed_out.wav")
        result = run_cli(self.long_src, self._plan("plan_mixed.json"), out)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        statuses = [seg["status"] for seg in report["segments"]]
        self.assertEqual(statuses, ["skipped", "ok", "ok"])
        self.assertTrue(report["segments"][0]["reason"])
        self.assertIn("越界", report["segments"][0]["reason"])
        params, _ = read_wav(out)
        self.assertEqual(params.nframes, 44100)  # 两个 0.5s 有效片段
        self.assertEqual(report["output_total_frames"], 44100)

    # ---- 验收 6：stdout 为合法 JSON 报告 ------------------------------------------

    def test_06_stdout_json_report_shape(self) -> None:
        out = self._out("report_out.wav")
        result = run_cli(self.long_src, self._plan("plan_mixed.json"), out)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)  # 非法 JSON 会直接抛错
        self.assertEqual(len(report["segments"]), 3)
        for seg in report["segments"]:
            self.assertIn(seg["status"], ("ok", "skipped"))
        self.assertIn("output_total_frames", report)

    # ---- 验收 7：全部跳过时生成 44 字节空 WAV，退出码 0 ----------------------------

    def test_07_all_skipped_writes_empty_44_byte_wav(self) -> None:
        out = self._out("empty_out.wav")
        result = run_cli(self.long_src, self._plan("plan_all_skipped.json"), out)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(all(seg["status"] == "skipped" for seg in report["segments"]))
        self.assertTrue(report["all_skipped"])
        self.assertEqual(os.path.getsize(out), 44)
        with wave.open(out, "rb") as wf:
            self.assertEqual(wf.getnframes(), 0)
            self.assertEqual(wf.getframerate(), 44100)

    # ---- 验收 8：输出通道/采样宽度与源一致，可被 wave 正常读取 ----------------------

    def test_08_output_format_matches_source_stereo(self) -> None:
        out = self._out("stereo_out.wav")
        result = run_cli(self.stereo_src, self._plan("plan_stereo.json"), out)
        self.assertEqual(result.returncode, 0, result.stderr)
        with wave.open(self.stereo_src, "rb") as src, wave.open(out, "rb") as dst:
            self.assertEqual(dst.getnchannels(), src.getnchannels())
            self.assertEqual(dst.getsampwidth(), src.getsampwidth())
            self.assertEqual(dst.getframerate(), src.getframerate())
            self.assertEqual(dst.getnframes(), src.getnframes())
        with open(out, "rb") as fh:
            self.assertEqual(fh.read(4), b"RIFF")
            fh.seek(8)
            self.assertEqual(fh.read(4), b"WAVE")

    # ---- 验收 9：输出已存在时先备份，备份失败则中止 --------------------------------

    def test_09_existing_output_backed_up(self) -> None:
        out = self._out("backup_out.wav")
        marker = b"ORIGINAL-CONTENT-MARKER" * 16
        with open(out, "wb") as fh:
            fh.write(marker)
        result = run_cli(
            self.mono_src, self._plan("plan_gain_minus6.json"), out
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(out + ".bak", "rb") as fh:
            self.assertEqual(fh.read(), marker, ".bak 内容必须与原输出文件一致")
        # 新输出是合法 WAV，且已不再是旧内容
        with wave.open(out, "rb") as wf:
            self.assertEqual(wf.getsampwidth(), 2)
        with open(out, "rb") as fh:
            self.assertNotEqual(fh.read(), marker)

    def test_09_backup_failure_aborts_and_keeps_output(self) -> None:
        out = self._out("readonly_out.wav")
        bak = out + ".bak"
        marker = b"DO-NOT-OVERWRITE-MARKER" * 16
        with open(out, "wb") as fh:
            fh.write(marker)
        with open(bak, "wb") as fh:
            fh.write(b"existing readonly backup")
        os.chmod(bak, stat.S_IREAD)  # 备份目标只读 -> copy2 必然失败
        try:
            result = run_cli(
                self.mono_src, self._plan("plan_gain_minus6.json"), out
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("备份", result.stderr)
            with open(out, "rb") as fh:
                self.assertEqual(fh.read(), marker, "备份失败时输出内容必须保持不变")
        finally:
            os.chmod(bak, stat.S_IWRITE)

    # ---- 附加：损坏 JSON 的明确报错 -----------------------------------------------

    def test_10_corrupt_json_rejected(self) -> None:
        bad_plan = os.path.join(self.temp_root, "broken_plan.json")
        with open(bad_plan, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        result = run_cli(self.mono_src, bad_plan, self._out("x.wav"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("JSON", result.stderr)

    def test_11_missing_output_directory_rejected(self) -> None:
        out = os.path.join(self.temp_root, "no_such_dir", "x.wav")
        result = run_cli(
            self.mono_src, self._plan("plan_gain_minus6.json"), out
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("输出目录", result.stderr)
        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main(verbosity=2)
