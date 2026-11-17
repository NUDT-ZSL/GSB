"""中断/恢复验收脚本：先发送 SIGTERM 再运行管线，随后第二次运行恢复。"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mocap_audio.session import load_session
from mocap_audio.state import StateStore, compute_snapshot_id


def run():
    session_path = sys.argv[1]
    session = load_session(session_path)
    snap = compute_snapshot_id(session)
    store = StateStore(session.resolve(session.state_dir), snap)

    env = dict(os.environ)
    env["MOCAP_ENGINE_REQUEST_INTERRUPT"] = "1"
    first = subprocess.run(
        [sys.executable, "-m", "mocap_audio.cli", "--session", session_path],
        env=env, capture_output=True, text=True)
    code = first.returncode
    print("first run exit code:", code)
    print("first run stderr:", first.stderr.strip())
    print("interrupt flag exists:", store.interrupted())

    second = subprocess.run(
        [sys.executable, "-m", "mocap_audio.cli", "--session", session_path],
        capture_output=True, text=True)
    code = second.returncode
    print("resume run exit code:", code)
    print("resume stdout:", second.stdout.strip())
    report_path = session.resolve(session.output_report)
    with open(report_path, encoding="utf-8") as fh:
        report = json.load(fh)
    restored = [s["segment_id"] for s in report["sync_segments"]
                if s["restored_from_state"]]
    print("restored segments:", restored)


if __name__ == "__main__":
    run()

