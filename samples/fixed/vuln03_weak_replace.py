# 修复3：弃用黑名单替换，改用 basename + commonpath 白名单式校验
import os


def save_upload(upload_dir, filename, content):
    safe_name = os.path.basename(filename)
    upload_root = os.path.realpath(upload_dir)
    target = os.path.realpath(os.path.join(upload_root, safe_name))
    if os.path.commonpath([upload_root, target]) != upload_root:
        raise ValueError("非法文件名：路径越界")
    with open(target, "wb") as fh:
        fh.write(content)
    return target
