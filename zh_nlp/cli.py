"""命令行入口：python -m zh_nlp.cli --config ... --input ... --output ..."""
from __future__ import annotations

import argparse

from .config import PipelineConfig
from .pipeline import Pipeline, read_input


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="本地确定性中文分词与词性标注流水线")
    parser.add_argument("--config", required=True, help="JSON 配置文件")
    parser.add_argument("--input", required=True, help="UTF-8 输入样本文件")
    parser.add_argument("--output", required=True, help="阶段文件与报告目录")
    args = parser.parse_args(argv)

    config = PipelineConfig.from_file(args.config)
    sentences = read_input(args.input)
    pipeline = Pipeline(config, args.output)
    return pipeline.run(sentences)


if __name__ == "__main__":
    raise SystemExit(main())
