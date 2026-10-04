"""上传页：本地音频同音替换预处理 + 上传到 Suno。

Suno API 的 JSON 调用通过 QWebEngine 页面上下文执行（Python TLS 会被拦截），
只有 S3 multipart 上传在 Python 线程里用 requests 完成。
"""

import json
from pathlib import Path

import requests

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.logger import get_logger
from app.obfuscator import STRENGTHS, process, similarity, slice_audio

log = get_logger("upload")

API_BASE = "https://studio-api-prod.suno.com"
INIT_URL = f"{API_BASE}/api/uploads/audio"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


class ObfuscateWorker(QThread):
    """后台同音替换：相位替换 + 幅度微扰，听感与原音频高度相似。"""

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


class SliceWorker(QThread):
    """后台切片：把混淆后的超长 WAV 切成多段。"""

    done = Signal(list)
    failed = Signal(str)

    def __init__(self, src: str, out_dir: str, max_sec: int, parent=None) -> None:
        super().__init__(parent)
        self.src = src
        self.out_dir = out_dir
        self.max_sec = max_sec

    def run(self) -> None:  # noqa: D102
        try:
            parts = slice_audio(self.src, self.out_dir, self.max_sec)
            self.done.emit([str(p) for p in parts])
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class UploadPage(QWidget):
    clip_initialized = Signal(str)

    def __init__(self, api, parent=None) -> None:
        super().__init__(parent)
        self.api = api
        self._src: str | None = None
        self._processed: str | None = None
        self._segments: list[str] = []
        self._obf_worker: ObfuscateWorker | None = None
        self._slice_worker: SliceWorker | None = None
        self._s3_worker: S3UploadWorker | None = None
        self._current_file: str | None = None
        self._queue: list[str] = []
        self._queue_index = 0
        self._upload_id: str | None = None
        self._upload_filename: str | None = None
        self._poll_attempt = 0
        self._retry_count = 0
        self._auto_upload = False
        self._batch_queue: list[str] = []
        self._batch_active = False
        self._batch_total = 0
        self._batch_done = 0
        self._batch_strength_label = "强对抗"
        self._prev_strength_label = "低"

        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(14)

        title = QLabel("上传")
        title.setObjectName("pageTitle")
        desc = QLabel("选择本地音频，先做同音替换预处理，再上传到 Suno")
        desc.setObjectName("pageDesc")

        file_row = QHBoxLayout()
        self.file_label = QLabel("未选择文件（可拖拽 WAV/MP3/FLAC/OGG/AIFF）")
        self.file_label.setObjectName("pageDesc")
        choose_btn = QPushButton("选择文件")
        file_row.addWidget(self.file_label, 1)
        file_row.addWidget(choose_btn)

        opt_row = QHBoxLayout()
        opt_row.addWidget(QLabel("替换强度"))
        self.strength = QComboBox()
        self.strength.addItems(list(STRENGTHS.keys()))
        self.strength.setCurrentText("低")
        self.process_btn = QPushButton("同音替换")
        self.process_btn.setObjectName("primary")
        self.upload_btn = QPushButton("上传到 Suno")
        opt_row.addWidget(self.strength)
        opt_row.addWidget(self.process_btn)
        opt_row.addWidget(self.upload_btn)
        opt_row.addStretch(1)

        slice_row = QHBoxLayout()
        self.slice_enabled = QCheckBox("超长自动切片")
        self.slice_sec = QSpinBox()
        self.slice_sec.setRange(10, 600)
        self.slice_sec.setValue(480)
        self.slice_sec.setSuffix(" 秒")
        slice_row.addWidget(self.slice_enabled)
        slice_row.addWidget(QLabel("每段最长"))
        slice_row.addWidget(self.slice_sec)
        slice_row.addStretch(1)

        retry_row = QHBoxLayout()
        self.retry_enabled = QCheckBox("内容检测命中时自动重新混淆并重传")
        self.retry_enabled.setChecked(True)
        self.retry_max = QSpinBox()
        self.retry_max.setRange(1, 10)
        self.retry_max.setValue(3)
        self.retry_max.setSuffix(" 次")
        retry_row.addWidget(self.retry_enabled)
        retry_row.addWidget(QLabel("最多"))
        retry_row.addWidget(self.retry_max)
        retry_row.addStretch(1)

        batch_row = QHBoxLayout()
        self.batch_add_btn = QPushButton("批量添加文件")
        self.batch_clear_btn = QPushButton("清空队列")
        self.batch_start_btn = QPushButton("开始批量（强对抗+自动重试）")
        self.batch_start_btn.setObjectName("primary")
        self.batch_label = QLabel("队列：0 个文件")
        self.batch_label.setObjectName("pageDesc")
        batch_row.addWidget(self.batch_add_btn)
        batch_row.addWidget(self.batch_clear_btn)
        batch_row.addWidget(self.batch_start_btn)
        batch_row.addWidget(self.batch_label, 1)

        self.log_view = QTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)

        layout.addWidget(title)
        layout.addWidget(desc)
        layout.addLayout(file_row)
        layout.addLayout(opt_row)
        layout.addLayout(slice_row)
        layout.addLayout(retry_row)
        layout.addLayout(batch_row)
        layout.addWidget(self.log_view, 1)

        choose_btn.clicked.connect(self._choose_file)
        self.process_btn.clicked.connect(lambda: self._process(auto=False))
        self.upload_btn.clicked.connect(self._upload)
        self.upload_btn.setEnabled(False)
        self.batch_add_btn.clicked.connect(self._batch_add_files)
        self.batch_clear_btn.clicked.connect(self._batch_clear)
        self.batch_start_btn.clicked.connect(self._batch_start)

    # ---- 文件 ----

    def _choose_file(self) -> None:
        if self._batch_active:
            self._append_log("批量任务进行中，请等待完成后再选择文件")
            return
        selected, _ = QFileDialog.getOpenFileName(
            self, "选择音频", "", "音频 (*.wav *.mp3 *.flac *.ogg *.aiff)"
        )
        if selected:
            self._set_src(selected)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.toLocalFile().lower().endswith(
                (".wav", ".mp3", ".flac", ".ogg", ".aiff")
            )
        ]
        if not paths:
            return
        if len(paths) == 1:
            self._set_src(paths[0])
            return
        for path in paths:
            if path not in self._batch_queue:
                self._batch_queue.append(path)
        self._append_log(f"拖入 {len(paths)} 个文件，已加入批量队列（点“开始批量”）")
        self._update_batch_label()

    def _set_src(self, path: str) -> None:
        self._src = path
        self._processed = None
        self._segments = []
        self._queue = []
        self._retry_count = 0
        self._auto_upload = False
        self.file_label.setText(path)
        self.upload_btn.setEnabled(not self._batch_active)
        self._append_log(f"已选择：{path}")

    # ---- 同音替换 ----

    def _process(self, auto: bool = False) -> None:
        if not self._src:
            self._append_log("请先选择音频文件")
            return
        if not auto:
            if self._batch_active:
                self._append_log("批量任务进行中，请等待完成后再手动处理")
                return
            # 手动处理：重置自动重试状态
            self._retry_count = 0
            self._auto_upload = False
        strength = STRENGTHS[self.strength.currentText()]
        dst = str(Path(self._src).with_suffix(".obf.wav"))
        self.process_btn.setEnabled(False)
        self.upload_btn.setEnabled(False)
        self._append_log(f"开始同音替换（强度={self.strength.currentText()}）")
        self._obf_worker = ObfuscateWorker(self._src, dst, strength, self)
        self._obf_worker.done.connect(self._on_obf_done)
        self._obf_worker.failed.connect(self._on_obf_failed)
        self._obf_worker.finished.connect(self._on_obf_finished)
        self._obf_worker.start()

    def _on_obf_done(self, path: str) -> None:
        self._processed = path
        self._segments = []
        if self._src and Path(path).exists():
            sim = similarity(self._src, path)
            self._append_log(f"同音替换完成：{path}")
            self._append_log(f"替换前后指纹相似度：{sim}%")
            tier = self.strength.currentText()
            if sim >= 90.0:
                self._append_log("✓ 听感与原音频高度相似")
            elif tier in ("对抗", "对抗+", "强对抗", "强对抗+"):
                self._append_log(
                    f"ℹ 对抗档已做变速变调+合唱，相似度 {sim}% 属预期，可继续上传"
                )
            else:
                self._append_log(f"⚠ 相似度 {sim}% 低于 90%，请检查原音频后重试")
            if self.slice_enabled.isChecked():
                self._start_slice(path)
            elif self._auto_upload:
                self._auto_upload = False
                self._append_log("自动上传混淆后的音频")
                self._upload()
            else:
                self.upload_btn.setEnabled(True)

    def _start_slice(self, path: str) -> None:
        max_sec = self.slice_sec.value()
        out_dir = Path(path).with_name(Path(path).stem + "_slices")
        self._append_log(f"开始切片：每段最长 {max_sec} 秒")
        self.upload_btn.setEnabled(False)
        self._slice_worker = SliceWorker(path, str(out_dir), max_sec, self)
        self._slice_worker.done.connect(self._on_slice_done)
        self._slice_worker.failed.connect(self._on_slice_failed)
        self._slice_worker.finished.connect(self._on_slice_finished)
        self._slice_worker.start()

    def _on_slice_done(self, parts: list) -> None:
        self._segments = [str(p) for p in parts]
        if self._segments:
            self._append_log(f"切片完成：{len(self._segments)} 段")
        else:
            self._append_log("音频未超过设定时长，无需切片，将上传完整文件")
        if self._auto_upload:
            self._auto_upload = False
            self._append_log("自动上传混淆后的音频")
            self._upload()
        else:
            self.upload_btn.setEnabled(True)

    def _on_slice_failed(self, error: str) -> None:
        self._segments = []
        self._append_log(f"切片失败：{error}（将回退为上传完整文件）")
        if self._auto_upload:
            self._auto_upload = False
            self._upload()
        else:
            self.upload_btn.setEnabled(True)

    def _on_slice_finished(self) -> None:
        if self._slice_worker:
            self._slice_worker.deleteLater()
            self._slice_worker = None

    def _on_obf_failed(self, error: str) -> None:
        self._auto_upload = False
        self._append_log(f"同音替换失败：{error}")
        if self._batch_active:
            self._on_batch_file_done(False)

    def _on_obf_finished(self) -> None:
        if self._obf_worker:
            self._obf_worker.deleteLater()
            self._obf_worker = None
        self.process_btn.setEnabled(True)

    # ---- 上传 ----

    def _upload(self) -> None:
        if self._segments:
            self._queue = list(self._segments)
        elif self._processed:
            self._queue = [self._processed]
        elif self._src:
            self._queue = [self._src]
        else:
            self._append_log("请先选择并处理音频")
            return
        self._queue_index = 0
        self._append_log(f"开始上传，共 {len(self._queue)} 个文件")
        self._start_current_upload()

    def _start_current_upload(self) -> None:
        file_path = self._queue[self._queue_index]
        self._current_file = file_path
        self.upload_btn.setEnabled(False)
        prefix = (
            f"[{self._queue_index + 1}/{len(self._queue)}] "
            if len(self._queue) > 1
            else ""
        )
        self._append_log(f"{prefix}准备上传：{file_path}")
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
        file_path = self._current_file or self._processed or self._src
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
            self._retry_or_fail(payload)
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
        self._append_log(f"上传成功：clip_id={clip_id}（{self._current_file}）")
        self.clip_initialized.emit(clip_id)
        self._advance_queue()

    def _advance_queue(self) -> None:
        if self._queue_index + 1 < len(self._queue):
            self._queue_index += 1
            QTimer.singleShot(600, self._start_current_upload)
            return
        if len(self._queue) > 1:
            self._append_log(f"全部 {len(self._queue)} 个切片上传完成")
        if self._batch_active:
            self._on_batch_file_done(True)
        else:
            self.upload_btn.setEnabled(True)

    def _fail_upload(self, error: str) -> None:
        if len(self._queue) > 1:
            self._append_log(
                f"已上传 {self._queue_index}/{len(self._queue)} 个文件，本次已停止"
            )
        self._append_log(f"上传失败：{error}")
        if self._batch_active:
            self._on_batch_file_done(False)
        else:
            self.upload_btn.setEnabled(True)

    def _retry_or_fail(self, payload: dict) -> None:
        """内容检测类失败时自动重新混淆（换随机种子）并重传。

        Audible Magic / ACRCloud / 歌词版权检测对同一处理结果可能时过
        时不过（随机相位/包络每次不同），自动重试能显著提高成功率。
        """
        error_type = payload.get("error_type", "") or ""
        matchable = (
            error_type.startswith("upload_failure_match")
            or error_type == "upload_failure_lyrics_copyright"
        )
        if (
            self.retry_enabled.isChecked()
            and matchable
            and self._retry_count < self.retry_max.value()
        ):
            self._retry_count += 1
            self._append_log(
                f"命中内容检测（{error_type}），自动重新混淆并重传"
                f"（{self._retry_count}/{self.retry_max.value()}）"
            )
            self._auto_upload = True
            self._process(auto=True)
            return
        self._fail_upload(f"上传处理失败：{payload}")

    # ---- 批量 ----

    def _batch_add_files(self) -> None:
        selected, _ = QFileDialog.getOpenFileNames(
            self, "批量选择音频", "", "音频 (*.wav *.mp3 *.flac *.ogg *.aiff)"
        )
        if not selected:
            return
        added = 0
        for path in selected:
            if path not in self._batch_queue:
                self._batch_queue.append(path)
                added += 1
        self._append_log(f"批量队列新增 {added} 个文件")
        self._update_batch_label()

    def _batch_clear(self) -> None:
        if self._batch_active:
            self._append_log("批量任务进行中，无法清空队列")
            return
        count = len(self._batch_queue)
        self._batch_queue = []
        self._append_log(f"已清空批量队列（{count} 个文件）")
        self._update_batch_label()

    def _batch_start(self) -> None:
        if self._batch_active:
            self._append_log("批量任务已在进行中")
            return
        if not self._batch_queue:
            self._append_log("批量队列为空，请先添加文件")
            return
        self._batch_active = True
        self._batch_total = len(self._batch_queue)
        self._batch_done = 0
        self._prev_strength_label = self.strength.currentText()
        self.batch_add_btn.setEnabled(False)
        self.batch_clear_btn.setEnabled(False)
        self.batch_start_btn.setEnabled(False)
        self._append_log(
            f"开始批量处理：共 {self._batch_total} 个文件"
            f"（强度={self._batch_strength_label}，"
            f"命中自动重试最多 {self.retry_max.value()} 次）"
        )
        self._batch_next()

    def _batch_next(self) -> None:
        if self._batch_queue:
            path = self._batch_queue.pop(0)
            self._append_log(
                f"[批量 {self._batch_done + 1}/{self._batch_total}] 开始处理：{path}"
            )
            self.strength.setCurrentText(self._batch_strength_label)
            self._set_src(path)
            self._auto_upload = True
            self._retry_count = 0
            self._update_batch_label()
            self._process(auto=True)
            return
        # 队列清空：批量结束
        self._batch_active = False
        self.strength.setCurrentText(self._prev_strength_label)
        self.batch_add_btn.setEnabled(True)
        self.batch_clear_btn.setEnabled(True)
        self.batch_start_btn.setEnabled(True)
        self._update_batch_label()
        self._append_log(f"批量处理结束：共 {self._batch_total} 个文件")

    def _on_batch_file_done(self, success: bool) -> None:
        self._batch_done += 1
        name = Path(self._src).name if self._src else "?"
        mark = "✓ 成功" if success else "✗ 失败"
        self._append_log(f"[批量 {self._batch_done}/{self._batch_total}] {mark}：{name}")
        QTimer.singleShot(800, self._batch_next)

    def _update_batch_label(self) -> None:
        self.batch_label.setText(f"队列：{len(self._batch_queue)} 个文件")

    def _append_log(self, text: str) -> None:
        self.log_view.append(text)
