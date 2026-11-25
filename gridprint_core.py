# GridPrint core: shared tools used by every script
#
#   load_audio(path)   any audio/video file -> mono samples at FS
#   hum_trace(audio)   track the grid frequency over time
#   grid_report(audio) how strong is the grid in this recording?

import os
import subprocess
import numpy as np
from scipy.signal import stft, medfilt

FS = 8000                       # samples per second, used everywhere
HARMONICS = [2, 3, 4, 5, 6]     # 100, 150, 200, 250, 300 Hz (50 Hz itself is cut by most mics)


# ---------- loading ----------

def load_audio(path):
    """Load .npy (our own recordings) or any audio/video file ffmpeg can read
    (.wav .mp3 .m4a .ogg .opus .mp4 .mov ...), as mono float samples at FS."""
    if path.endswith(".npy"):
        return np.load(path).astype(np.float64)
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        ffmpeg = "ffmpeg"       # fall back to an ffmpeg on the PATH
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    # decode -> mono -> resample to FS -> raw 16-bit samples on stdout
    cmd = [ffmpeg, "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(FS),
           "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    if len(audio) == 0:
        raise ValueError(f"No audio track found in {path}")
    return audio


# ---------- tracking ----------

def hum_trace(audio, centre=100, window_s=16, half=3):
    """Track the grid hum near `centre` Hz (a multiple of 50) over time.
    Returns times (s), frequency scaled back to grid Hz (around 50), and per-window strength."""
    window_s = min(window_s, max(2, len(audio) // FS // 2))   # short clips get shorter windows
    f, t, Z = stft(audio, fs=FS, nperseg=FS * window_s, noverlap=FS * (window_s - 1))
    band = (f > centre - half) & (f < centre + half)
    fb, mag = f[band], np.abs(Z[band])
    step = fb[1] - fb[0]
    trace, strength = [], []
    for col in mag.T:
        i = np.argmax(col)
        # parabolic interpolation: finds the peak BETWEEN frequency bins
        if 0 < i < len(col) - 1:
            a, b, c = np.log(col[i - 1:i + 2] + 1e-12)
            shift = 0.5 * (a - c) / (a - 2 * b + c)
        else:
            shift = 0
        trace.append(fb[i] + shift * step)
        strength.append(col[i] / (np.median(col) + 1e-12))
    trace = np.array(trace) * 50 / centre    # e.g. 100.67 Hz at the 100 Hz echo -> 50.335 grid Hz
    k = min(9, len(trace) - (1 - len(trace) % 2))            # median filter needs an odd size
    if k >= 3:
        trace = medfilt(trace, k)
    return t, trace, np.array(strength)


# ---------- judging ----------

def grid_report(audio):
    """Check each grid echo (100, 150, 200... Hz) over time and decide whether the grid is here.

    The real grid has three tells:
      1. a clear tone (strength well above the noise, median over time)
      2. that moves SLOWLY and stays in a believable range (49.4 to 50.6 Hz)
      3. and is NOT parked exactly on 50.000 (that's the device's own electronics)
    Echoes of the same grid must also agree with each other.
    Returns per-echo rows, a verdict (STRONG / WEAK / NONE) and the best echo."""
    rows = []
    for k in HARMONICS:
        t, trace, strength = hum_trace(audio, centre=50 * k)
        med, sd, st = float(np.median(trace)), float(np.std(trace)), float(np.median(strength))
        device = abs(med - 50) < 0.003 and sd < 0.01
        plausible = 49.4 < med < 50.6
        steady = sd < 0.1
        rows.append(dict(harmonic=50 * k, grid_hz=med, spread=sd, snr=st, device=device,
                         plausible=plausible, steady=steady,
                         grid_like=st >= 5 and steady and plausible and not device))

    candidates = [r for r in rows if r["grid_like"]]
    if not candidates:
        return rows, "NONE", max(rows, key=lambda r: r["snr"])
    best = max(candidates, key=lambda r: r["snr"])
    # other echoes that back it up: steady, reasonably strong, and pointing at the same grid Hz
    support = [r for r in rows if r is not best and r["snr"] >= 4 and r["steady"]
               and not r["device"] and abs(r["grid_hz"] - best["grid_hz"]) < 0.02]
    best["support"] = [r["harmonic"] for r in support]
    verdict = "STRONG" if best["snr"] >= 8 or support else "WEAK"
    return rows, verdict, best
