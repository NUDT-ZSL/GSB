#!/usr/bin/env python3
"""
make_samples.py — 确定性生成扫描测试样本树（无需外部样本）。

样本内容与布局完全由本脚本决定，每次执行结果一致。生成内容：

  root/
    readme.txt              正常文本（不含任何关键字）
    evil_only.txt           仅含 EVIL        -> clean
    malware_only.txt        仅含 MALWARE     -> clean
    notes/mixed_case.txt    含 EvIl + MaLwArE（大小写混合，跨块无关）-> 恶意
    sub/deeper/payload.bin  同时含 EVIL 与 MALWARE -> 恶意（用于隔离）
    sub/deeper/empty.dat    0 字节           -> clean，空串 SHA-256
    .hidden_evil.txt        隐藏文件（含双关键字）-> 跳过，不出现在报告
    .hidden_dir/secret.txt  隐藏目录         -> 跳过
    link_to_readme.txt      指向 readme.txt 的符号链接 -> 跳过

用法：
    python -I make_samples.py --root <样本目录>
"""
from __future__ import annotations

import argparse
import os
import subprocess

FILES = {
    "readme.txt": b"This is a benign business document. Nothing suspicious here.\n",
    "evil_only.txt": b"Contains the word EVIL but not the other marker.\n",
    "malware_only.txt": b"Contains the word MALWARE but not the other marker.\n",
    # 大小写混合：验证大小写不敏感判定
    "notes/mixed_case.txt": b"Scattered markers: EvIl ... MaLwArE in one file.\n",
    "sub/deeper/payload.bin": b"BINARY\x00\x01 EVIL payload with MALWARE signature\x00\n",
    "sub/deeper/empty.dat": b"",
    # 隐藏项：即使含双关键字也必须被跳过
    ".hidden_evil.txt": b"EVIL and MALWARE inside a hidden file must be skipped.\n",
    ".hidden_dir/secret.txt": b"EVIL MALWARE inside a hidden directory must be skipped.\n",
}

LINK_NAME = "link_to_readme.txt"
LINK_TARGET = "readme.txt"
# 无符号链接权限时的降级方案：指向 notes 目录的 junction
JUNCTION_NAME = "junction_link"
JUNCTION_TARGET = "notes"


def build_tree(root):
    os.makedirs(root, exist_ok=True)
    for rel_path, content in FILES.items():
        dest = os.path.join(root, rel_path.replace("/", os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as handle:
            handle.write(content)

    link_path = os.path.join(root, LINK_NAME)
    link_status = "not created"
    try:
        if os.path.lexists(link_path):
            os.remove(link_path)
        os.symlink(os.path.abspath(os.path.join(root, LINK_TARGET)), link_path)
        link_status = "symlink created"
    except OSError as exc:
        # Windows 无创建符号链接权限时（非开发者模式/非管理员），
        # 降级为目录联接 junction（同样属于重解析点，扫描器必须跳过）
        junction_path = os.path.join(root, JUNCTION_NAME)
        junction_target = os.path.abspath(os.path.join(root, JUNCTION_TARGET))
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", junction_path, junction_target],
            capture_output=True, text=True)
        if result.returncode == 0:
            link_status = f"symlink unavailable ({type(exc).__name__}); junction created"
        else:
            link_status = f"both failed: {exc!r}; {result.stderr.strip()}"
    return link_status


def main(argv=None):
    parser = argparse.ArgumentParser(description="确定性生成扫描测试样本树")
    parser.add_argument("--root", required=True, help="样本目录（将被创建/覆盖写入）")
    args = parser.parse_args(argv)
    root = os.path.abspath(args.root)
    link_status = build_tree(root)
    print(f"samples created under {root}")
    print(f"regular files: {len(FILES)}, link: {link_status}")


if __name__ == "__main__":
    raise SystemExit(main())
