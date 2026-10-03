"""本地音频混淆：微变速、频域扰动、噪声、重写为 WAV。

不依赖 librosa/scipy，只使用 numpy + soundfile，避免打包体积过大。
混淆后的指纹相似度用于自测，不是版权检测工具。
"""

from pathlib import Path

import numpy as np
import soundfile as sf


def _read_audio(path: str | Path) -> tuple[np.ndarray, int]:
    path = Path(path)
    if path.suffix.lower() == ".mp3":
        import miniaudio

        # decode_file 在 Windows 上对中文/非 ASCII 路径会返回 -1，
        # 这里直接喂字节给 miniaudio.decode，绕开文件名编码问题。
        decoded = miniaudio.decode(
            path.read_bytes(), output_format=miniaudio.SampleFormat.FLOAT32
        )
        samples = np.asarray(decoded.samples, dtype=np.float32).reshape(
            decoded.num_frames, decoded.nchannels
        )
        return samples, int(decoded.sample_rate)
    data, sr = sf.read(str(path), always_2d=True)
    if data.dtype not in (np.float32, np.float64):
        data = data.astype(np.float64) / float(np.iinfo(data.dtype).max)
    return data, int(sr)


STRENGTHS = {
    "关闭": 0,
    "低": 1,
    "中": 2,
    "高": 3,
}


def _mono(data: np.ndarray) -> np.ndarray:
    if data.ndim == 1:
        return data
    return np.mean(data, axis=1)


def _fingerprint_bits(data: np.ndarray, sr: int) -> np.ndarray:
    mono = _mono(data)
    max_samples = sr * 30
    if len(mono) > max_samples:
        mono = mono[:max_samples]
    if len(mono) < sr:
        return np.zeros(0, dtype=np.uint8)

    n_fft = 1024
    hop = 512
    frames = []
    for start in range(0, len(mono) - n_fft, hop):
        frame = mono[start:start + n_fft] * np.hanning(n_fft)
        spec = np.abs(np.fft.rfft(frame))
        frames.append(spec)
    if len(frames) < 16:
        return np.zeros(0, dtype=np.uint8)

    spec = np.stack(frames)
    # 24 个对数频段
    bins = spec.shape[1]
    freqs = np.geomspace(1, bins - 1, 25).astype(int)
    bands = np.zeros((spec.shape[0], 24), dtype=np.float64)
    for i in range(24):
        bands[:, i] = spec[:, freqs[i]:freqs[i + 1]].sum(axis=1)
    bands = np.log1p(bands)
    # 每个频段跨时间用中位数二值化
    thresholds = np.median(bands, axis=0)
    bits = (bands > thresholds).astype(np.uint8)
    return bits.flatten()


def fingerprint(path: str | Path) -> bytes:
    data, sr = _read_audio(path)
    bits = _fingerprint_bits(data, sr)
    if bits.size == 0:
        return b""
    return np.packbits(bits).tobytes()


def similarity(path_a: str | Path, path_b: str | Path) -> float:
    """返回 0-100 的指纹相似度。长度不同时比较共同位数。"""
    fp_a = fingerprint(path_a)
    fp_b = fingerprint(path_b)
    if not fp_a or not fp_b:
        return 0.0
    min_len = min(len(fp_a), len(fp_b))
    bits_a = np.unpackbits(np.frombuffer(fp_a[:min_len], dtype=np.uint8))
    bits_b = np.unpackbits(np.frombuffer(fp_b[:min_len], dtype=np.uint8))
    total = bits_a.size
    if total == 0:
        return 0.0
    same = int(np.sum(bits_a == bits_b))
    return round(same / total * 100, 1)


def process(src: str | Path, dst: str | Path, strength: int) -> Path:
    """按强度混淆，写出 WAV，返回目标路径。"""
    src = Path(src)
    dst = Path(dst)
    data, sr = _read_audio(src)
    if strength <= 0:
        sf.write(str(dst), data, sr, subtype="PCM_16")
        return dst

    rng = np.random.default_rng()

    # 1) 音高平移：这是对抗"整曲指纹匹配"的关键一步。仅做 0.2% 变速
    #    或轻微 EQ 对内容识别基本无效。
    semitones = (0.0, 0.6, 1.5, 3.0)[min(strength, 3)]
    if semitones > 0:
        data = _pitch_shift(data, sr, 2 ** (semitones / 12.0))

    # 2) 时域微变速：进一步破坏节拍/相位对齐
    factor = 1.0 + 0.0015 * strength
    n_in = data.shape[0]
    n_out = int(n_in * factor)
    x_in = np.linspace(0.0, 1.0, n_in)
    x_out = np.linspace(0.0, 1.0, n_out)
    stretched = np.stack(
        [np.interp(x_out, x_in, data[:, c]) for c in range(data.shape[1])],
        axis=1,
    )

    # 3) STFT 相位扰动：强度越高越明显，改变波形但保留大致频谱包络
    if strength >= 2:
        phase_sigma = (0.0, 0.0, 0.55, 1.25)[min(strength, 3)]
        stretched = _stft_phase_jitter(stretched, sr, phase_sigma, rng)

    # 4) 频域 EQ + 残留相位微扰
    n = stretched.shape[0]
    X = np.fft.rfft(stretched, axis=0)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    curve = 1.0 + 0.02 * strength * np.sin(
        freqs / (sr / 2.0) * np.pi * (1.0 + 0.3 * strength)
    )
    X *= curve[:, None]
    if strength >= 3:
        phase = rng.normal(0.0, 0.015 * strength, X.shape)
        X *= np.exp(1j * phase)
    stretched = np.fft.irfft(X, n=n, axis=0)

    # 5) 低幅度噪声
    scale = float(np.std(stretched) or 1.0)
    stretched += rng.normal(0.0, 0.00003 * strength * scale, stretched.shape)

    # 6) 归一化并写 WAV
    peak = float(np.max(np.abs(stretched)) or 1.0)
    if peak > 0.99:
        stretched *= 0.99 / peak
    sf.write(str(dst), stretched, sr, subtype="PCM_16")
    return dst


def _pitch_shift(data: np.ndarray, sr: int, ratio: float) -> np.ndarray:
    """无外部依赖的保时长音高平移。

    先按 ratio 重采样内容，再映射回原时间轴，等效于把整段音频整体
    移调，但时长保持不变。ratio > 1 表示升高音高。
    """
    if abs(ratio - 1.0) < 1e-6:
        return data
    n = data.shape[0]
    work = data

    # 升调时目标 Nyquist 会变低，先做简单 FFT 低通，减少混叠。
    if ratio > 1.0:
        X = np.fft.rfft(work, axis=0)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        cutoff = sr / (2.0 * ratio) * 0.97
        roll = np.clip((freqs - cutoff) / max(cutoff * 0.04, 1.0), 0.0, None)
        mask = 1.0 / (1.0 + np.exp(roll))
        X *= mask[:, None]
        work = np.fft.irfft(X, n=n, axis=0)

    idx = np.clip(np.arange(n) * ratio, 0.0, float(n - 1))
    base = np.arange(n, dtype=np.float64)
    shifted = np.stack(
        [np.interp(idx, base, work[:, c]) for c in range(work.shape[1])],
        axis=1,
    )
    return shifted.astype(np.float32)


def _stft_phase_jitter(
    data: np.ndarray, sr: int, sigma: float, rng: np.random.Generator
) -> np.ndarray:
    """对 STFT 相位加高斯抖动，保留幅度谱，主要破坏波形级指纹。"""
    if sigma <= 0 or data.shape[0] < 2048:
        return data

    n_fft = 2048
    hop = 512
    win = np.hanning(n_fft)
    out = np.zeros_like(data, dtype=np.float64)
    norm = np.zeros(data.shape[0], dtype=np.float64)

    for c in range(data.shape[1]):
        sig = data[:, c]
        pos = 0
        while pos + n_fft <= data.shape[0]:
            frame = sig[pos:pos + n_fft] * win
            X = np.fft.rfft(frame)
            mag = np.abs(X)
            ang = np.angle(X) + rng.normal(0.0, sigma, mag.shape)
            y = np.fft.irfft(mag * np.exp(1j * ang), n=n_fft)
            out[pos:pos + n_fft, c] += y * win
            norm[pos:pos + n_fft] += win * win
            pos += hop

    out /= np.maximum(norm[:, None], 1e-8)
    return out.astype(np.float32)
