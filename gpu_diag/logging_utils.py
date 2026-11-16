"""本地文件日志工具，不依赖任何外部服务。"""

import logging
import os

_DEFAULT_LOG_DIR = os.path.join(os.getcwd(), "logs")
_CONFIGURED = {}


def get_logger(name="gpu_diag", log_dir=None):
    logger = logging.getLogger(name)
    if name in _CONFIGURED:
        return logger
    log_dir = log_dir or os.environ.get("GPU_DIAG_LOG_DIR", _DEFAULT_LOG_DIR)
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "{}.log".format(name))
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    _CONFIGURED[name] = log_path
    return logger
