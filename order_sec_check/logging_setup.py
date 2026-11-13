"""日志配置：同时输出到控制台与日志文件，凭据经 Filter 兜底脱敏。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from .redact import SecretRedactionFilter


def setup_logger(log_dir: Path, secrets: list[str], verbose: bool = True) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "security_check.log"
    logger = logging.getLogger("order_sec_check")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    redaction_filter = SecretRedactionFilter(secrets)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redaction_filter)
    logger.addHandler(file_handler)

    if verbose:
        # Windows 控制台默认 GBK，统一把标准流切到 UTF-8，避免中文日志乱码
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8", errors="replace")
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(redaction_filter)
        logger.addHandler(stream_handler)

    return logger, log_path
