"""底部播放条：在 Suno 页面上下文里解密并播放 m4a-opus。

Suno 的 media_urls 是 AES 加密的碎片 MP4。解密需要：
  1. POST /api/mango/rights 取 key/iv
  2. userKey = SHA-256(Clerk JWT)
  3. 用 AES-GCM(additionalData=clipId) 解开 key/iv
  4. 下载加密媒体后 AES-CTR 解密整段
  5. 结果 Blob -> HTMLAudioElement 播放
复用 AuthService 的页面上下文（suno.com），避免 CORS 和 cookie 问题。
"""

import json

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWebEngineCore import QWebEngineSettings
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


# 一次性注入：创建全局 audio 并定义解密播放函数。
_INIT_JS = r"""
if (!window.__chilliAudio) {
  window.__chilliAudio = new Audio();
  window.__chilliAudio.preload = "auto";
  document.body.appendChild(window.__chilliAudio);
}
window.__chilliPlay = async function(clipId, encUrl) {
  const gen = (window.__chilliGen = (window.__chilliGen || 0) + 1);
  window.__chilliPerr = null;
  window.__chilliPlaying = false;
  const a = window.__chilliAudio;
  try {
    a.pause();
    const cr = await fetch("https://auth.suno.com/v1/client", { credentials: "include" });
    if (!cr.ok) throw new Error("clerk-http-" + cr.status);
    const client = await cr.json();
    const sessions = (client && client.response && client.response.sessions)
                     || (client && client.sessions) || [];
    let jwt = null;
    for (const s of sessions) {
      const t = s.last_active_token && s.last_active_token.jwt;
      if (t) { jwt = t; break; }
    }
    if (!jwt) throw new Error("no-session");

    const rr = await fetch("https://studio-api-prod.suno.com/api/mango/rights", {
      method: "POST",
      headers: { "Authorization": "Bearer " + jwt, "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({
        content_params: { content_id: clipId, content_type: "clip" }
      })
    });
    if (!rr.ok) throw new Error("rights-http-" + rr.status);
    const rights = await rr.json();
    if (!rights.key || !rights.iv) throw new Error("no-key-iv");

    const b64ToBytes = (b64) =>
      Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const digest = await crypto.subtle.digest(
      "SHA-256", new TextEncoder().encode(jwt)
    );
    const userKey = await crypto.subtle.importKey(
      "raw", digest, { name: "AES-GCM" }, false, ["decrypt"]
    );
    const unwrap = async (b64) => {
      const w = b64ToBytes(b64);
      return new Uint8Array(await crypto.subtle.decrypt(
        { name: "AES-GCM", iv: w.slice(0, 12),
          additionalData: new TextEncoder().encode(clipId) },
        userKey,
        w.slice(12)
      ));
    };
    const rawKey = await unwrap(rights.key);
    const rawIv = await unwrap(rights.iv);
    const aesKey = await crypto.subtle.importKey(
      "raw", rawKey, { name: "AES-CTR" }, false, ["decrypt"]
    );

    const mr = await fetch(encUrl);
    if (!mr.ok) throw new Error("media-http-" + mr.status);
    const data = new Uint8Array(await mr.arrayBuffer());

    // 16-byte counter block，按 64KB 分块解密；counter 每次前进一个 16B 块。
    const incCounter = (iv12, add) => {
      const out = new Uint8Array(16);
      out.set(iv12);
      if (add === 0) return out;
      let n = 0n;
      for (let i = 0; i < 16; i++) n = (n << 8n) | BigInt(out[i]);
      n += BigInt(add);
      for (let i = 15; i >= 0; i--) { out[i] = Number(n & 255n); n >>= 8n; }
      return out;
    };
    const chunkSize = 65536;
    const out = [];
    let carry = new Uint8Array(0);
    let counter = 0;
    for (let pos = 0; pos < data.length; pos += chunkSize) {
      const slice = data.slice(pos, pos + chunkSize);
      const merged = new Uint8Array(carry.length + slice.length);
      merged.set(carry);
      merged.set(slice, carry.length);
      const fullLen = 16 * Math.floor(merged.length / 16);
      if (fullLen > 0) {
        const ctr = incCounter(rawIv, counter);
        const dec = new Uint8Array(await crypto.subtle.decrypt(
          { name: "AES-CTR", counter: ctr, length: 128 },
          aesKey,
          merged.buffer.slice(merged.byteOffset, merged.byteOffset + fullLen)
        ));
        out.push(dec);
        counter += fullLen / 16;
      }
      carry = merged.slice(fullLen);
    }
    if (carry.length > 0) {
      const ctr = incCounter(rawIv, counter);
      out.push(new Uint8Array(await crypto.subtle.decrypt(
        { name: "AES-CTR", counter: ctr, length: 128 },
        aesKey,
        carry.buffer.slice(carry.byteOffset, carry.byteOffset + carry.byteLength)
      )));
    }
    let total = 0;
    for (const c of out) total += c.length;
    const dec = new Uint8Array(total);
    let off = 0;
    for (const c of out) { dec.set(c, off); off += c.length; }

    if (window.__chilliUrl) URL.revokeObjectURL(window.__chilliUrl);
    const blob = new Blob([dec], { type: "audio/mp4" });
    const url = URL.createObjectURL(blob);
    window.__chilliUrl = url;
    a.src = url;
    a.load();
    const p = a.play();
    if (p !== undefined) {
      p.then(() => {
        if (gen === window.__chilliGen) {
          window.__chilliPlaying = true;
          window.__chilliPerr = null;
        }
      }).catch((e) => {
        if (gen === window.__chilliGen) {
          window.__chilliPerr = String(e);
          window.__chilliPlaying = false;
        }
      });
    }
  } catch (e) {
    if (gen === window.__chilliGen) {
      window.__chilliPerr = String(e);
      window.__chilliPlaying = false;
    }
  }
};
true
"""

_STATE_JS = (
    "JSON.stringify({t:window.__chilliAudio.currentTime,"
    "d:window.__chilliAudio.duration,"
    "paused:window.__chilliAudio.paused,"
    "ended:window.__chilliAudio.ended,"
    "err:window.__chilliPerr||null})"
)


class PlayerBar(QWidget):
    def __init__(self, auth, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("playerBar")

        self.auth = auth
        self._playlist: list[dict] = []
        self._index = -1
        self._playing = False
        self._pending: tuple[dict, str] | None = None
        self._page = None
        self._ready = False

        auth.profile.settings().setAttribute(
            QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False
        )

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
        self.hide()

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
        self._pending = (clip, url)
        self._ensure_page()

    def _ensure_page(self, attempt: int = 0) -> None:
        page = self.auth.acquire_page()
        if page is None:
            if attempt >= 60:
                log.warning("player page never ready")
                self.title_label.setText("播放页面未就绪")
                return
            QTimer.singleShot(500, lambda: self._ensure_page(attempt + 1))
            return
        self._page = page
        page.runJavaScript(_INIT_JS)
        self._ready = True
        if self._pending:
            clip, url = self._pending
            self._pending = None
            self._start_play(clip, url)

    def _start_play(self, clip: dict, url: str) -> None:
        page = self._page
        if page is None:
            return
        js = "window.__chilliPlay(%s,%s);" % (
            json.dumps(clip["id"]),
            json.dumps(url),
        )
        page.runJavaScript(js)
        self._playing = True
        self.play_btn.setText("⏸")
        self.show()

    def _toggle(self) -> None:
        page = self._page
        if page is None:
            return
        if self._playing:
            page.runJavaScript("window.__chilliAudio.pause();")
            self._playing = False
            self.play_btn.setText("▶")
        elif self._index >= 0:
            page.runJavaScript("window.__chilliAudio.play();")
            self._playing = True
            self.play_btn.setText("⏸")

    def _step(self, delta: int) -> None:
        if not self._playlist:
            return
        self._index = (self._index + delta) % len(self._playlist)
        self._load_current()

    def _set_volume(self, v: int) -> None:
        page = self._page
        if page is None:
            return
        page.runJavaScript("window.__chilliAudio.volume=%s;" % (v / 100))

    def _on_seek_released(self) -> None:
        self._seeking = False
        page = self._page
        if page is None:
            return
        sec = self.progress.value()
        page.runJavaScript("window.__chilliAudio.currentTime=%s;" % sec)

    def _poll_state(self) -> None:
        if not self._ready or self._page is None:
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
