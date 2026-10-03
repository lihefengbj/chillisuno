"""日志模块：文件日志 + Qt 信号（供界面日志面板实时显示）。"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QObject, QStandardPaths, Signal


class LogBus(QObject):
    """把日志记录转发到 UI。"""

    record = Signal(str, str)  # (levelname, message)


class _QtHandler(logging.Handler):
    def __init__(self, bus: LogBus) -> None:
        super().__init__()
        self.bus = bus

    def emit(self, record: logging.LogRecord) -> None:
        self.bus.record.emit(record.levelname, record.getMessage())


bus = LogBus()


def setup_logging() -> Path:
    data_dir = Path(
        QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation
        )
    )
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "chillisuno.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root.addHandler(file_handler)

    qt_handler = _QtHandler(bus)
    qt_handler.setLevel(logging.DEBUG)
    root.addHandler(qt_handler)

    logging.getLogger(__name__).info("log file: %s", log_file)
    return log_file


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
