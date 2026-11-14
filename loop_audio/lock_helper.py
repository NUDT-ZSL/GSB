# -*- coding: utf-8 -*-
"""跨平台独占锁定指定文件，保持 N 秒后释放，用于验证规则 12。

用法：
    python -I lock_helper.py <文件路径> <保持秒数>
锁定成功后打印 LOCKED 并刷新 stdout，等待结束后自动释放并退出。
"""

import sys
import time


def main() -> int:
    path = sys.argv[1]
    hold = float(sys.argv[2])
    fh = open(path, "rb")
    try:
        import msvcrt  # Windows：对当前位置起 1 字节加排他锁

        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except ImportError:
        import fcntl  # POSIX：flock 排他锁

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("LOCKED", flush=True)
    try:
        time.sleep(hold)
    finally:
        try:
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except ImportError:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
    print("RELEASED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
