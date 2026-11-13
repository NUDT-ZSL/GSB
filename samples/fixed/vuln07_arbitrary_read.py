# 修复7：读取侧也做 basename + 边界校验，防止任意文件读取
from pathlib import Path


def save_upload(upload_dir, filename, content):
    root = Path(upload_dir).resolve()
    safe_name = Path(filename).name
    target = (root / safe_name).resolve()
    if not target.is_relative_to(root):
        raise ValueError("非法文件名：路径越界")
    data = target.read_bytes()
    return data
