# 修复4：扩展名校验之外补充 basename 与 resolve 边界校验
import os

ALLOWED_EXT = (".png", ".jpg", ".jpeg")


def save_upload(upload_dir, filename, content):
    if not filename.endswith(ALLOWED_EXT):
        raise ValueError("仅允许上传图片文件")
    safe_name = os.path.basename(filename)
    upload_root = os.path.realpath(upload_dir)
    target = os.path.realpath(os.path.join(upload_root, safe_name))
    if os.path.commonpath([upload_root, target]) != upload_root:
        raise ValueError("非法文件名：路径越界")
    with open(target, "wb") as fh:
        fh.write(content)
    return target
