# 修复1：basename 剥离目录分量 + resolve/commonpath 边界校验
import os


class _Request:
    def __init__(self, form):
        self.form = form


request = _Request(form={})


def save_upload(upload_dir, filename, content):
    name = request.form.get("filename") or filename
    safe_name = os.path.basename(name)
    upload_root = os.path.realpath(upload_dir)
    target = os.path.realpath(os.path.join(upload_root, safe_name))
    if os.path.commonpath([upload_root, target]) != upload_root:
        raise ValueError("非法文件名：路径越界")
    with open(target, "wb") as fh:
        fh.write(content)
    return target
