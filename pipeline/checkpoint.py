"""检查点、原子写与文件锁（业务要求 7）。

- 检查点按“已完成请求标识 + 已核对结论”持久化；
- 恢复时只从最后一个完整检查点继续；结构版本 / 策略版本 /
  服务摘要版本任一变化即整体重建，不做增量续接；
- 检查点与报告均先写临时文件再重命名整体替换；
- 同工作目录并发写入以文件锁（O_EXCL 锁文件）串行化，锁冲突抛 E_LOCK。
"""

from __future__ import annotations

import json
import os
import time

from . import errors

CHECKPOINT_VERSION = 1


class LockError(Exception):
    """文件锁冲突，业务错误码 E_LOCK。"""

    code = errors.E_LOCK

    def __init__(self, path):
        super().__init__("文件锁冲突: %s" % path)
        self.path = path


class FileLock:
    """基于 O_CREAT|O_EXCL 锁文件的跨进程/跨线程文件锁。"""

    def __init__(self, target_path):
        self.lock_path = target_path + ".lock"
        self._fd = None

    def __enter__(self):
        try:
            self._fd = os.open(self.lock_path,
                               os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise LockError(self.lock_path)
        os.write(self._fd, str(os.getpid()).encode("ascii"))
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            os.unlink(self.lock_path)
        except FileNotFoundError:
            pass
        return False


def atomic_write_json(path, obj):
    """先写临时文件再重命名整体替换。"""
    tmp_path = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)


def atomic_write_json_locked(path, obj):
    """在文件锁保护下原子写；锁冲突抛 LockError（E_LOCK）。"""
    with FileLock(path):
        atomic_write_json(path, obj)


class CheckpointStore:
    """工作目录下的检查点存取。"""

    def __init__(self, workdir):
        self.workdir = workdir
        self.path = os.path.join(workdir, "checkpoint.json")

    def load(self, policy_version, service_summary_version):
        """加载最后一个完整检查点；版本不匹配时返回 None（整体重建）。"""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        if not isinstance(state, dict):
            return None
        if state.get("checkpoint_version") != CHECKPOINT_VERSION:
            return None
        if state.get("policy_version") != policy_version:
            return None
        if state.get("service_summary_version") != service_summary_version:
            return None
        if not isinstance(state.get("completed"), dict):
            return None
        return state

    def save(self, policy_version, service_summary_version, completed,
             rejected_entries):
        state = {
            "checkpoint_version": CHECKPOINT_VERSION,
            "policy_version": policy_version,
            "service_summary_version": service_summary_version,
            "completed": completed,
            "rejected_entries": rejected_entries,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        atomic_write_json_locked(self.path, state)
        return state
