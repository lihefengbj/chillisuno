"""底部播放条：通过内嵌 Chromium 页面播放音频。

Qt 的 QMediaPlayer 在 Windows 上（WMF 后端）不支持 Opus，而 Suno 的
media_urls 是 m4a-opus。改用隐藏 QWebEnginePage + HTMLAudioElement，
借助 Chromium 原生 Opus 解码。
"""

import json

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QWidget,
)

from app.logger import get_logger

log = get_logger("player")


def _fmt(sec: float) -> str:
    s = int(max(0, sec))
    return f"{s // 60}:{s % 60:02d}"


# 一次性注入：创建全局 audio 元素
_INIT_JS = """
if (!window.__chilliAudio) {
  window.__chilliAudio = new Audio();
  window.__chilliAudio.preload = "auto";
  document.body.appendChild(window.__chilliAudio);
}
true
"""


def _play_js(url: str) -> str:
    return (
        "(function(){var a=window.__chilliAudio;window.__chilliPerr=null;"
        "a.pause();a.src=%s;a.load();"
        "a.play().then(function(){window.__chilliStarted=true;})"
        ".catch(function(e){window.__chilliPerr=String(e);});})();true"
        % json.dumps(url)
    )


_STATE_JS = (
    "JSON.stringify({t:window.__chilliAudio.currentTime,"
    "d:window.__chilliAudio.duration,"
    "paused:window.__chilliAudio.paused,"
    "ended:window.__chilliAudio.ended,"
    "err:window.__chilliPerr||null})"
)


class PlayerBar(QWidget):
    def __init__(self, profile, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("playerBar")

        self._playlist: list[dict] = []
        self._index = -1
        self._playing = False
        self._pending_url: str | None = None
        self._ready = False

        # 隐藏播放页：复用登录 profile（cookie 上下文）
        self._page = QWebEnginePage(profile, self)
        self._page.settings().setAttribute(
            QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False
        )
        self._page.loadFinished.connect(self._on_loaded)
        self._page.load(QUrl("about:blank"))

        # UI
        self.title_label = QLabel("未在播放")
        self.title_label.setObjectName("playerTitle")
        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setObjectName("playerTime")

        self.prev_btn = QPushButton("⏮")
        self.play_btn = QPushButton("▶")
        self.play_btn.setObjectName("playBtn")
        self.next_btn = QPushButton("⏭")
        for b in (self.prev_btn, self.play_btn, self.next_btn):
            b.setFixedSize(36, 36)

        self.progress = QSlider(Qt.Orientation.Horizontal)
        self.progress.setRange(0, 0)
        self._seeking = False

        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(90)

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 8, 16, 8)
        row.setSpacing(12)
        row.addWidget(self.title_label, 1)
        row.addWidget(self.prev_btn)
        row.addWidget(self.play_btn)
        row.addWidget(self.next_btn)
        row.addWidget(self.progress, 2)
        row.addWidget(self.time_label)
        row.addWidget(self.volume)

        self.play_btn.clicked.connect(self._toggle)
        self.prev_btn.clicked.connect(lambda: self._step(-1))
        self.next_btn.clicked.connect(lambda: self._step(1))
        self.volume.valueChanged.connect(self._set_volume)
        self.progress.sliderPressed.connect(
            lambda: setattr(self, "_seeking", True)
        )
        self.progress.sliderReleased.connect(self._on_seek_released)

        # 状态轮询
        self._poll = QTimer(self)
        self._poll.setInterval(500)
        self._poll.timeout.connect(self._poll_state)
        self._poll.start()

    # ---- 对外 ----

    def play_clip(self, clip: dict, playlist: list[dict] | None = None) -> None:
        self._playlist = playlist or [clip]
        self._index = next(
            (i for i, c in enumerate(self._playlist) if c["id"] == clip["id"]), 0
        )
        self._load_current()

    # ---- 内部 ----

    def _load_current(self) -> None:
        clip = self._playlist[self._index]
        url = clip.get("audio_url") or ""
        log.info("play: %s (%s)", clip["title"], url)
        self.title_label.setText(clip["title"])
        if not url:
            self.title_label.setText("无可用音频地址")
            return
        if not self._ready:
            self._pending_url = url
            return
        self._page.runJavaScript(_play_js(url))
        self._playing = True
        self.play_btn.setText("⏸")

    def _on_loaded(self, ok: bool) -> None:
        if not ok:
            log.warning("player page load failed")
            return
        self._page.runJavaScript(_INIT_JS)
        self._ready = True
        if self._pending_url:
            url = self._pending_url
            self._pending_url = None
            self._page.runJavaScript(_play_js(url))
            self._playing = True
            self.play_btn.setText("⏸")

    def _toggle(self) -> None:
        if self._playing:
            self._page.runJavaScript("window.__chilliAudio.pause();")
            self._playing = False
            self.play_btn.setText("▶")
        elif self._index >= 0:
            self._page.runJavaScript("window.__chilliAudio.play();")
            self._playing = True
            self.play_btn.setText("⏸")

    def _step(self, delta: int) -> None:
        if not self._playlist:
            return
        self._index = (self._index + delta) % len(self._playlist)
        self._load_current()

    def _set_volume(self, v: int) -> None:
        self._page.runJavaScript(
            "window.__chilliAudio.volume=%s;" % (v / 100)
        )

    def _on_seek_released(self) -> None:
        self._seeking = False
        sec = self.progress.value()
        self._page.runJavaScript(
            "window.__chilliAudio.currentTime=%s;" % sec
        )

    def _poll_state(self) -> None:
        if not self._ready:
            return
        self._page.runJavaScript(_STATE_JS, self._on_state)

    def _on_state(self, raw) -> None:
        try:
            s = json.loads(raw) if raw else {}
        except (TypeError, json.JSONDecodeError):
            return
        if not s:
            return
        if s.get("err") and self._playing:
            log.warning("play error: %s", s["err"])
            self.title_label.setText("播放失败")
            self._playing = False
            self.play_btn.setText("▶")
            return
        d = s.get("d") or 0
        t = s.get("t") or 0
        if d and self.progress.maximum() != int(d):
            self.progress.setRange(0, int(d))
        if not self._seeking:
            self.progress.setValue(int(t))
        self.time_label.setText(f"{_fmt(t)} / {_fmt(d)}")
        if s.get("ended"):
            self._step(1)
