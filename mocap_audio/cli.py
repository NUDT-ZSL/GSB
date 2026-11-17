"""命令行入口。

用法：
    python -m mocap_audio.cli --session session.json [--snapshot-id ID]
成功退出码 0；业务失败输出 JSON 错误并返回对应约定整数退出码。
"""

import argparse
import json
import os
import signal
import sys

from .errors import EngineError, EXIT_CODES, Interrupted
from .pipeline import run
from .session import load_session
from .state import BoundarySignaler, compute_snapshot_id


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="多设备动捕录音同步拼接与响度交付引擎")
    parser.add_argument("--session", required=True, help="会话 JSON 路径")
    parser.add_argument("--snapshot-id", default=None,
                        help="显式快照标识；缺省由会话与 WAV 内容计算")
    args = parser.parse_args(argv)

    signaler = BoundarySignaler()
    if os.environ.get("MOCAP_ENGINE_REQUEST_INTERRUPT") == "1":
        signaler.requested = True
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, signaler.request)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signaler.request)

    try:
        session = load_session(args.session)
        snapshot_id = args.snapshot_id or compute_snapshot_id(session)
        outcome = run(session, signaler, provided_snapshot=snapshot_id)
    except Interrupted as exc:
        print(exc.to_json(), file=sys.stderr)
        return EXIT_CODES[exc.code]
    except EngineError as exc:
        print(exc.to_json(), file=sys.stderr)
        return EXIT_CODES[exc.code]

    summary = {
        "ok": True,
        "snapshot_id": outcome["snapshot_id"],
        "commit": outcome["commit"],
        "degraded_overall": outcome["report"]["degraded_overall"],
        "output_frames": outcome["report"]["output"]["frames"],
        "integrated_loudness_dbfs":
            outcome["report"]["loudness"]["integrated_loudness_dbfs"],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

