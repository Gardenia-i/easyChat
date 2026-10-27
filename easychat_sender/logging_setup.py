"""Console and rotating file logs for the local send service."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


DEFAULT_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "easychat.log"


def configure_logging(level: str = "INFO", log_path: Path = DEFAULT_LOG_PATH) -> Path | None:
    logger = logging.getLogger("easychat")
    if logger.handlers:
        return log_path

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s %(message)s"
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("无法创建文件日志 path=%s error=%s", log_path, exc)
        return None
    return log_path
