import json
import os
import signal
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from mocap_audio.cli import main as cli_main
from mocap_audio.errors import BudgetError, Interrupted
from mocap_audio.pipeline import run
from mocap_audio.session import load_session
from mocap_audio.state import BoundarySignaler, StateStore, compute_snapshot_id
from mocap_audio.wavio import read_header, write_wav
from tests._helpers import make_tone, stamp_pulse


def _write_device_wavs(tmp, devices, missing_pulse=False):
    for dev in devices:
        rate, bits = dev["sample_rate"], dev["bits"]
        drift = 1.0 + dev.get("drift_ppm", 0.0) * 1e-6
        offset = dev.get("offset_ms", 0.0) * rate / 1000.0
        for seg in dev["segments"]:
            frames = int(round(seg["duration_ms"] * rate / 1000.0))
            data = make_tone(frames, len(seg["channels"]),
                             seed=rate + frames)
            for idx, pulse in enumerate(seg["pulses"]):
                if missing_pulse and dev["device_id"] == "D1" and idx == 0:
                    continue
                ms = pulse["theoretical_ms"]
                peak = int(round(drift * ms * rate / 1000.0 + offset))
                if 0 <= peak < frames:
                    stamp_pulse(data, peak)
            write_wav(os.path.join(tmp, seg["file"]), data, rate, bits)


def _session_dict(devices, **kw):
    base = {
        "session_id": "integ", "snapshot_id": "snap-integ",
        "devices": devices, "model_order": 1, "stat_window_ms": 100,
        "absolute_gate_db": -70, "relative_gate_db": -10,
        "dr_lower_db": 6, "dr_upper_db": 40,
        "target_sample_rate": 48000, "target_bits": 24,
        "timeslice_ms": 200, "concurrency": 2,
        "memory_budget_bytes": 1 << 24, "state_dir": "state",
        "output_wav": "out/program.wav", "output_report": "out/report.json",
        "output_channels": ["A", "B"], "tail_rule": "include",
        "peak_threshold": 0.5,
    }
    base.update(kw)
    return base


def _devices():
    ref = {
        "device_id": "REF", "sample_rate": 48000, "bits": 24,
        "is_reference": True, "offset_ms": 0.0, "drift_ppm": 0.0,
        "segments": [{
            "segment_id": "S1", "file": "wav/ref.wav",
            "start_ms": 0, "duration_ms": 500,
            "channels": ["A", "B"], "gain": [1.0, 1.0],
            "pulses": [{"theoretical_ms": 80, "search_ms": 5},
                       {"theoretical_ms": 240, "search_ms": 5},
                       {"theoretical_ms": 400, "search_ms": 5}]}],
    }
    dev = {
        "device_id": "D1", "sample_rate": 44100, "bits": 16,
        "offset_ms": 2.0, "drift_ppm": 120.0,
        "segments": [{
            "segment_id": "S2", "file": "wav/dev.wav",
            "start_ms": 700, "duration_ms": 400,
            "channels": ["A", "B"], "gain": [1.1, 0.9],
            "pulses": [{"theoretical_ms": 80, "search_ms": 5},
                       {"theoretical_ms": 240, "search_ms": 5}]}],
    }
    return [ref, dev]


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "wav"), exist_ok=True)
        self.session_path = os.path.join(self.tmp, "session.json")

    def _write_session(self, **kw):
        devices = _devices()
        _write_device_wavs(self.tmp, devices)
        with open(self.session_path, "w", encoding="utf-8") as fh:
            json.dump(_session_dict(devices, **kw), fh, ensure_ascii=False)
        return load_session(self.session_path)

    def test_determinism_across_concurrency_and_budget(self):
        session = self._write_session()
        outcomes = []
        for concurrency, budget in ((1, 1 << 24), (4, 1 << 24),
                                    (2, 1 << 20), (8, 1500000)):
            import shutil
            shutil.rmtree(os.path.join(self.tmp, "state"), ignore_errors=True)
            session.concurrency = concurrency
            session.memory_budget_bytes = budget
            outcome = run(session, BoundarySignaler())
            outcomes.append(outcome)
        normalized = []
        for o in outcomes:
            report = dict(o["report"])
            report["concurrency"] = 1
            report["memory_budget_bytes"] = 0
            normalized.append(report)
        reports = [json.dumps(report, sort_keys=True,
                              default=lambda x: x.item()
                              if isinstance(x, (np.floating, np.integer))
                              else x) for report in normalized]
        self.assertEqual(len(set(reports)), 1)
        wav_bytes = [open(os.path.join(self.tmp, "out/program.wav"), "rb")
                     .read() for _ in outcomes]
        self.assertEqual(len(set(wav_bytes)), 1)
        header = read_header(os.path.join(self.tmp, "out/program.wav"))
        self.assertEqual(header.sample_rate, 48000)
        self.assertEqual(header.bits, 24)

    def test_budget_smaller_than_single_segment(self):
        session = self._write_session(memory_budget_bytes=1024)
        with self.assertRaises(BudgetError) as ctx:
            run(session, BoundarySignaler())
        self.assertEqual(ctx.exception.code, "E_BUDGET")

    def test_sync_degradation_recorded(self):
        devices = _devices()
        _write_device_wavs(self.tmp, devices, missing_pulse=True)
        with open(self.session_path, "w", encoding="utf-8") as fh:
            json.dump(_session_dict(devices), fh, ensure_ascii=False)
        session = load_session(self.session_path)
        outcome = run(session, BoundarySignaler())
        dev_sync = next(s for s in outcome["report"]["sync_segments"]
                        if s["device_id"] == "D1")
        self.assertTrue(dev_sync["sync"]["degraded"])
        self.assertEqual(dev_sync["sync"]["used_model_order"], 0)
        self.assertTrue(dev_sync["sync"]["missing_markers"])

    def test_interrupt_and_resume(self):
        session = self._write_session()
        signaler = BoundarySignaler()
        signaler.requested = True
        with self.assertRaises(Interrupted) as ctx:
            run(session, signaler)
        self.assertEqual(ctx.exception.code, "E_INTR")
        store = StateStore(session.resolve("state"),
                           compute_snapshot_id(session))
        self.assertTrue(store.interrupted())

        session2 = load_session(self.session_path)
        outcome = run(session2, BoundarySignaler(),
                      provided_snapshot=compute_snapshot_id(session2))
        self.assertTrue(any(
            s["restored_from_state"] for s in outcome["report"]["sync_segments"]))

    def test_snapshot_change_forces_restart(self):
        session = self._write_session()
        snap = compute_snapshot_id(session)
        run(session, BoundarySignaler(), provided_snapshot=snap)
        wav_path = os.path.join(self.tmp, "wav", "dev.wav")
        _, samples = __import__("mocap_audio.wavio", fromlist=["decode_wav"]) \
            .decode_wav(wav_path)
        perturbed = samples.copy()
        perturbed[10, 0] = 0.777
        write_wav(wav_path, perturbed, 44100, 16)
        changed_session = load_session(self.session_path)
        new_snap = compute_snapshot_id(changed_session)
        self.assertNotEqual(snap, new_snap)
        outcome = run(changed_session, BoundarySignaler(),
                      provided_snapshot=new_snap)
        self.assertFalse(any(
            s["restored_from_state"] for s in outcome["report"]["sync_segments"]))

    def test_cli_exit_codes(self):
        session = self._write_session()
        code = cli_main(["--session", self.session_path])
        self.assertEqual(code, 0)
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w", encoding="utf-8") as fh:
            json.dump({"session_id": "x"}, fh)
        code = cli_main(["--session", bad])
        self.assertEqual(code, 22)


if __name__ == "__main__":
    unittest.main()

