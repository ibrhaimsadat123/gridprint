# GridPrint: hum test
# Record audio, then look for the power grid's hum at 50 Hz (and its echo at 100 Hz)

import time
import numpy as np
import sounddevice as sd
from scipy.signal import stft
import matplotlib.pyplot as plt

FS = 8000        # samples per second (plenty for a 50 Hz hum)
SECONDS = 300     # how long to record

# ---------- 1. RECORD ----------
print(f"Recording {SECONDS} seconds... keep the laptop PLUGGED IN and the room quiet")
start = time.time()   # exact start moment, so two devices can be lined up later
audio = sd.rec(int(FS * SECONDS), samplerate=FS, channels=1, dtype="float64")
sd.wait()
audio = audio[:, 0]
name = input("Name this recording (e.g. laptop1 / laptop2): ")
np.save(name + ".npy", audio)
with open(name + "_start.txt", "w") as f:
    f.write(str(start))
print(f"Saved {name}.npy (started at {time.ctime(start)})")
print("Done recording")

# ---------- 2. WHOLE SPECTRUM: is there a spike at 50 or 100 Hz? ----------
spectrum = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
freqs = np.fft.rfftfreq(len(audio), 1 / FS)

# ---------- 3. HUM OVER TIME: does the frequency wobble? ----------
def hum_trace(audio, centre, window_s=4):
    """Track the exact hum frequency near `centre` Hz, every second."""
    f, t, Z = stft(audio, fs=FS, nperseg=FS * window_s, noverlap=FS * (window_s - 1))
    band = (f > centre - 1) & (f < centre + 1)
    fb, mag = f[band], np.abs(Z[band])
    step = fb[1] - fb[0]
    trace = []
    for col in mag.T:
        i = np.argmax(col)
        # parabolic interpolation: finds the peak BETWEEN frequency bins (more precise)
        if 0 < i < len(col) - 1:
            a, b, c = np.log(col[i - 1:i + 2] + 1e-12)
            shift = 0.5 * (a - c) / (a - 2 * b + c)
        else:
            shift = 0
        trace.append(fb[i] + shift * step)
    return t, np.array(trace)

t50, trace50 = hum_trace(audio, 50)
t100, trace100 = hum_trace(audio, 100)

# ---------- 4. PLOT ----------
fig, ax = plt.subplots(3, 1, figsize=(10, 9))

zoom = (freqs > 40) & (freqs < 110)
ax[0].semilogy(freqs[zoom], spectrum[zoom])
ax[0].axvline(50, color="r", ls="--"); ax[0].axvline(100, color="r", ls="--")
ax[0].set_title("Spectrum: look for spikes on the red lines")

ax[1].plot(t50, trace50, "o-")
ax[1].set_title("Hum at 50 Hz over time")
ax[1].set_ylabel("Hz")

ax[2].plot(t100, trace100 / 2, "o-", color="orange")
ax[2].set_title("Hum at 100 Hz (divided by 2) over time")
ax[2].set_ylabel("Hz"); ax[2].set_xlabel("seconds")

plt.tight_layout()
plt.savefig("hum_test.png")
plt.show()