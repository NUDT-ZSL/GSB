# 修复5：服务端 uuid 重命名 + Path.resolve/is_relative_to 双重约束
import uuid
from pathlib import Path


def save_upload(upload_dir, filename, content):
    root = Path(upload_dir).resolve()
    safe_name = uuid.uuid4().hex
    target = (root / safe_name).resolve()
    if not target.is_relative_to(root):
        raise ValueError("非法文件名：路径越界")
    target.write_bytes(content)
    return str(target)
