"""本地音频混淆：同音替换（重叠分帧相位重排）+ 有损 MP3 重编码输出。

核心是"同音替换"：把音频按重叠 50% 的汉宁窗分帧（帧长 1024~8192、
帧移为帧长一半），每帧频谱乘上均匀随机相位（各频点独立、左右声道
共享同一旋转角），再重叠相加（COLA 归一化）还原波形。听感与原音频
高度相似（人耳对相位不敏感、长期平均频谱严格保持），但波形相关与
逐帧幅度谱峰结构被彻底改写。

与旧版"临界采样矩形分块相位替换"的关键区别：旧版每块幅度谱与原文件
严格一致，Audible Magic 这类幅度谱峰级指纹对相位透明、仍能命中
（实测王以太hiphop 全档拦截）；新版重叠帧在相邻帧交界处发生随机
干涉，逐帧幅度谱相对原文件产生 ±8~15 dB 的随机波动（帧间去相关、
约 46 ms 半衰期），谱峰位置/选择被逐帧打乱，而 200 ms 以上时间窗
的平均频谱几乎不变。实测参照样本（musics/ 下两对"转换"文件，可
成功上传）正是这一结构：波形相关 ≈ 0（100 ms 窗标准差约 0.3~0.4）、
逐帧幅度比标准差约 8~20 dB、全曲对数频谱相关 0.98+、相位差均匀
随机（一致性约 0.04~0.05）、无变速变调（对数频谱相关峰值在 0 音分）。

低/中/高/实验档分别用 1024/2048/4096/8192 帧长（帧移一半），帧长
越大逐帧幅度打散越强（对幅度谱指纹越有效）、瞬态涂抹越宽（仍远低于
可闻阈值，参照样本听感与原音频基本无区别）。

Suno 上传有两道检测：声纹检测（Audible Magic，幅度谱峰级指纹，
被 OLA 逐帧干涉击穿）与内容检测（ACRCloud，峰值对 (f1,f2,Δt)
哈希，对相位/幅度微扰与 ±2.44 半音以内的变速变调鲁棒——实测
+15%/+2.44 半音仍被拦截、+17%/+2.87 半音通过）。针对内容检测
新增的档位：
- 参照复刻：完整复刻 musics/ 下可成功上传样本的处理链——首 1/6
  时长直通、其余 OLA 相位重排、峰值归一化、192kbps CBR MP3 重
  编码（需 lameenc 或系统 ffmpeg；libsndfile VBR 兜底），听感与
  原音频基本一致，是当前"听感不变"方向最接近样本的档位；
- 内容打散/内容打散+：OLA 之上叠加分段频带随机群延迟（1 秒段、
  ±60/±120 ms、16 个对数频带独立随机游走、段间交叉淡化），各频带
  谱峰相对其他频带获得随机时间偏移，ACRCloud 峰值对的 Δt 无法
  对齐；每段全通、听感基本不变；
- 音调偏移/音调偏移+：保时长升调 +2.9 半音（SOLA 拉伸还原时长，
  只升调不加速），与已过检的内容对抗+同幅度；
- 内容对抗/内容对抗+：干净整体变速 +15%/+17%，听感为"升调加速"，
  内容对抗+已实测上传成功。

对抗/强对抗档继续叠加变调变速、合唱、颤振、带通与回声等经典对抗
手段（见 _REPLACE_PARAMS 注释），作为连续失手时的兜底。

不依赖 librosa/scipy，只使用 numpy + soundfile，避免打包体积过大。
混淆后的指纹相似度用于自测，不是版权检测工具。
"""

from pathlib import Path
import os
import shutil
import subprocess

import numpy as np
import soundfile as sf

# 分块长度（= 帧移，临界采样：每块 1024 个采样、513 个独立频点）
_BLOCK_N = 1024
_BLOCK_BINS = _BLOCK_N // 2 + 1


# 各档位的界面说明（上传页下拉框下方实时显示）
STRENGTH_DESC = {
    "关闭": "不做任何处理，原样上传（WAV 直通）。",
    "低": "同音替换·轻度：重叠帧相位重排（帧 1024），听感不变，仅打散声纹指纹。",
    "中": "同音替换·标准：重叠帧相位重排（帧 2048），听感不变，逐帧谱峰打散更强。",
    "高": "同音替换·深度：重叠帧相位重排（帧 4096），听感不变，谱峰打散更强。",
    "实验": "同音替换·极限：重叠帧相位重排（帧 8192），听感基本不变。",
    "参照复刻": "完整复刻参照样本的处理链：-12 LUFS 归一化 + 首 1/6 直通 + "
        "OLA 相位重排 + 192kbps CBR MP3。听感与原音频基本一致，"
        "是『听感不变』方向最接近可上传样本的档位。",
    "内容打散": "听感不变 + 频带随机群延迟（±60ms）：打乱 ACRCloud 峰值对的"
        "时间偏移，力度较轻。",
    "内容打散+": "听感基本不变 + 频带随机群延迟（±120ms）：时间偏移打散更强，"
        "瞬态略有涂抹。",
    "音调偏移2.6": "保时长升调 +2.6 半音（只变调不变速）：边界二分的最小变调档，"
        "若过检则听感冲击最小。",
    "音调偏移": "保时长升调 +2.9 半音（只变调不变速）：已实测过内容检测，"
        "节奏与时长不变、整曲升调。",
    "降调偏移": "保时长降调 -2.9 半音（只变调不变速）：与升调同幅度，"
        "部分歌曲降调更自然。",
    "音调偏移+": "保时长升调 +2.9 半音 + 频带群延迟：双重打乱，升调听感同上。",
    "内容对抗": "干净整体变速 +15%（约 +2.44 半音，升调加速）：实测仍会被拦，"
        "仅作强度递进中间档。",
    "内容对抗+": "干净整体变速 +17%（约 +2.87 半音，升调加速）+ 群延迟："
        "已实测过内容检测，听感为加速升调。",
    "对抗": "变速 +2% + 轻合唱：经典对抗手段，听感轻微变化。",
    "对抗+": "变速 +4% + 合唱：听感进一步变化。",
    "强对抗": "变速 +12% + 合唱 + 带通 + 人声带处理：听感明显处理过，"
        "历史实测可通过多道检测。",
    "强对抗+": "变速 +16% + 合唱 + 带通 + 人声带处理 + 回声：最强破坏档。",
}


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
    "参照复刻": 15,
    "内容打散": 9,
    "内容打散+": 10,
    "音调偏移2.6": 16,
    "音调偏移": 11,
    "降调偏移": 17,
    "音调偏移+": 12,
    "内容对抗": 13,
    "内容对抗+": 14,
    "对抗": 5,
    "对抗+": 6,
    "强对抗": 7,
    "强对抗+": 8,
}

# 同音替换档位（低/中/高/实验）的重叠帧长与帧移：
# 帧长越大，相邻帧干涉造成的逐帧幅度谱打散越强（对抗幅度谱峰指纹），
# 瞬态涂抹越宽但仍不可闻。对抗及以上档位沿用下方 _REPLACE_PARAMS。
_OLA_WINS = {
    1: (1024, 512),    # 低：轻度打散
    2: (2048, 1024),   # 中：参照 musics/ 样本结构（推荐）
    3: (4096, 2048),   # 高：深度打散
    4: (8192, 4096),   # 实验：极限打散
}

# 对抗及以上档位（5-8）的同音替换参数（低~实验档走 _OLA_WINS，
# 不再使用本表 1-4 列）：
#   jitter      原相位上叠加的高斯扰动幅度（弧度，对抗档位不使用）
#   uniform     是否把相位完全替换为均匀随机角度（对抗及以上全为 True）
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
#   warm_start  边界相位求解是否用上一块的 δ 热启动（对抗及以上
#               档位相位完全随机，热启动加速收敛）
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

# 内容对抗档位的干净变速比例：+15%/+17% 对应 +2.44/+2.87 半音。
# 实测 +15% 仍被 ACRCloud 拦截、+17% 通过：ACRCloud 的变速变调
# 容差边界在 +2.44~+2.87 半音之间。只做整体变速、不做合唱/带通/
# 回声，听感为干净的"升调加速"。
_CONTENT_RATIO = {13: 1.15, 14: 1.17}

# 音调偏移档位的保时长升调半音数：与已过检的"内容对抗+"（+2.87 半音）
# 取同一升调幅度，但用 SOLA 时间拉伸把时长/节奏还原——只升调不加速，
# 听感冲击比变速变调小（节奏与时长完全不变）。
_PITCH_ONLY_SEMITONES = 2.9

# 参照复刻档：musics/ 下两对可成功上传的"(转换)"样本的完整处理链
# 逆向结论——① 首 1/6 时长直通（实测两样本边界 = 全曲/6，精确到
# 0.1s）；② 其余 5/6 做 OLA 相位重排（帧 2048/1024）；③ 峰值
# 归一化；④ 有损 MP3 重编码（样本为 192kbps CBR；libsndfile 的
# lame 无法指定码率，默认 VBR 约 110~190kbps，同类编码噪声）。
# 样本另有 -14 LUFS 响度归一化（进阶 ×1.0、不习惯 ×1.23），检测
# 与响度无关，未复刻。
_REF_PREFIX_FRAC = 1.0 / 6.0

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

    矩形分块分析。低频段至少 4 个频点宽 + 频率轴 3 点三角平滑 +
    时间轴 9 块平滑，对重叠帧相位重排的逐帧幅度干涉（±8~15 dB、
    约 46 ms 去相关）、整体变速变调与跨块瞬态保持稳定（约 210 ms /
    12~25 音分，人耳无感）。
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
    # 时间轴 9 块（约 209 ms）平滑：重叠帧相位重排会在逐帧幅度谱上
    # 产生 ±8~15 dB 的随机干涉（帧间约 46 ms 去相关），9 块窗口平均
    # 后长期频谱几乎不变（实测参照样本与原音频对数频谱相关 0.98+），
    # 同时仍能容忍整体变速带来的缓慢时间漂移与跨块瞬态。
    kernel = np.ones(9, dtype=np.float64) / 9.0
    sm_t = np.empty_like(bands)
    for i in range(bands.shape[1]):
        sm_t[:, i] = np.convolve(bands[:, i], kernel, mode="same")
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
    """按强度做同音替换，写出 MP3（192kbps CBR），返回目标路径。

    强度 0（关闭）=原样写出 WAV；其余档位统一输出 MP3 有损重编码
    （编码器：lameenc → 系统 ffmpeg → libsndfile VBR 兜底），与
    musics/ 参照样本的输出结构一致。

    强度含义：0=原样写出；1-4（低/中/高/实验）为重叠分帧相位重排
    （帧长 1024/2048/4096/8192、帧移一半），逐帧幅度谱被相邻帧随机
    干涉打散、长期平均频谱与听感不变；15（参照复刻）复刻 musics/ 下
    可成功上传样本的完整处理链——-12 LUFS 归一化、首 1/6 时长直通、
    其余 OLA 相位重排、尾部独立峰值归一化、192kbps CBR MP3 重编码，
    听感与原音频基本一致；9/10（内容打散/内容打散+）在 OLA 之上
    叠加分段频带随机群延迟（±60/±120 ms），打乱 ACRCloud 峰值对的
    时间偏移、听感基本不变；16/11/17（音调偏移2.6/音调偏移/降调偏移）
    为保时长变调（+2.6/+2.9/-2.9 半音，SOLA 拉伸还原时长与节奏，
    只变调不变速，实测 +2.87 半音过检、+2.44 被拦）；12（音调偏移+）
    为 +2.9 半音再叠加群延迟；13/14（内容对抗/内容对抗+）为干净整体
    变速（+15%/+17%，约 +2.44/+2.87 半音），内容对抗+再叠加群延迟，
    听感为"升调加速"；5-6（对抗/对抗+）叠加整体变调变速（+2%/+4%）
    与轻合唱；7-8（强对抗/强对抗+）叠加大幅变调变速（+12%/+16%）、
    wow 颤振、低保真带通与回声。变速档位时长相应缩短。
    """
    src = Path(src)
    dst = Path(dst)
    data, sr = _read_audio(src)
    if strength <= 0:
        sf.write(str(dst), data, sr, subtype="PCM_16")
        return dst

    rng = np.random.default_rng()
    if 1 <= strength <= 4:
        win, hop = _OLA_WINS[strength]
        transformed = _same_sound_replace_ola(data, sr, win, hop, rng)
    elif strength == 15:
        # 参照复刻：-12 LUFS 归一化 → 首 1/6 直通（保持 LUFS 电平）→
        # 其余 OLA 相位重排 → 尾部独立峰值归一化 → MP3 重编码。
        # 参照样本的增益结构正是如此：进阶整体 RMS 比 0.597、不习惯
        # 0.895，与"LUFS 增益 + 尾部峰值归一化"模型完全吻合。
        lu_gain = _lufs_gain(data, sr, -12.0)
        data = (data * lu_gain).astype(np.float32)
        t_cut = int(len(data) * _REF_PREFIX_FRAC)
        head = data[:t_cut]
        # 头部防削波：LUFS 增益可能使峰值超 1（不习惯 ×1.17）
        hp = float(np.max(np.abs(head)) or 1.0)
        if hp > 0.99:
            head = head * (0.99 / hp)
        tail = _same_sound_replace_ola(data[t_cut:], sr, 2048, 1024, rng)
        tp = float(np.max(np.abs(tail)) or 1.0)
        if tp > 0.99:
            tail = tail * (0.99 / tp)
        transformed = np.concatenate([head, tail], axis=0)
        dst = dst.with_suffix(".mp3")
        return _write_mp3(transformed, sr, dst, bitrate_kbps=192)
    elif strength in (9, 10):
        transformed = _same_sound_replace_ola(data, sr, 2048, 1024, rng)
        max_ms = 60.0 if strength == 9 else 120.0
        transformed = _group_delay_scramble(transformed, sr, rng, max_ms=max_ms)
    elif strength in (11, 12):
        transformed = _pitch_shift_only(data, sr, _PITCH_ONLY_SEMITONES)
        if strength == 12:
            transformed = _group_delay_scramble(transformed, sr, rng)
    elif strength == 16:
        # 边界二分：实测 +2.44 半音被拦、+2.87 半音通过，+2.6 是
        # 未测过的中间值，若过检则听感冲击更小
        transformed = _pitch_shift_only(data, sr, 2.6)
    elif strength == 17:
        # 降调变体：部分歌曲降 2.9 半音比升调更自然
        transformed = _pitch_shift_only(data, sr, -2.9)
    elif strength in (13, 14):
        data = _resample(data, sr, _CONTENT_RATIO[strength])
        transformed = _same_sound_replace_ola(data, sr, 2048, 1024, rng)
        if strength == 14:
            transformed = _group_delay_scramble(transformed, sr, rng)
    else:
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
    # 所有处理档位统一输出 MP3（ffmpeg/lameenc 192kbps CBR，与参照样本一致）
    dst = dst.with_suffix(".mp3")
    return _write_mp3(transformed, sr, dst, bitrate_kbps=192)


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


def _same_sound_replace_ola(
    data: np.ndarray,
    sr: int,
    win: int,
    hop: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """重叠分帧相位重排（OLA）：每帧频谱乘均匀随机相位后重叠相加。

    与临界采样矩形分块的 _same_sound_replace 不同，本函数不做块边界
    相位求解——汉宁窗 50% 重叠 + COLA 归一化天然无缝，不需要连续化。
    相邻帧在重叠区随机干涉，使逐帧幅度谱产生 ±8~15 dB 的随机波动
    （打乱 Audible Magic 谱峰），而 200 ms 以上窗口的平均频谱几乎
    不变（听感不变）。旋转角在左右声道间共享，立体声像与单声道平均
    频谱保持稳定。

    全程 float64。注意合成端不能再乘窗：相位随机化后
    IFFT(FFT(x·w)·e^{iθ}) 的时域样本不再等于 x·w（能量在时域被随机
    摊平、窗边缘也有满幅样本），若再乘窗并除以 Σw²，窗边缘的低权重
    会把普通样本放大成数千倍尖峰（实测 6147~134359），进而把整曲
    峰值归一化拉到静音。正确做法是 IFFT 后直接叠加、归一化除以 Σw
    （恒等旋转时 Σ(x·w)/Σw = x 严格成立）。
    """
    n, n_ch = data.shape
    if n < 256:
        win = 256
        hop = 128
    win = int(win)
    hop = max(1, hop)
    nwin = np.hanning(win)
    # 两端反射填充半个窗，处理后裁掉：首尾不完整窗口的 Σw 趋近 0，
    # 相位随机化后的满幅样本除以它会放大成尖峰（实测 3e11），
    # 反射填充保证保留区域内 Σw ≈ 1（汉宁窗 50% 重叠的 COLA）。
    margin = min(win // 2, max(n - 1, 0))
    head_pad = data[margin:0:-1] if margin else np.zeros((0, n_ch), dtype=np.float32)
    tail_pad = (
        data[n - 2:n - 2 - margin:-1] if margin else np.zeros((0, n_ch), dtype=np.float32)
    )
    padded0 = np.concatenate([head_pad, data, tail_pad], axis=0).astype(np.float64)
    n0 = len(padded0)
    n_frames = max(1, int(np.ceil((n0 - win) / hop)) + 1)
    total = (n_frames - 1) * hop + win
    padded = np.pad(padded0, ((0, total - n0), (0, 0)))
    # (n_frames, n_ch, win) 步进视图；按批处理控制内存
    view = np.lib.stride_tricks.sliding_window_view(padded, win, axis=0)[::hop]
    out = np.zeros((total, n_ch), dtype=np.float64)
    norm = np.zeros(total, dtype=np.float64)
    batch = max(1, min(128, 2_097_152 // max(win * n_ch * 16, 1)))
    t_in = np.arange(win, dtype=np.int64)
    for k0 in range(0, n_frames, batch):
        k1 = min(k0 + batch, n_frames)
        frames = view[k0:k1]  # (m, n_ch, win)
        X = np.fft.rfft(frames * nwin[None, None, :], axis=2)
        # 旋转角跨声道共享：(m, bins)
        theta = rng.uniform(0.0, 2.0 * np.pi, (k1 - k0, X.shape[2]))
        Y = X * np.exp(1j * theta)[:, None, :]
        y = np.fft.irfft(Y, n=win, axis=2)  # 不再乘窗
        y = np.moveaxis(y, 1, 2)  # (m, win, n_ch)
        pos = np.arange(k0, k1, dtype=np.int64)[:, None] * hop + t_in[None, :]
        pos = pos.ravel()
        flat = y.reshape(-1, n_ch)
        for c in range(n_ch):
            out[:, c] += np.bincount(pos, weights=flat[:, c], minlength=total)
        norm += np.bincount(
            pos, weights=np.broadcast_to(nwin, (k1 - k0, win)).ravel(), minlength=total
        )
    out /= np.maximum(norm, 1e-12)[:, None]
    return out[margin:margin + n].astype(np.float32)


def _group_delay_scramble(
    data: np.ndarray,
    sr: int,
    rng: np.random.Generator,
    seg_s: float = 1.0,
    max_ms: float = 60.0,
    n_bands: int = 16,
    pad_ms: float = 80.0,
    fade_ms: float = 100.0,
) -> np.ndarray:
    """分段频带随机群延迟：打乱 ACRCloud 峰值对哈希的时间偏移。

    把音频切成 seg_s 秒段（段间 100 ms 交叉淡化），每段先按 ±pad_ms
    填充再做 FFT；16 个对数频带（80~16000 Hz，升余弦半重叠、频点和
    恒为 1）各乘 e^{-2πif·d}，d 为该段该频带的随机延迟（跨段随机
    游走、±max_ms、频带间独立），IFFT 后裁掉填充。结果：每个频带的
    谱峰相对其他频带获得 ±(d1-d2) 的随机时间偏移，ACRCloud/Shazam
    类"峰值对 (f1,f2,Δt)"哈希的 Δt 被打乱而无法对齐；频带内幅度谱
    严格不变（每段全通），听感不变，仅在段交界处有极轻微的相位感。
    填充保证 ±max_ms 平移不产生循环回绕与内容丢失。
    """
    n = data.shape[0]
    n_ch = data.shape[1]
    seg = int(seg_s * sr)
    pad = int(pad_ms * sr / 1000)
    fade = int(fade_ms * sr / 1000)
    hop = seg - fade
    n_segs = int(np.ceil(max(n - seg, 0) / hop)) + 1
    # (n_segs, n_bands) 延迟：每频带独立随机游走，钳制到 ±max_ms
    walk = np.clip(
        np.cumsum(rng.normal(0.0, 1.0, (n_segs, n_bands)), axis=0), -2.5, 2.5
    )
    delays = walk * (max_ms / 1000.0 / 2.5)  # 秒
    n_fft = seg + 2 * pad
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    edges = np.geomspace(80.0, 16000.0, n_bands + 1)
    log_edges = np.log(edges)
    centers_log = log_edges[:-1] + np.diff(log_edges) / 2
    width = float(np.diff(log_edges)[0])
    logf = np.log(np.maximum(freqs, 1.0))
    W = np.zeros((n_bands, len(freqs)), dtype=np.float64)
    for b in range(n_bands):
        p = (logf - centers_log[b]) / width
        m = np.abs(p) <= 1.0
        W[b, m] = 0.5 * (1.0 + np.cos(np.pi * p[m]))  # 频带权重和恒为 1
    ramp_in = np.hanning(2 * fade)[:fade].astype(np.float32)   # 0→1
    ramp_out = np.hanning(2 * fade)[fade:].astype(np.float32)  # 1→0
    out = np.zeros((n, n_ch), dtype=np.float32)
    wgt = np.zeros(n, dtype=np.float32)
    for s in range(n_segs):
        start = s * hop
        chunk = np.zeros((n_fft, n_ch), dtype=np.float64)
        # 左填充 [start-pad, start)、主体 [start, start+seg)、右填充
        if start >= pad:
            chunk[:pad] = data[start - pad:start]
        else:
            chunk[pad - start:pad] = data[0:start]
        m1 = min(start + seg, n)
        chunk[pad:pad + (m1 - start)] = data[start:m1]
        r0, r1 = start + seg, min(start + seg + pad, n)
        if r1 > r0:
            chunk[pad + seg:pad + seg + (r1 - r0)] = data[r0:r1]
        X = np.fft.rfft(chunk, axis=0)
        Xb = X.copy()
        for b in range(n_bands):
            d = delays[s, b]
            if abs(d) > 1e-6:
                Xb += (np.exp(-2j * np.pi * freqs * d) - 1.0)[:, None] * (
                    W[b][:, None] * X
                )
        y = np.fft.irfft(Xb, n=n_fft, axis=0)
        seg_out = y[pad:pad + seg].astype(np.float32)
        L = min(seg, n - start)
        seg_out = seg_out[:L]
        w_local = np.ones(L, dtype=np.float32)
        if s > 0:
            f = min(fade, L)
            w_local[:f] = ramp_in[:f]
        if s < n_segs - 1:
            f = min(fade, L)
            w_local[L - f:] = np.minimum(w_local[L - f:], ramp_out[:f])
        out[start:start + L] += seg_out * w_local[:, None]
        wgt[start:start + L] += w_local
    out /= np.maximum(wgt, 1e-9)[:, None]
    return out[:n]


def _time_stretch_sola(
    data: np.ndarray,
    sr: int,
    factor: float,
    frame: int = 1024,
    search: int = 128,
) -> np.ndarray:
    """SOLA 时间拉伸（保音高）：factor>1 变慢变长、<1 变快变短。

    输出帧移固定 frame//2；分析位置每帧前进 frame//2/factor，并在
    ±search 内做互相关对齐上一帧的自然延续（避免瞬态重复/丢失）。
    单声道混合信号决定偏移，左右声道共享；汉宁窗 50% 重叠 +
    归一化，无咔哒声。用于"音调偏移"档：先变速升调、再拉伸还原
    时长，得到只升调不加速的音频。
    """
    n, n_ch = data.shape
    Hs = frame // 2
    Ha = max(1, int(round(Hs / factor)))
    n_out = int(round(n * factor))
    mono = (
        data.mean(axis=1).astype(np.float64)
        if n_ch > 1
        else data[:, 0].astype(np.float64)
    )
    out = np.zeros((n_out, n_ch), dtype=np.float32)
    norm = np.zeros(n_out, dtype=np.float32)
    win = np.hanning(frame).astype(np.float32)
    ov = Hs  # 相邻输出帧重叠长度
    pos = 0
    prev_pos = 0
    k = 0
    while pos + frame <= n and k * Hs + frame <= n_out:
        if k > 0:
            nominal = min(pos + Ha, n - frame)
            lo = max(0, nominal - search)
            hi = min(n - frame, nominal + search)
            # 上一帧的自然延续：输入 [prev_pos+Hs, prev_pos+Hs+ov)
            ref = mono[prev_pos + Hs:prev_pos + Hs + ov]
            rn = float(np.linalg.norm(ref))
            if rn > 1e-9:
                region = mono[lo:hi + ov]
                # 滑窗互相关（FFT）+ 滑动能量归一
                cc = np.correlate(region, ref, mode="valid").astype(np.float64)
                e2 = np.convolve(region ** 2, np.ones(ov), mode="valid")
                score = cc / np.maximum(np.sqrt(e2) * rn, 1e-12)
                p = int(np.argmax(score))
                pos = lo + p
            else:
                pos = nominal
        prev_pos = pos
        seg = data[pos:pos + frame] * win[:, None]
        o0 = k * Hs
        out[o0:o0 + frame] += seg
        norm[o0:o0 + frame] += win  # Σw 归一化：Σ(x·w)/Σw = x（恒等重建）
        pos += Ha
        k += 1
    out /= np.maximum(norm, 1e-9)[:, None]
    return out[:n_out]


def _pitch_shift_only(
    data: np.ndarray, sr: int, semitones: float
) -> np.ndarray:
    """保时长音高平移：先变速升调（变调+变速），再 SOLA 拉伸还原时长。

    结果：音高整体上移 semitones 个半音、节奏与时长不变。用于"音调
    偏移"档——与已过检的"内容对抗+"取同一升调幅度（+2.9 半音，
    超出 ACRCloud 实测容差边界），但避免变速带来的双重听感变化。
    """
    ratio = float(2 ** (semitones / 12.0))
    sped = _resample(data, sr, ratio)
    return _time_stretch_sola(sped, sr, ratio)


def _lufs_gain(data: np.ndarray, sr: int, target_lufs: float = -12.0) -> float:
    """估算把音频归一化到 target_lufs 的线性增益（ITU-R BS.1770）。

    K 加权用标准双二阶系数（1681.97 Hz 高架 +4 dB、38.13 Hz 二阶
    高通）在频域解析求频响后对整段 FFT 滤波；400 ms 分块、-70 LUFS
    绝对门限 + -10 LU 相对门限（两遍）。目标取 -12 LUFS：参照样本
    实测前缀增益 进阶 ×1.0（本曲 -11.7 LUFS，增益 ≈0.97）、不习惯
    ×1.23（约 -13.8 LUFS，增益 10^(1.8/20)=1.23）与该目标精确吻合；
    ffmpeg ebur128 校准本仪表误差 <1 dB。
    """
    n = data.shape[0]
    mono = data.mean(axis=1).astype(np.float64)
    # BS.1770 标准 K 加权双二阶系数
    b_s = [1.53512485958697, -2.69169618940638, 1.19839281085285]
    a_s = [1.0, -1.69065929318241, 0.73248077421585]
    b_h = [1.0, -2.0, 1.0]
    a_h = [1.0, -1.99004745483398, 0.99007225036621]
    freqs = np.fft.rfftfreq(n, 1 / sr)
    z = np.exp(-2j * np.pi * freqs / sr)
    z2 = z * z

    def h_biquad(b, a):
        num = b[0] + b[1] * z + b[2] * z2
        den = a[0] + a[1] * z + a[2] * z2
        return num / den

    h = h_biquad(b_s, a_s) * h_biquad(b_h, a_h)
    h[0] = 0.0
    zf = np.fft.irfft(np.fft.rfft(mono) * h, n=n)
    block = int(sr * 0.4)
    n_blocks = n // block
    if n_blocks < 1:
        return 1.0
    z2b = zf[: n_blocks * block].reshape(n_blocks, block)
    energy = (z2b ** 2).mean(axis=1)
    # -70 LUFS 绝对门限（相对满幅方波 0 LUFS）
    active = energy[energy > 1e-7]
    if len(active) == 0:
        return 1.0
    # -10 LU 相对门限：去掉比均值低 10 LU 的块后重算
    thr = float(active.mean() * 10 ** (-10.0 / 10.0))
    active2 = active[active > thr]
    if len(active2) == 0:
        active2 = active
    lufs = -0.691 + 10.0 * np.log10(float(active2.mean()))
    return float(10 ** ((target_lufs - lufs) / 20.0))


def _to_pcm16_interleaved(data: np.ndarray) -> bytes:
    """float 音频转交错 int16 PCM 字节流。"""
    if data.ndim == 1:
        return (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    n_ch = data.shape[1]
    inter = np.empty(n_ch * len(data), dtype="<i2")
    for c in range(n_ch):
        inter[c::n_ch] = (np.clip(data[:, c], -1.0, 1.0) * 32767.0).astype("<i2")
    return inter.tobytes()


_FFMPEG_PATH: str | None = None
_last_mp3_encoder = ""


def get_last_mp3_encoder() -> str:
    """最近一次 MP3 编码使用的编码器描述（供界面日志显示）。"""
    return _last_mp3_encoder


def _find_ffmpeg() -> str | None:
    """查找 ffmpeg：PATH → imageio-ffmpeg（venv 内置）→ winget/scoop/
    常见安装目录，结果缓存。"""
    global _FFMPEG_PATH, _last_mp3_encoder
    if _FFMPEG_PATH is not None:
        return _FFMPEG_PATH or None
    candidates = []
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(found)
    try:
        import imageio_ffmpeg  # noqa: PLC0415

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe:
            candidates.append(str(exe))
    except ImportError:
        pass
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates.append(os.path.join(local, "chillisuno", "ffmpeg", "ffmpeg.exe"))
        candidates.append(os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"))
        candidates.append(os.path.join(local, "Microsoft", "WinGet", "Packages"))
    prog = os.environ.get("ProgramFiles", "")
    if prog:
        candidates.append(os.path.join(prog, "ffmpeg", "bin", "ffmpeg.exe"))
    user = os.environ.get("USERPROFILE", "")
    if user:
        candidates.append(os.path.join(user, "scoop", "shims", "ffmpeg.exe"))
        candidates.append(os.path.join(user, "chocolatey", "bin", "ffmpeg.exe"))
    for cand in candidates:
        if not cand:
            continue
        if os.path.isfile(cand):
            _FFMPEG_PATH = cand
            return cand
        if cand.endswith("Packages") and os.path.isdir(cand):
            for entry in os.scandir(cand):
                if entry.is_dir() and entry.name.lower().startswith("gyan.ffmpeg"):
                    exe = os.path.join(entry.path, "ffmpeg.exe")
                    if os.path.isfile(exe):
                        _FFMPEG_PATH = exe
                        return exe
    _FFMPEG_PATH = ""
    return None


def _write_mp3(
    data: np.ndarray, sr: int, dst: Path, bitrate_kbps: int = 192
) -> Path:
    """有损 MP3 重编码，按可用性选编码器：

    1. lameenc（Python 包、内置 libmp3lame，可指定 CBR 码率）；
    2. 系统 ffmpeg（PATH 或 winget/scoop/常见目录）——libmp3lame
       CBR，精确控制码率，与参照样本一致；
    3. libsndfile 内置 LAME（VBR，码率不可控）——兜底。
    记录所用编码器到 get_last_mp3_encoder()。
    """
    global _last_mp3_encoder
    pcm = _to_pcm16_interleaved(data)
    n_ch = data.shape[1] if data.ndim > 1 else 1
    try:
        import lameenc  # noqa: PLC0415

        enc = lameenc.Encoder()
        enc.set_bit_rate(bitrate_kbps)
        enc.set_in_sample_rate(sr)
        enc.set_channels(n_ch)
        enc.set_quality(2)
        frame_sz = 1152 * n_ch * 2
        usable = (len(pcm) // frame_sz) * frame_sz
        mp3 = enc.encode(pcm[:usable]) + enc.flush()
        dst.write_bytes(mp3)
        _last_mp3_encoder = f"lameenc {bitrate_kbps}kbps CBR"
        return dst
    except ImportError:
        pass
    ffmpeg = _find_ffmpeg()
    if ffmpeg:
        raw_path = dst.with_name(dst.stem + "._raw.pcm")
        raw_path.write_bytes(pcm)
        try:
            proc = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "s16le",
                    "-ar",
                    str(sr),
                    "-ac",
                    str(n_ch),
                    "-i",
                    str(raw_path),
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    f"{bitrate_kbps}k",
                    str(dst),
                ],
                capture_output=True,
                timeout=600,
            )
        finally:
            raw_path.unlink(missing_ok=True)
        if proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
            _last_mp3_encoder = f"ffmpeg {bitrate_kbps}kbps CBR"
            return dst
    sf.write(str(dst), data, sr, format="MP3", subtype="MPEG_LAYER_III")
    _last_mp3_encoder = "内置 VBR（建议安装 ffmpeg 获得 192kbps CBR）"
    return dst


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
