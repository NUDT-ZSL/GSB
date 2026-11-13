"""命令行入口。

执行顺序保证：先完成全部配置校验（失败即退出，不创建 db/log/lock 任何文件），
再获取单实例锁、初始化资源并运行引擎。
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import load_config
from .engine import Engine
from .errors import ConfigError, LockError
from .locking import FileLock

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_LOCKED = 3
EXIT_RUN_FAILED = 4
EXIT_INTERRUPTED = 5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对账明细采集引擎")
    parser.add_argument("--config", required=True, help="JSON 配置文件路径")
    parser.add_argument("--failure-hook", default=None,
                        help="测试钩子：source:batch_seq，命中即在事务中途抛异常")
    args = parser.parse_args(argv)

    # 1) 配置校验（任何文件创建之前）
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        sys.stderr.write(json.dumps(
            {"error_code": exc.code, "message": str(exc)}, ensure_ascii=False) + "\n")
        return EXIT_CONFIG

    failure_hook = None
    if args.failure_hook:
        src, seq = args.failure_hook.split(":")
        seq = int(seq)

        def failure_hook(source, batch_seq):
            if source == src and batch_seq == seq:
                raise RuntimeError("TEST_HOOK_TRANSACTION_FAILURE")

    # 2) 单实例锁（5 秒内拿不到即 E_LOCKED 退出）
    try:
        lock = FileLock(cfg.lock_path, timeout_s=5.0)
        lock.acquire()
    except LockError as exc:
        sys.stderr.write(json.dumps(
            {"error_code": "E_LOCKED", "message": str(exc)}, ensure_ascii=False) + "\n")
        return EXIT_LOCKED

    engine = Engine(cfg, failure_hook=failure_hook)
    try:
        report = engine.run()
    except Exception as exc:  # 引擎内部已分类源级错误；此处兜底
        code = getattr(exc, "code", "E_INTERNAL")
        sys.stderr.write(json.dumps(
            {"error_code": code, "message": str(exc)}, ensure_ascii=False) + "\n")
        return EXIT_RUN_FAILED
    finally:
        lock.release()

    print(json.dumps({"run_id": report["run_id"],
                      "run_status": report["run_status"],
                      "resumed_from_run_id": report["resumed_from_run_id"]},
                     ensure_ascii=False))
    if report["run_status"] == "aborted":
        return EXIT_INTERRUPTED
    if report["run_status"] == "failed":
        return EXIT_RUN_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
