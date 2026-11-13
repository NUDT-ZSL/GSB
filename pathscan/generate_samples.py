"""确定性样本生成器（仅标准库）。

在指定工作区下生成：
  <workspace>/base_dir/                下载基目录（含若干正常文件）
  <workspace>/outside/SECRET_MARKER.txt  基目录外标记文件
  <workspace>/samples/test_paths.json  测试路径集合（确定性顺序与内容）
  <workspace>/samples/corrupt.json     损坏 JSON（用于错误处理验收）

重复执行生成的文件内容完全一致。
"""
from __future__ import annotations

import argparse
import json
import os

MARKER_DIR = "outside"
MARKER_NAME = "SECRET_MARKER.txt"
MARKER_CONTENT = (
    "PATHSCAN-SECRET-MARKER-7f3a91\n"
    "此文件位于下载基目录之外，任何成功读取其内容的响应均判定为高风险。\n"
)

# 确定性测试路径集合（顺序固定，便于复核与重复运行比对）。
SAMPLE_PATHS: list[str] = [
    "docs/readme.txt",
    "docs/../docs/readme.txt",
    "../outside/SECRET_MARKER.txt",
    "..%2foutside%2fSECRET_MARKER.txt",
    "%2e%2e%2foutside%2fSECRET_MARKER.txt",
    "..%252foutside%252fSECRET_MARKER.txt",
    "missing/no_such_file.txt",
]

BASE_FILES: dict[str, str] = {
    "docs/readme.txt": "这是基目录内的正常文件 readme。\n",
    "docs/notes.txt": "基目录内笔记 notes。\n",
    "images/logo.txt": "基目录内占位文件 logo。\n",
}

CORRUPT_JSON = '{"paths": ["docs/readme.txt",\n'  # 截断的非法 JSON


def generate(workspace: str) -> dict[str, str]:
    base_dir = os.path.join(workspace, "base_dir")
    outside_dir = os.path.join(workspace, MARKER_DIR)
    samples_dir = os.path.join(workspace, "samples")

    for rel, content in BASE_FILES.items():
        full = os.path.join(base_dir, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)

    os.makedirs(outside_dir, exist_ok=True)
    marker_path = os.path.join(outside_dir, MARKER_NAME)
    with open(marker_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(MARKER_CONTENT)

    os.makedirs(samples_dir, exist_ok=True)
    samples_path = os.path.join(samples_dir, "test_paths.json")
    with open(samples_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(SAMPLE_PATHS, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    corrupt_path = os.path.join(samples_dir, "corrupt.json")
    with open(corrupt_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(CORRUPT_JSON)

    return {
        "base_dir": base_dir,
        "marker_path": marker_path,
        "samples_path": samples_path,
        "corrupt_path": corrupt_path,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="generate_samples", description="生成确定性测试样本"
    )
    parser.add_argument(
        "--workspace", default="workspace",
        help="样本工作区目录，默认 ./workspace",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = generate(os.path.abspath(args.workspace))
    print("样本已生成：")
    for key, value in paths.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
