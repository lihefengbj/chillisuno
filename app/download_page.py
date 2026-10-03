"""下载中心：队列下载 Suno 加密音频并解密落盘。"""

import base64
import hashlib
import json
import re
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from PySide6.QtCore import QStandardPaths, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.logger import get_logger

log = get_logger("download")

API_BASE = "https://studio-api-prod.suno.com"
RIGHTS_URL = f"{API_BASE}/api/mango/rights"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


def _sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    return name[:120] or "untitled"


def _default_dir() -> Path:
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.DownloadLocation
    )
    return Path(base) / "chillisuno"


def _post_json(url: str, token: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def _decrypt(token: str, clip_id: str, enc_data: bytes, rights: dict) -> bytes:
    key = base64.b64decode(rights["key"])
    iv = base64.b64decode(rights["iv"])
    user_key = hashlib.sha256(token.encode("utf-8")).digest()
    raw_key = AESGCM(user_key).decrypt(
        key[:12], key[12:], clip_id.encode("utf-8")
    )
    raw_iv = AESGCM(user_key).decrypt(
        iv[:12], iv[12:], clip_id.encode("utf-8")
    )
    # 与 JS 侧一致：12 字节 content IV 零扩展到 16 字节 counter；
    # 部分版本直接下发 16 字节 IV，则原样使用。
    if len(raw_iv) == 12:
        counter = raw_iv + b"\x00\x00\x00\x00"
    elif len(raw_iv) == 16:
        counter = raw_iv
    else:
        raise ValueError(f"unexpected content IV length: {len(raw_iv)}")
    decryptor = Cipher(
        algorithms.AES(raw_key), modes.CTR(counter)
    ).decryptor()
    return decryptor.update(enc_data) + decryptor.finalize()


class DownloadWorker(QThread):
    progress = Signal(int, str)  # percent, status text
    succeeded = Signal(dict, str)  # clip, saved path
    failed = Signal(dict, str)  # clip, error

    def __init__(self, token: str, clip: dict, directory: Path, parent=None) -> None:
        super().__init__(parent)
        self.token = token
        self.clip = clip
        self.directory = directory

    def run(self) -> None:  # noqa: D102
        clip = self.clip
        title = clip.get("title") or "未命名"
        clip_id = clip.get("id")
        enc_url = clip.get("audio_url") or ""
        if not clip_id or not enc_url:
            self.failed.emit(clip, "missing id/audio_url")
            return
        try:
            self.progress.emit(0, "获取授权")
            rights = _post_json(
                RIGHTS_URL,
                self.token,
                {
                    "content_params": {
                        "content_id": clip_id,
                        "content_type": "clip",
                    }
                },
            )
            if not rights.get("key") or not rights.get("iv"):
                self.failed.emit(clip, "rights missing key/iv")
                return

            self.progress.emit(5, "下载加密音频")
            req = urllib.request.Request(enc_url)
            req.add_header("User-Agent", UA)
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                buf = bytearray()
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if total:
                        pct = 5 + int(len(buf) / total * 80)
                        self.progress.emit(pct, "下载加密音频")

            self.progress.emit(88, "解密")
            plain = _decrypt(self.token, clip_id, bytes(buf), rights)

            self.progress.emit(96, "写入文件")
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / (f"{_sanitize(title)}.m4a")
            if path.exists():
                path = self.directory / (
                    f"{_sanitize(title)}_{clip_id[:8]}.m4a"
                )
            path.write_bytes(plain)
            self.progress.emit(100, "完成")
            self.succeeded.emit(clip, str(path))
        except Exception as exc:  # noqa: BLE001
            log.warning("download failed: %s", exc)
            self.failed.emit(clip, str(exc))


class DownloadPage(QWidget):
    def __init__(self, api, parent=None) -> None:
        super().__init__(parent)
        self.api = api
        self.directory = _default_dir()
        self._queue: list[dict] = []
        self._worker: DownloadWorker | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(14)

        title = QLabel("下载中心")
        title.setObjectName("pageTitle")

        toolbar = QHBoxLayout()
        self.dir_label = QLabel(str(self.directory))
        self.dir_label.setObjectName("pageDesc")
        choose_btn = QPushButton("选择目录")
        open_btn = QPushButton("打开目录")
        clear_btn = QPushButton("清空已完成")
        toolbar.addWidget(self.dir_label, 1)
        toolbar.addWidget(choose_btn)
        toolbar.addWidget(open_btn)
        toolbar.addWidget(clear_btn)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["曲名", "状态", "进度", "保存位置"])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)

        layout.addWidget(title)
        layout.addLayout(toolbar)
        layout.addWidget(self.table, 1)

        choose_btn.clicked.connect(self._choose_dir)
        open_btn.clicked.connect(self._open_dir)
        clear_btn.clicked.connect(self._clear_done)

    # ---- 对外 ----

    def add_clip(self, clip: dict) -> None:
        self._queue.append(clip)
        self._append_row(clip)
        self._start_next()

    def add_clips(self, clips: list[dict]) -> None:
        for clip in clips:
            self._queue.append(clip)
            self._append_row(clip)
        self._start_next()

    # ---- 内部 ----

    def _update_row(self, clip_id: str, status: str, pct: int = -1, path: str = "") -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == clip_id:
                if status:
                    self.table.item(row, 1).setText(status)
                if pct >= 0:
                    self.table.item(row, 2).setText(f"{pct}%")
                if path:
                    self.table.item(row, 3).setText(path)
                return

    def _append_row(self, clip: dict) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        item = QTableWidgetItem(clip.get("title") or "")
        item.setData(Qt.ItemDataRole.UserRole, clip.get("id"))
        self.table.setItem(row, 0, item)
        self.table.setItem(row, 1, QTableWidgetItem("排队中"))
        self.table.setItem(row, 2, QTableWidgetItem("0%"))
        self.table.setItem(row, 3, QTableWidgetItem(""))

    def _start_next(self) -> None:
        if self._worker is not None:
            return
        if not self._queue:
            return
        clip = self._queue.pop(0)
        clip_id = clip.get("id")
        self._update_row(clip_id, "获取令牌")
        self.api.fetch_token(
            lambda token, c=clip, cid=clip_id: self._on_token(token, c, cid)
        )

    def _on_token(self, token: str | None, clip: dict, clip_id: str) -> None:
        if not token:
            self._update_row(clip_id, "获取令牌失败")
            self._finish_one(clip, None, "token-failed")
            return
        self._update_row(clip_id, "下载中")
        self._worker = DownloadWorker(token, clip, self.directory, self)
        self._worker.progress.connect(
            lambda pct, status, cid=clip_id: self._update_row(cid, status, pct)
        )
        self._worker.succeeded.connect(self._on_success)
        self._worker.failed.connect(self._on_failure)
        self._worker.finished.connect(self._on_worker_done)
        self._worker.start()

    def _on_success(self, clip: dict, path: str) -> None:
        self._update_row(clip.get("id"), "完成", 100, path)

    def _on_failure(self, clip: dict, error: str) -> None:
        self._update_row(clip.get("id"), f"失败：{error[:80]}")

    def _on_worker_done(self) -> None:
        if self._worker:
            self._worker.deleteLater()
            self._worker = None
        QTimer.singleShot(0, self._start_next)

    def _finish_one(self, clip: dict, path: str | None, error: str) -> None:
        if path:
            self._on_success(clip, path)
        else:
            self._on_failure(clip, error)
        self._on_worker_done()

    def _choose_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "选择下载目录", str(self.directory)
        )
        if selected:
            self.directory = Path(selected)
            self.dir_label.setText(str(self.directory))

    def _open_dir(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.directory)))

    def _clear_done(self) -> None:
        rows = self.table.rowCount()
        for row in range(rows - 1, -1, -1):
            status = self.table.item(row, 1).text()
            if status in ("完成",) or status.startswith("失败"):
                self.table.removeRow(row)
