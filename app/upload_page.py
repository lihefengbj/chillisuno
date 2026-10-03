"""上传页：本地音频混淆预处理 + 上传到 Suno。"""

from pathlib import Path

import requests

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.logger import get_logger
from app.obfuscator import STRENGTHS, process, similarity

log = get_logger("upload")

API_BASE = "https://studio-api-prod.suno.com"
INIT_URL = f"{API_BASE}/api/uploads/audio"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


class ObfuscateWorker(QThread):
    done = Signal(str)  # 处理后文件路径
    failed = Signal(str)

    def __init__(self, src: str, dst: str, strength: int, parent=None) -> None:
        super().__init__(parent)
        self.src = src
        self.dst = dst
        self.strength = strength

    def run(self) -> None:  # noqa: D102
        try:
            out = process(self.src, self.dst, self.strength)
            self.done.emit(str(out))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class UploadWorker(QThread):
    progress = Signal(str)
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, token: str, file_path: str, parent=None) -> None:
        super().__init__(parent)
        self.token = token
        self.file_path = file_path

    def run(self) -> None:  # noqa: D102
        path = Path(self.file_path)
        ext = path.suffix.lstrip(".").lower() or "wav"
        filename = path.name
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "User-Agent": UA,
        }
        try:
            self.progress.emit("获取上传授权")
            init = requests.post(
                INIT_URL,
                headers=headers,
                json={"extension": ext},
                timeout=30,
            )
            init.raise_for_status()
            info = init.json()
            upload_id = info.get("id")
            upload_url = info.get("url")
            fields = info.get("fields") or {}
            if not upload_id or not upload_url or not fields.get("key"):
                self.failed.emit("上传授权响应缺少 id/url/key")
                return

            self.progress.emit("上传到对象存储")
            form = {
                k: v for k, v in fields.items()
                if k not in ("file",)
            }
            content_type = fields.get("Content-Type") or f"audio/{ext}"
            with path.open("rb") as fh:
                files = {"file": (filename, fh, content_type)}
                resp = requests.post(
                    upload_url,
                    data=form,
                    files=files,
                    headers={"User-Agent": UA},
                    timeout=180,
                )
            if resp.status_code >= 400:
                self.failed.emit(f"S3 上传失败 HTTP {resp.status_code}")
                return

            self.progress.emit("通知 Suno 上传完成")
            finish = requests.post(
                f"{API_BASE}/api/uploads/audio/{upload_id}/upload-finish",
                headers=headers,
                json={
                    "upload_type": "file_upload",
                    "upload_filename": filename,
                },
                timeout=30,
            )
            finish.raise_for_status()

            self.progress.emit("等待 Suno 处理")
            payload = {}
            for _ in range(20):
                poll = requests.get(
                    f"{API_BASE}/api/uploads/audio/{upload_id}",
                    headers=headers,
                    timeout=30,
                )
                poll.raise_for_status()
                payload = poll.json()
                status = payload.get("status")
                if status in ("complete", "failed", "error"):
                    break
                QThread.sleep(2)
            if payload.get("status") != "complete":
                self.failed.emit(f"上传处理未完成：{payload}")
                return
            self.progress.emit("上传成功")
            self.succeeded.emit(payload)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class UploadPage(QWidget):
    def __init__(self, api, parent=None) -> None:
        super().__init__(parent)
        self.api = api
        self._src: str | None = None
        self._processed: str | None = None
        self._obf_worker: ObfuscateWorker | None = None
        self._upload_worker: UploadWorker | None = None

        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(14)

        title = QLabel("上传")
        title.setObjectName("pageTitle")
        desc = QLabel("选择本地音频，先做混淆预处理，再上传到 Suno")
        desc.setObjectName("pageDesc")

        file_row = QHBoxLayout()
        self.file_label = QLabel("未选择文件（可拖拽 WAV/MP3/FLAC/OGG/AIFF）")
        self.file_label.setObjectName("pageDesc")
        choose_btn = QPushButton("选择文件")
        file_row.addWidget(self.file_label, 1)
        file_row.addWidget(choose_btn)

        opt_row = QHBoxLayout()
        opt_row.addWidget(QLabel("混淆强度"))
        self.strength = QComboBox()
        self.strength.addItems(list(STRENGTHS.keys()))
        self.strength.setCurrentText("低")
        self.process_btn = QPushButton("混淆处理")
        self.process_btn.setObjectName("primary")
        self.upload_btn = QPushButton("上传到 Suno")
        opt_row.addWidget(self.strength)
        opt_row.addWidget(self.process_btn)
        opt_row.addWidget(self.upload_btn)
        opt_row.addStretch(1)

        self.log_view = QTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)

        layout.addWidget(title)
        layout.addWidget(desc)
        layout.addLayout(file_row)
        layout.addLayout(opt_row)
        layout.addWidget(self.log_view, 1)

        choose_btn.clicked.connect(self._choose_file)
        self.process_btn.clicked.connect(self._process)
        self.upload_btn.clicked.connect(self._upload)
        self.upload_btn.setEnabled(False)

    # ---- 文件 ----

    def _choose_file(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "选择音频", "", "音频 (*.wav *.mp3 *.flac *.ogg *.aiff)"
        )
        if selected:
            self._set_src(selected)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith((".wav", ".mp3", ".flac", ".ogg", ".aiff")):
                self._set_src(path)
                break

    def _set_src(self, path: str) -> None:
        self._src = path
        self._processed = None
        self.file_label.setText(path)
        self.upload_btn.setEnabled(False)
        self._append_log(f"已选择：{path}")

    # ---- 处理/上传 ----

    def _process(self) -> None:
        if not self._src:
            self._append_log("请先选择音频文件")
            return
        strength = STRENGTHS[self.strength.currentText()]
        dst = str(Path(self._src).with_suffix(".obf.wav"))
        self.process_btn.setEnabled(False)
        self._append_log(f"开始混淆（强度={self.strength.currentText()}）")
        self._obf_worker = ObfuscateWorker(self._src, dst, strength, self)
        self._obf_worker.done.connect(self._on_obf_done)
        self._obf_worker.failed.connect(self._on_obf_failed)
        self._obf_worker.finished.connect(self._on_obf_finished)
        self._obf_worker.start()

    def _on_obf_done(self, path: str) -> None:
        self._processed = path
        if self._src and Path(path).exists():
            sim = similarity(self._src, path)
            self._append_log(f"混淆完成：{path}")
            self._append_log(f"混淆前后指纹相似度：{sim}%")
            self.upload_btn.setEnabled(True)

    def _on_obf_failed(self, error: str) -> None:
        self._append_log(f"混淆失败：{error}")

    def _on_obf_finished(self) -> None:
        if self._obf_worker:
            self._obf_worker.deleteLater()
            self._obf_worker = None
        self.process_btn.setEnabled(True)

    def _upload(self) -> None:
        file_path = self._processed or self._src
        if not file_path:
            self._append_log("请先选择并处理音频")
            return
        self.upload_btn.setEnabled(False)
        self._append_log("获取登录令牌…")
        self.api.fetch_token(self._on_token)

    def _on_token(self, token: str | None) -> None:
        file_path = self._processed or self._src
        if not token:
            self._append_log("获取令牌失败，无法上传")
            self.upload_btn.setEnabled(True)
            return
        self._append_log("开始上传")
        self._upload_worker = UploadWorker(token, file_path, self)
        self._upload_worker.progress.connect(self._append_log)
        self._upload_worker.succeeded.connect(self._on_upload_success)
        self._upload_worker.failed.connect(self._on_upload_failed)
        self._upload_worker.finished.connect(self._on_upload_finished)
        self._upload_worker.start()

    def _on_upload_success(self, payload: dict) -> None:
        self._append_log(f"上传成功：{payload}")

    def _on_upload_failed(self, error: str) -> None:
        self._append_log(f"上传失败：{error}")

    def _on_upload_finished(self) -> None:
        if self._upload_worker:
            self._upload_worker.deleteLater()
            self._upload_worker = None
        self.upload_btn.setEnabled(True)

    def _append_log(self, text: str) -> None:
        self.log_view.append(text)
