"""底部播放条：QMediaPlayer 封装。"""

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from app.logger import get_logger

log = get_logger("player")


def _fmt(ms: int) -> str:
    s = max(0, ms // 1000)
    return f"{s // 60}:{s % 60:02d}"


class PlayerBar(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("playerBar")

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._playlist: list[dict] = []
        self._index = -1

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

        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(90)
        self._audio.setVolume(0.8)

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
        self.volume.valueChanged.connect(
            lambda v: self._audio.setVolume(v / 100)
        )
        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(
            lambda d: self.progress.setRange(0, d)
        )
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self.progress.sliderMoved.connect(self._player.setPosition)

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
        log.info("play: %s (%s)", clip["title"], clip["audio_url"])
        self.title_label.setText(clip["title"])
        self._player.setSource(QUrl(clip["audio_url"]))
        self._player.play()
        self.play_btn.setText("⏸")

    def _toggle(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
            self.play_btn.setText("▶")
        elif self._index >= 0:
            self._player.play()
            self.play_btn.setText("⏸")

    def _step(self, delta: int) -> None:
        if not self._playlist:
            return
        self._index = (self._index + delta) % len(self._playlist)
        self._load_current()

    def _on_position(self, pos: int) -> None:
        if not self.progress.isSliderDown():
            self.progress.setValue(pos)
        self.time_label.setText(
            f"{_fmt(pos)} / {_fmt(self._player.duration())}"
        )

    def _on_media_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._step(1)
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            log.warning("invalid media")
            self.title_label.setText("播放失败")
