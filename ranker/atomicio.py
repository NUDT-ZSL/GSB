"""跨平台原子写盘与整体替换。

临时文件与目标文件始终位于同一目录，保证 os.replace 是同卷原子重命名。
Windows 上 os.replace 会直接替换已存在的目标文件，语义与 POSIX 一致。
"""

import json
import os
import tempfile


def _tmp_path(path):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-" + os.path.basename(path) + "-", dir=directory)
    return fd, tmp


def atomic_write_bytes(path, data):
    fd, tmp = _tmp_path(path)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, obj):
    data = (json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    atomic_write_bytes(path, data)


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
