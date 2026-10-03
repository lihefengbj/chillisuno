"""SQLite 本地存储：收藏。"""

import sqlite3
from pathlib import Path

from PySide6.QtCore import QStandardPaths


class Storage:
    def __init__(self) -> None:
        data_dir = Path(
            QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppDataLocation
            )
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(data_dir / "chillisuno.db")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS favorites "
            "(clip_id TEXT PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        self._conn.commit()

    def is_favorite(self, clip_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM favorites WHERE clip_id=?", (clip_id,)
        )
        return cur.fetchone() is not None

    def toggle_favorite(self, clip_id: str) -> bool:
        """返回切换后的收藏状态。"""
        if self.is_favorite(clip_id):
            self._conn.execute(
                "DELETE FROM favorites WHERE clip_id=?", (clip_id,)
            )
            self._conn.commit()
            return False
        self._conn.execute(
            "INSERT INTO favorites (clip_id) VALUES (?)", (clip_id,)
        )
        self._conn.commit()
        return True

    def favorite_ids(self) -> set[str]:
        cur = self._conn.execute("SELECT clip_id FROM favorites")
        return {row[0] for row in cur.fetchall()}
