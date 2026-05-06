import logging
import os
from logging.handlers import RotatingFileHandler

def setup_logging(log_dir: str = "logs", level: str = "INFO") -> None:
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "app.log")

    root = logging.getLogger()
    if root.handlers:
        return

    lvl = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(lvl)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    sh = logging.StreamHandler()
    sh.setLevel(lvl)
    sh.setFormatter(fmt)

    fh = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setLevel(lvl)
    fh.setFormatter(fmt)

    root.addHandler(sh)
    root.addHandler(fh)

    logging.getLogger("telethon").setLevel(lvl)
    logging.getLogger("aiogram").setLevel(lvl)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
