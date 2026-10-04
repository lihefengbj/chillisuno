"""本地音频混淆：同音替换（替换相位 + 幅度谱微扰）+ 重写为 WAV。

核心是同音替换：在临界采样的矩形分块频域（块长 1024、无重叠）内把
相位旋转为随机角度（DC/Nyquist 实系数翻转符号），每块重分析得到的
幅度谱与替换前严格一致（浮点精度量级）。人耳对相位不敏感，因此听感
与原音频高度相似；波形与相位级指纹被彻底改写。

针对 ACRCloud / Audible Magic 这类幅度谱峰级指纹，各档位在相位替换
之上叠加"微扰保真"层：对数频段上的平滑随机包络（±0.8~2.5 dB，慢层
宽带慢时变、快层逐块约 0.4 倍频程相关）打乱谱峰选择并在相邻频点间
重分配能量（等效谱峰位置微移），实验档再叠加 ±1% 幅度抖动。扰动
幅度控制在听感几乎不变的量级。

"对抗/对抗+"档在微扰保真之上再叠加经典对抗手段：整体变调变速
（+2%/+4% 重采样，重排全部时频对齐关系）与轻合唱（24 ms 调制延迟、
30%/45% 混合比，梳状滤波破坏谱峰结构）。听感轻微变化但仍是同一首
歌，时长相应缩短 2%/4%。

块边界的取值与斜率通过全部频点的相位最小范数求解保证连续，不产生
咔哒声。左右声道共享旋转角度、包络与修正量，因此单声道（左右平均）
幅度谱同样严格跟随包络，自测指纹相似度能如实反映替换后的保真程度。

不依赖 librosa/scipy，只使用 numpy + soundfile，避免打包体积过大。
混淆后的指纹相似度用于自测，不是版权检测工具。
"""

from pathlib import Path

import numpy as np
import soundfile as sf

# 分块长度（= 帧移，临界采样：每块 1024 个采样、513 个独立频点）
_BLOCK_N = 1024
_BLOCK_BINS = _BLOCK_N // 2 + 1


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
    "实验": 4,
    "对抗": 5,
    "对抗+": 6,
    "强对抗": 7,
    "强对抗+": 8,
}

# 各档位的同音替换参数：
#   jitter      原相位上叠加的高斯扰动幅度（弧度），"低/中"档使用
#   uniform     是否把相位完全替换为均匀随机角度（"高"及以上档位）
#   dither      是否叠加 ±1% 的幅度微扰
#   env_db      幅度谱微扰深度（dB）：对数频段平滑随机包络。
#               慢层（宽带、慢时变）攻击谱峰选择，快层（逐块、约 0.4 倍
#               频程相关）在相邻频点间重分配能量，等效谱峰位置随机微移；
#   resample    重采样比例（>1 整体变调变速）：重排全部时频对齐关系。
#               "对抗/对抗+"=2%/4%，"强对抗/强对抗+"=12%/16%（超出鲁棒
#               指纹系统约 ±10% 的容差窗口）
#   chorus_mix  轻合唱混合比例：梳状滤波破坏谱峰结构
#   wow_depth   wow/flutter 颤振深度：时变重采样率，破坏精细频率对齐
#   wow_rate    wow/flutter 调制速率（Hz）
#   bandpass    低保真带通 (lo, hi) Hz：削掉带外谱峰，改变整体频谱结构
#   echo_gain   回声增益：时域涂抹，破坏节拍级对齐
#   vocal_echo  人声带（250~3500 Hz）双回声增益：涂抹共振峰时序，
#               破坏歌词 ASR 识别（版权歌词检测），不影响带外音乐
#   vocal_emph  人声带包络强调倍率：快层包络在人声带额外加深，
#               逐块打乱共振峰结构，进一步破坏 ASR 特征
#   solve_iters 边界相位求解的迭代轮数
#   warm_start  边界相位求解是否用上一块的 δ 热启动。
#               低/中档相位接近原相位，从 δ=0 起步迭代找"最近解"，
#               避免求解器漂移到远处解破坏波形的接近性；
#               高及以上档位相位完全随机，用热启动加速收敛。
_REPLACE_PARAMS = {
    "jitter": (0.0, 0.6, 1.8, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    "uniform": (False, False, False, True, True, True, True, True, True),
    "dither": (False, False, False, False, True, False, True, True, True),
    "env_db": (0.0, 0.8, 1.2, 1.6, 2.0, 2.0, 2.5, 3.0, 3.0),
    "resample": (1.0, 1.0, 1.0, 1.0, 1.0, 1.02, 1.04, 1.12, 1.16),
    "chorus_mix": (0.0, 0.0, 0.0, 0.0, 0.0, 0.30, 0.45, 0.50, 0.60),
    "wow_depth": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.010, 0.015),
    "wow_rate": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, 0.25),
    "bandpass": (
        (0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0),
        (0.0, 0.0), (0.0, 0.0), (200.0, 6000.0), (300.0, 5000.0),
    ),
    "echo_gain": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.25),
    "vocal_echo": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.35, 0.45),
    "vocal_emph": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.8, 1.0),
    "solve_iters": (0, 8, 12, 24, 24, 24, 24, 24, 24),
    "warm_start": (False, False, False, True, True, True, True, True, True),
}

# 轻合唱参数：基延迟 24 ms、调制深度 5 ms、调制速率 0.5 Hz
_CHORUS_DELAY_MS = 24.0
_CHORUS_DEPTH_MS = 5.0
_CHORUS_RATE_HZ = 0.5
# 回声参数：单次回声 90 ms
_ECHO_DELAY_MS = 90.0


def _mono(data: np.ndarray) -> np.ndarray:
    if data.ndim == 1:
        return data
    return np.mean(data, axis=1)


def _band_series(data: np.ndarray, sr: int) -> np.ndarray:
    """(n_frames, n_bands) 对数频段能量序列。

    矩形分块分析，与替换管道的分块完全同构（纯相位替换时幅度严格
    不变）。低频段至少 4 个频点宽 + 频率轴 3 点三角平滑 + 时间轴 3 块
    三角平滑，对整体变速变调、微小频率平移与跨块瞬态保持稳定
    （约 70 ms / 12~25 音分，人耳无感）。
    """
    mono = _mono(data)
    max_samples = sr * 30
    if len(mono) > max_samples:
        mono = mono[:max_samples]
    if len(mono) < _BLOCK_N:
        return np.zeros((0, 24), dtype=np.float64)

    n_blocks = len(mono) // _BLOCK_N
    if n_blocks < 8:
        return np.zeros((0, 24), dtype=np.float64)

    blocks = mono[: n_blocks * _BLOCK_N].reshape(n_blocks, _BLOCK_N)
    mag = np.abs(np.fft.rfft(blocks, axis=1)).astype(np.float64)

    # 频率轴 3 点三角平滑
    sm = np.empty_like(mag)
    sm[:, 0] = mag[:, 0]
    sm[:, -1] = mag[:, -1]
    sm[:, 1:-1] = 0.25 * mag[:, :-2] + 0.5 * mag[:, 1:-1] + 0.25 * mag[:, 2:]
    bins = sm.shape[1]
    freqs = np.geomspace(1, bins - 1, 25).astype(int)
    # 低频段保证至少 4 个频点宽：窄带泄漏摆动在频段内平均掉，
    # 对整体变速变调（对抗档）保持稳定
    merged = [freqs[0]]
    for f in freqs[1:]:
        if f - merged[-1] < 4:
            continue
        merged.append(f)
    if merged[-1] != bins - 1:
        merged.append(bins - 1)
    n_bands = len(merged) - 1
    bands = np.empty((sm.shape[0], n_bands), dtype=np.float64)
    for i in range(n_bands):
        bands[:, i] = sm[:, merged[i]:merged[i + 1]].sum(axis=1)
    bands = np.log1p(bands)
    # 时间轴 3 块三角平滑：把跨块边界劈裂的瞬态并回单峰
    sm_t = np.empty_like(bands)
    sm_t[0] = bands[0]
    sm_t[-1] = bands[-1]
    sm_t[1:-1] = 0.25 * bands[:-2] + 0.5 * bands[1:-1] + 0.25 * bands[2:]
    return sm_t


def fingerprint(path: str | Path) -> bytes:
    data, sr = _read_audio(path)
    bands = _band_series(data, sr)
    if bands.shape[0] == 0:
        return b""
    thresholds = np.median(bands, axis=0)
    bits = (bands > thresholds).astype(np.uint8)
    return np.packbits(bits.flatten()).tobytes()


def similarity(path_a: str | Path, path_b: str | Path) -> float:
    """返回 0-100 的听感保真度：各频段能量时间序列的加权相关均值。

    在 5 秒窗内做 ±12 块的局部滞后搜索，容忍整体变速带来的缓慢时间
    漂移；权重取两文件该频段能量的几何平均，静音/底噪频段几乎不参与
    评分。
    """
    data_a, sr_a = _read_audio(path_a)
    data_b, sr_b = _read_audio(path_b)
    if sr_a != sr_b:
        return 0.0
    bands_a = _band_series(data_a, sr_a)
    bands_b = _band_series(data_b, sr_b)
    n = min(bands_a.shape[0], bands_b.shape[0])
    if n < 16:
        return 0.0
    bands_a, bands_b = bands_a[:n], bands_b[:n]

    win = max(16, int(sr_a * 5 / _BLOCK_N))  # 5 秒窗
    step = max(8, win // 2)
    lag_max = 12

    total = 0.0
    weight = 0.0
    for start in range(0, max(1, n - win), step):
        end = start + win
        if end > n:
            start, end = n - win, n
        for i in range(bands_a.shape[1]):
            x = bands_a[start:end, i]
            x = x - x.mean()
            norm_x = float(np.linalg.norm(x))
            w = float(np.sqrt(np.std(bands_a[start:end, i]) * np.std(bands_b[start:end, i])))
            if norm_x < 1e-12:
                continue
            best = 0.0
            for lag in range(-lag_max, lag_max + 1):
                lo, hi = start + lag, end + lag
                if lo < 0 or hi > n:
                    continue
                y = bands_b[lo:hi, i]
                y = y - y.mean()
                norm_y = float(np.linalg.norm(y))
                if norm_y < 1e-12:
                    continue
                r = float(np.dot(x, y) / (norm_x * norm_y))
                if r > best:
                    best = r
            total += w * best
            weight += w
    if weight <= 0:
        return 0.0
    return round(total / weight * 100, 1)


def slice_audio(
    src: str | Path, out_dir: str | Path, max_seconds: int
) -> list[Path]:
    """把音频按最大时长切成多段 WAV，返回切片路径列表。

    用于超长音频逐段上传。若音频本身不超过 max_seconds，则返回空列表
    （表示无需切片）。
    """
    src = Path(src)
    out_dir = Path(out_dir)
    info = sf.info(str(src))
    sr = info.samplerate
    frames_per = int(max_seconds * sr)
    if info.frames <= frames_per:
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = src.stem
    parts: list[Path] = []
    with sf.SoundFile(str(src)) as fh:
        pos = 0
        idx = 1
        while pos < info.frames:
            end = min(pos + frames_per, info.frames)
            fh.seek(pos)
            data = fh.read(end - pos, dtype="float32", always_2d=True)
            out = out_dir / f"{stem}_part{idx:03d}.wav"
            sf.write(str(out), data, sr, subtype="PCM_16")
            parts.append(out)
            pos = end
            idx += 1
    return parts


def process(src: str | Path, dst: str | Path, strength: int) -> Path:
    """按强度做同音替换，写出 WAV，返回目标路径。

    强度含义：0=原样写出；1-4 依次加大相位替换力度与幅度谱微扰深度；
    5-6（对抗/对抗+）叠加整体变调变速（+2%/+4%）与轻合唱；7-8
    （强对抗/强对抗+）叠加大幅变调变速（+12%/+16%）、wow 颤振、
    低保真带通与回声，听感明显处理过但仍是同一首歌。变速档位时长
    相应缩短。
    """
    src = Path(src)
    dst = Path(dst)
    data, sr = _read_audio(src)
    if strength <= 0:
        sf.write(str(dst), data, sr, subtype="PCM_16")
        return dst

    rng = np.random.default_rng()
    idx = min(strength, 8)
    ratio = _REPLACE_PARAMS["resample"][idx]
    chorus_mix = _REPLACE_PARAMS["chorus_mix"][idx]
    wow_depth = _REPLACE_PARAMS["wow_depth"][idx]
    wow_rate = _REPLACE_PARAMS["wow_rate"][idx]
    echo_gain = _REPLACE_PARAMS["echo_gain"][idx]
    vocal_echo = _REPLACE_PARAMS["vocal_echo"][idx]
    if ratio != 1.0 or wow_depth > 0.0:
        data = _resample(data, sr, ratio, wow_depth, wow_rate, rng)
    if chorus_mix > 0.0:
        data = _chorus(data, sr, chorus_mix, rng)
    if vocal_echo > 0.0:
        data = _vocal_echo(data, sr, vocal_echo)

    transformed = _same_sound_replace(data, sr, strength, rng)

    if echo_gain > 0.0:
        transformed = _echo(transformed, sr, echo_gain)

    peak = float(np.max(np.abs(transformed)) or 1.0)
    if peak > 0.99:
        transformed *= 0.99 / peak
    sf.write(str(dst), transformed, sr, subtype="PCM_16")
    return dst


def _resample(
    data: np.ndarray,
    sr: int,
    ratio: float,
    wow_depth: float = 0.0,
    wow_rate: float = 0.0,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """整体变速变调 + 可选 wow/flutter 颤振：按（时变）重采样率抽取内容。

    ratio > 1 加快升高（时长 ×1/ratio）；wow_depth 给重采样率叠加慢速
    正弦调制，破坏精细频率对齐（模拟磁带颤振）。重采样会重排全部
    时频对齐关系（谱峰频率、节拍位置、瞬态边界），是对抗鲁棒指纹
    系统的经典手段。
    """
    if ratio == 1.0 and wow_depth <= 0.0:
        return data
    n = data.shape[0]
    new_n = int(n / ratio)
    base = np.arange(n, dtype=np.float64)
    if wow_depth > 0.0:
        t = np.arange(new_n, dtype=np.float64) / sr
        phase = float(rng.uniform(0.0, 2.0 * np.pi)) if rng is not None else 0.0
        rate = ratio * (1.0 + wow_depth * np.sin(2.0 * np.pi * wow_rate * t + phase))
        idx = np.cumsum(rate)
    else:
        idx = np.arange(new_n, dtype=np.float64) * ratio
    idx = np.clip(idx, 0.0, float(n - 1))
    out = np.stack(
        [
            np.interp(idx, base, data[:, c].astype(np.float64))
            for c in range(data.shape[1])
        ],
        axis=1,
    )
    return out.astype(np.float32)


def _echo(data: np.ndarray, sr: int, gain: float) -> np.ndarray:
    """单次回声：时域涂抹，破坏节拍级的时间对齐。"""
    delay = int(_ECHO_DELAY_MS * sr / 1000.0)
    out = data.astype(np.float32, copy=True)
    out[delay:] += (gain * data[:-delay]).astype(np.float32)
    return out


_VOCAL_LO_HZ = 250.0
_VOCAL_HI_HZ = 3500.0


def _vocal_echo(data: np.ndarray, sr: int, gain: float) -> np.ndarray:
    """人声带（250~3500 Hz）双回声：涂抹共振峰时序，破坏歌词 ASR 识别。

    只把带通后的人声带副本按 60/120 ms 延迟混回原信号，带外内容不变；
    共振峰被时间涂抹后，语音识别的谱特征（MFCC 等）明显退化，而人耳
    听到的只是人声带上的 slapback 处理感。
    """
    n = data.shape[0]
    spec = np.fft.rfft(data, axis=0)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    mask = 1.0 / (
        (1.0 + np.exp((_VOCAL_LO_HZ - freqs) / (_VOCAL_LO_HZ * 0.25)))
        * (1.0 + np.exp((freqs - _VOCAL_HI_HZ) / (_VOCAL_HI_HZ * 0.25)))
    )
    band = np.fft.irfft(spec * mask[:, None], n=n, axis=0).astype(np.float32)
    d1 = int(0.060 * sr)
    d2 = int(0.120 * sr)
    out = data.astype(np.float32, copy=True)
    out[d1:] += (gain * 0.5 * band[:-d1]).astype(np.float32)
    out[d2:] += (gain * 0.35 * band[:-d2]).astype(np.float32)
    return out


def _chorus(
    data: np.ndarray, sr: int, mix: float, rng: np.random.Generator
) -> np.ndarray:
    """轻合唱：缓慢调制延迟的副本按 mix 比例混入原声。

    调制延迟产生梳状滤波（谱峰分裂/位移），破坏谱峰级指纹的峰位置
    与峰选择；30%~45% 的混合比下听感是轻微的"加宽"效果。
    """
    n = data.shape[0]
    t = np.arange(n, dtype=np.float64) / sr
    delay = _CHORUS_DELAY_MS / 1000.0 + (_CHORUS_DEPTH_MS / 1000.0) * np.sin(
        2.0 * np.pi * _CHORUS_RATE_HZ * t + float(rng.uniform(0.0, 2.0 * np.pi))
    )
    idx = np.clip(np.arange(n, dtype=np.float64) - delay * sr, 0.0, float(n - 1))
    base = np.arange(n, dtype=np.float64)
    out = np.stack(
        [
            (1.0 - mix) * data[:, c]
            + mix * np.interp(idx, base, data[:, c].astype(np.float64))
            for c in range(data.shape[1])
        ],
        axis=1,
    )
    return out.astype(np.float32)


def _bandpass_mask(sr: int, lo_hz: float, hi_hz: float) -> np.ndarray:
    """低保真带通掩码（S 形软滚降，约 12 dB/倍频程）。

    削掉带外谱峰，整体改变频谱结构；lo/hi 为 0 表示该侧不做滚降。
    """
    if lo_hz <= 0.0 and hi_hz <= 0.0:
        return np.ones(_BLOCK_BINS, dtype=np.float32)
    f = np.arange(_BLOCK_BINS, dtype=np.float64) * sr / _BLOCK_N
    mask = np.ones(_BLOCK_BINS, dtype=np.float64)
    if lo_hz > 0.0:
        mask /= 1.0 + np.exp((lo_hz - f) / (lo_hz * 0.25))
    if hi_hz > 0.0:
        mask /= 1.0 + np.exp((f - hi_hz) / (hi_hz * 0.25))
    return mask.astype(np.float32)


def _envelope_multipliers(
    n_blocks: int,
    depth_db: float,
    rng: np.random.Generator,
    sr: int = 0,
    vocal_emph: float = 0.0,
) -> np.ndarray:
    """对数频段平滑随机幅度包络（dB→乘数），慢/快两层随机游走。

    慢层（7 个控制点、每 8 块更新一次）制造宽带音色微摆，快层（15 个
    控制点、逐块更新）打乱相邻频点的相对大小——两者都会改变指纹系统
    的谱峰选择，而听感上只是极轻微的均衡器漂移。vocal_emph > 0 时
    快层在人声带（250~3500 Hz）额外加深，逐块打乱共振峰结构，
    破坏歌词 ASR 特征。返回 (n_blocks, bins) 的乘数矩阵，与声道无关
    （各声道共享）。
    """
    if depth_db <= 0:
        return np.ones((n_blocks, _BLOCK_BINS), dtype=np.float32)
    nbins = _BLOCK_BINS
    # 只在 bin 1..nbins-1 上做插值（跳过 DC）
    log_axis = np.log(np.arange(1, nbins, dtype=np.float64))

    def layer(n_ctrl: int, update_every: int) -> np.ndarray:
        pos = np.unique(np.geomspace(1.0, nbins - 1.0, n_ctrl).astype(int))
        n_t = max(1, int(np.ceil(n_blocks / update_every)))
        walk = np.cumsum(rng.normal(0.0, 1.0, (n_t, len(pos))), axis=0)
        walk -= walk.mean(axis=0, keepdims=True)
        scale = np.maximum(np.max(np.abs(walk), axis=0, keepdims=True), 1e-9)
        walk = walk / scale  # 每列归一化到 [-1, 1]
        t_idx = np.minimum(
            np.arange(n_blocks) // update_every, n_t - 1
        ).astype(np.int64)
        ctrl = walk[t_idx]  # (n_blocks, n_ctrl)
        log_pos = np.log(pos.astype(np.float64))
        # 频率轴线性插值（log 坐标），输出覆盖 bin 1..nbins-1
        idx = np.searchsorted(log_pos, log_axis).clip(1, len(pos) - 1)
        lo = log_pos[idx - 1]
        hi = log_pos[idx]
        frac = np.clip((log_axis - lo) / np.maximum(hi - lo, 1e-12), 0.0, 1.0)
        return (
            ctrl[:, idx - 1] * (1.0 - frac[None, :])
            + ctrl[:, idx] * frac[None, :]
        )  # (n_blocks, nbins-1)

    slow = layer(7, 8)
    fast = layer(15, 1)
    # 慢层为主、快层为辅，峰值幅度 ≈ depth_db
    env_db = depth_db * (0.6 * slow + 0.4 * fast)
    if vocal_emph > 0.0 and sr > 0:
        # 人声带（250~3500 Hz）梯形强调：快层在此额外加深
        f_hz = np.arange(1, nbins, dtype=np.float64) * sr / _BLOCK_N
        profile = np.clip(
            np.minimum(
                (f_hz - _VOCAL_LO_HZ) / 500.0,
                (_VOCAL_HI_HZ - f_hz) / 500.0,
            ),
            0.0,
            1.0,
        )
        env_db += (depth_db * vocal_emph) * fast * profile[None, :]
    env = np.ones((n_blocks, nbins), dtype=np.float32)
    env[:, 1:] = np.power(10.0, env_db / 20.0).astype(np.float32)
    return env


def _same_sound_replace(
    data: np.ndarray, sr: int, strength: int, rng: np.random.Generator
) -> np.ndarray:
    """同音替换：矩形分块频域旋转相位 + 幅度谱微扰（+带通），块边界连续。

    流程：
      1. 末尾补零到整块，逐块 FFT 得到频谱；
      2. 按强度把相位旋转（低/中=原相位+高斯扰动，高及以上=均匀随机），
         叠加对数频段平滑随机包络（±0.8~3.0 dB，慢层攻击谱峰选择、
         快层等效谱峰微移）、强对抗档的低保真带通与 ±1% 幅度微扰；
         DC/Nyquist 实系数随机翻转符号；
      3. 每块用最小范数求全部频点的相位修正，使本块起点取值与斜率
         衔接上一块终点（消除块边界咔哒声）；
      4. 逐块 IFFT 拼回原长度。

    旋转角度、包络与修正量在左右声道间共享，因此单声道（左右平均）
    的幅度谱严格跟随同一包络，指纹自测能如实反映保真程度。
    """
    jitter = _REPLACE_PARAMS["jitter"][min(strength, 8)]
    uniform = _REPLACE_PARAMS["uniform"][min(strength, 8)]
    dither = _REPLACE_PARAMS["dither"][min(strength, 8)]
    env_db = _REPLACE_PARAMS["env_db"][min(strength, 8)]
    vocal_emph = _REPLACE_PARAMS["vocal_emph"][min(strength, 8)]
    solve_iters = _REPLACE_PARAMS["solve_iters"][min(strength, 8)]
    warm_start = _REPLACE_PARAMS["warm_start"][min(strength, 8)]
    bp_lo, bp_hi = _REPLACE_PARAMS["bandpass"][min(strength, 8)]
    bp_mask = _bandpass_mask(sr, bp_lo, bp_hi)

    data = data.astype(np.float32, copy=False)
    n, n_ch = data.shape
    n_blocks = int(np.ceil(n / _BLOCK_N))
    total = n_blocks * _BLOCK_N
    padded = np.pad(data, ((0, total - n), (0, 0)))

    # 旋转角度/幅度微扰/实系数翻转/包络在声道间共享
    if uniform:
        theta = rng.uniform(0.0, 2.0 * np.pi, (n_blocks, _BLOCK_BINS))
        theta = theta.astype(np.float32)
    else:
        theta = rng.normal(0.0, jitter, (n_blocks, _BLOCK_BINS)).astype(np.float32)
    if dither:
        gain = rng.uniform(0.99, 1.01, (n_blocks, _BLOCK_BINS)).astype(np.float32)
    else:
        gain = np.ones((n_blocks, _BLOCK_BINS), dtype=np.float32)
    flip = (rng.integers(0, 2, (n_blocks, 2)) * 2 - 1).astype(np.float32)
    env = _envelope_multipliers(n_blocks, env_db, rng, sr, vocal_emph)

    # 斜率约束用到的频点旋转因子
    e1 = np.exp(2j * np.pi * np.arange(_BLOCK_BINS) / _BLOCK_N).astype(np.complex64)

    # 逐块处理（保持低内存：只保留上一块的时域波形）
    out = np.empty_like(padded)
    blocks_view = padded.reshape(n_blocks, _BLOCK_N, n_ch)
    y_prev: np.ndarray | None = None
    delta_prev: np.ndarray | None = None
    for k in range(n_blocks):
        spec = np.fft.rfft(blocks_view[k].T, axis=1)  # (n_ch, bins)
        S2 = spec * (gain[k] * env[k] * bp_mask) * np.exp(1j * theta[k])
        S2[:, 0] = spec[:, 0] * flip[k, 0]
        S2[:, -1] = spec[:, -1] * flip[k, 1]
        if k > 0:
            # 逐块衔接边界：本块起点值/斜率 ← 上一块终点值/斜率
            # 相位修正 δ 在声道间共享，保证单声道幅度谱严格不变
            targets = np.stack(
                [y_prev[:, -1], y_prev[:, -1] - y_prev[:, -2]], axis=1
            )
            S2, delta_prev = _solve_boundary_phase(
                S2, targets, e1, delta_prev, solve_iters, warm_start
            )
        y_prev = np.fft.irfft(S2, n=_BLOCK_N, axis=1)  # (n_ch, _BLOCK_N)
        out[k * _BLOCK_N:(k + 1) * _BLOCK_N] = y_prev.T

    return out[:n]


def _solve_boundary_phase(
    specs: np.ndarray,
    targets: np.ndarray,
    e1: np.ndarray,
    delta_prev: np.ndarray | None,
    max_iters: int,
    warm_start: bool,
) -> tuple[np.ndarray, np.ndarray]:
    """求声道间共享的相位修正 δ，使每个声道该块的起点值/斜率衔接上一块终点。

    specs: (n_ch, bins) 本块各声道频谱；targets: (n_ch, 2) 各声道的
    (起点值目标, 斜率目标)。只改变相位、各频点幅度保持不变，δ 共享使
    单声道（左右平均）幅度谱严格不变。用 2·n_ch 个约束对 510 个未知
    相位做最小范数高斯-牛顿迭代（带阻尼线搜索）。warm_start=True 时
    用上一块的 δ 热启动（相位随机档位适用）；否则从 δ=0 起步，找离
    当前相位最近的解（相位接近原相位的档位适用）。目标不可行时（如
    相邻块能量分布差异过大）残差留在自然信号波动量级内，不产生可闻
    咔哒声。
    """
    n_ch = specs.shape[0]
    cand = np.arange(1, specs.shape[1] - 1)
    f = cand.astype(np.float64)
    w = 2.0 * np.pi * f / _BLOCK_N

    mag = np.abs(specs[:, cand]).astype(np.float64)
    th = np.angle(specs[:, cand]).astype(np.float64)
    rest0 = np.empty(n_ch, dtype=np.float64)
    rest1 = np.empty(n_ch, dtype=np.float64)
    for c in range(n_ch):
        s = specs[c]
        # irfft 自带 1/N 归一化：约束目标换算到"未归一化求和"域。
        # 复频点 f 与 N-f 互为共轭，对时域值的贡献是 2·Re(S_f)；
        # DC/Nyquist 为实系数，单独计一次。
        full0 = 2.0 * float(s.sum().real) - float(s[0].real + s[-1].real)
        rest0[c] = full0 - 2.0 * float(s[cand].sum().real)
        full1 = (
            2.0 * float((s * (e1 - 1.0)).sum().real) + 2.0 * float(s[-1].real)
        )
        rest1[c] = full1 - 2.0 * float((s[cand] * (e1[cand] - 1.0)).sum().real)
    t0 = targets[:, 0].astype(np.float64) * _BLOCK_N
    t1 = targets[:, 1].astype(np.float64) * _BLOCK_N

    def residual(delta: np.ndarray) -> np.ndarray:
        cur_th = th + delta[None, :]
        cur0 = np.sum(2.0 * mag * np.cos(cur_th), axis=1)
        cur1 = np.sum(2.0 * mag * (np.cos(w + cur_th) - np.cos(cur_th)), axis=1)
        return np.concatenate([rest0 + cur0 - t0, rest1 + cur1 - t1])

    delta = (
        delta_prev.copy()
        if (warm_start and delta_prev is not None)
        else np.zeros(cand.size, dtype=np.float64)
    )
    for _ in range(max_iters):
        res = residual(delta)
        if float(np.max(np.abs(res))) < 1e-9:
            break
        cur_th = th + delta[None, :]
        jac = np.concatenate(
            [
                -2.0 * mag * np.sin(cur_th),
                -2.0 * mag * (np.sin(w + cur_th) - np.sin(cur_th)),
            ],
            axis=0,
        )
        d = np.linalg.lstsq(jac, res, rcond=None)[0]
        # 阻尼线搜索：残差变大则逐步减半步长
        step = 1.0
        norm_old = float(np.linalg.norm(res))
        for _ in range(4):
            trial = delta - step * d
            if float(np.linalg.norm(residual(trial))) <= norm_old:
                break
            step *= 0.5
        delta = delta - step * d

    specs[:, cand] = (mag * np.exp(1j * (th + delta[None, :]))).astype(np.complex64)
    return specs, delta
