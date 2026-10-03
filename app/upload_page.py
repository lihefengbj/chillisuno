"""上传页：本地音频混淆预处理 + 上传到 Suno。

Suno API 的 JSON 调用通过 QWebEngine 页面上下文执行（Python TLS 会被拦截），
只有 S3 multipart 上传在 Python 线程里用 requests 完成。
"""

import json
from pathlib import Path

import requests

from PySide6.QtCore import Qt, QThread, QTimer, Signal
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
    done = Signal(str)
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


class S3UploadWorker(QThread):
    """只负责把本地文件 multipart 上传到 Suno 的 S3 预签名地址。"""

    done = Signal()
    failed = Signal(str)

    def __init__(self, upload_url: str, fields: dict, file_path: str, parent=None) -> None:
        super().__init__(parent)
        self.upload_url = upload_url
        self.fields = fields
        self.file_path = file_path

    def run(self) -> None:  # noqa: D102
        path = Path(self.file_path)
        filename = path.name
        content_type = self.fields.get("Content-Type") or "audio/wav"
        form = {k: v for k, v in self.fields.items() if k != "file"}
        try:
            with path.open("rb") as fh:
                files = {"file": (filename, fh, content_type)}
                resp = requests.post(
                    self.upload_url,
                    data=form,
                    files=files,
                    headers={"User-Agent": UA},
                    timeout=180,
                )
            if resp.status_code >= 400:
                self.failed.emit(f"S3 上传失败 HTTP {resp.status_code}")
                return
            self.done.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class UploadPage(QWidget):
    clip_initialized = Signal(str)

    def __init__(self, api, parent=None) -> None:
        super().__init__(parent)
        self.api = api
        self._src: str | None = None
        self._processed: str | None = None
        self._obf_worker: ObfuscateWorker | None = None
        self._s3_worker: S3UploadWorker | None = None
        self._upload_id: str | None = None
        self._upload_filename: str | None = None
        self._poll_attempt = 0

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
        self.upload_btn.setEnabled(True)
        self._append_log(f"已选择：{path}")

    # ---- 混淆 ----

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

    # ---- 上传 ----

    def _upload(self) -> None:
        file_path = self._processed or self._src
        if not file_path:
            self._append_log("请先选择并处理音频")
            return
        self.upload_btn.setEnabled(False)
        self._append_log(f"准备上传：{file_path}")
        self._append_log("获取上传授权")
        ext = Path(file_path).suffix.lstrip(".").lower() or "wav"
        self._upload_filename = Path(file_path).name
        self.api.request_json(
            "POST", INIT_URL, {"extension": ext}, self._on_init
        )

    def _on_init(self, resp: dict) -> None:
        if not resp.get("ok"):
            self._fail_upload(f"获取授权失败 HTTP {resp.get('status')} {resp.get('text','')[:200]}")
            return
        try:
            info = json.loads(resp.get("text") or "{}")
        except json.JSONDecodeError:
            self._fail_upload(f"授权响应不是 JSON：{resp.get('text','')[:200]}")
            return
        upload_id = info.get("id")
        upload_url = info.get("url")
        fields = info.get("fields") or {}
        if not upload_id or not upload_url or not fields.get("key"):
            self._fail_upload(f"授权响应缺少 id/url/key：{info}")
            return
        self._upload_id = upload_id
        self._append_log("上传到对象存储")
        file_path = self._processed or self._src
        self._s3_worker = S3UploadWorker(upload_url, fields, file_path, self)
        self._s3_worker.done.connect(self._on_s3_done)
        self._s3_worker.failed.connect(self._fail_upload)
        self._s3_worker.finished.connect(self._on_s3_finished)
        self._s3_worker.start()

    def _on_s3_done(self) -> None:
        self._append_log("通知 Suno 上传完成")
        self.api.request_json(
            "POST",
            f"{API_BASE}/api/uploads/audio/{self._upload_id}/upload-finish",
            {
                "upload_type": "file_upload",
                "upload_filename": self._upload_filename,
            },
            self._on_finish,
        )

    def _on_s3_finished(self) -> None:
        if self._s3_worker:
            self._s3_worker.deleteLater()
            self._s3_worker = None

    def _on_finish(self, resp: dict) -> None:
        if not resp.get("ok"):
            self._fail_upload(f"upload-finish 失败 HTTP {resp.get('status')} {resp.get('text','')[:200]}")
            return
        self._append_log("等待 Suno 处理")
        self._poll_attempt = 0
        self._poll()

    def _poll(self) -> None:
        if self._poll_attempt >= 20:
            self._fail_upload("等待处理超时")
            return
        self.api.request_json(
            "GET",
            f"{API_BASE}/api/uploads/audio/{self._upload_id}",
            None,
            self._on_poll,
        )

    def _on_poll(self, resp: dict) -> None:
        if not resp.get("ok"):
            self._fail_upload(f"查询状态失败 HTTP {resp.get('status')} {resp.get('text','')[:200]}")
            return
        try:
            payload = json.loads(resp.get("text") or "{}")
        except json.JSONDecodeError:
            self._fail_upload(f"状态响应不是 JSON：{resp.get('text','')[:200]}")
            return
        status = payload.get("status")
        if status == "complete":
            # 上传处理完成只是素材就绪，还要 initialize-clip 才会在曲库
            # 生成真正的 clip，否则刷新曲库看不到这条上传。
            self._append_log("素材处理完成，正在初始化曲库条目…")
            self._initialize_clip()
            return
        if status in ("failed", "error"):
            self._fail_upload(f"上传处理失败：{payload}")
            return
        self._poll_attempt += 1
        QTimer.singleShot(2000, self._poll)

    def _initialize_clip(self) -> None:
        self.api.request_json(
            "POST",
            f"{API_BASE}/api/uploads/audio/{self._upload_id}/initialize-clip",
            {},
            self._on_clip_init,
        )

    def _on_clip_init(self, resp: dict) -> None:
        if not resp.get("ok"):
            self._fail_upload(
                f"initialize-clip 失败 HTTP {resp.get('status')} "
                f"{resp.get('text','')[:200]}"
            )
            return
        try:
            payload = json.loads(resp.get("text") or "{}")
        except json.JSONDecodeError:
            self._fail_upload(
                f"initialize-clip 响应不是 JSON：{resp.get('text','')[:200]}"
            )
            return
        clip_id = payload.get("clip_id")
        if not clip_id:
            self._fail_upload(f"initialize-clip 缺少 clip_id：{payload}")
            return
        self._append_log(f"上传成功：clip_id={clip_id}")
        self.clip_initialized.emit(clip_id)
        self.upload_btn.setEnabled(True)

    def _fail_upload(self, error: str) -> None:
        self._append_log(f"上传失败：{error}")
        self.upload_btn.setEnabled(True)

    def _append_log(self, text: str) -> None:
        self.log_view.append(text)
