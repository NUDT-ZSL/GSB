# 修复6：copyfile 目标路径同样做 basename + commonpath 校验
import os
import shutil
import tempfile


def save_upload(upload_dir, filename, content):
    safe_name = os.path.basename(filename)
    upload_root = os.path.realpath(upload_dir)
    target = os.path.realpath(os.path.join(upload_root, safe_name))
    if os.path.commonpath([upload_root, target]) != upload_root:
        raise ValueError("非法文件名：路径越界")
    tmp = tempfile.NamedTemporaryFile(delete=False)
    try:
        tmp.write(content)
        tmp.close()
        shutil.copyfile(tmp.name, target)
    finally:
        os.unlink(tmp.name)
    return target
