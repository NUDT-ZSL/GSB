# 样例5：pathlib Path 拼接后 write_bytes（高危）
from pathlib import Path


def save_upload(upload_dir, filename, content):
    target = Path(upload_dir) / filename
    target.write_bytes(content)
    return str(target)
