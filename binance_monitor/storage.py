from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


DATA_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent.parent) / "data"
LOG_DIR = DATA_DIR.parent / "logs"


def load_config(path: Path | None = None) -> dict:
    path = path or DATA_DIR / "config.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("配置文件必须为 JSON 对象。")
    return data


def save_config(data: dict, path: Path | None = None):
    path = path or DATA_DIR / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement avoids truncating a working configuration on interruption.
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def get_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("binance_monitor")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(LOG_DIR / "monitor.log", maxBytes=2_000_000,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
        logger.addHandler(handler)
    return logger
