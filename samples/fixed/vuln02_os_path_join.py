# 修复2：PurePath.name 取纯文件名 + is_relative_to 边界校验（Python 3.9+）
import os
from pathlib import Path


def save_upload(upload_dir, filename, content):
    safe_name = Path(filename).name
    root = Path(upload_dir).resolve()
    target = (root / safe_name).resolve()
    if not target.is_relative_to(root):
        raise ValueError("非法文件名：路径越界")
    with open(target, "wb") as fh:
        fh.write(content)
    return str(target)
