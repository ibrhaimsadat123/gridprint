# GridPrint: two-device comparison
# Do two recordings made at the same time, in different places, share the same hum wobble?
# If yes, the hum is the power grid (the whole grid shares one frequency).
#
# Usage:  python compare.py laptop1 laptop2

import sys
import os
import numpy as np
import matplotlib.pyplot as plt
from gridprint_core import load_audio, hum_trace


def load(name):
    audio = load_audio(name + ".npy")
    start = 0.0
    if os.path.exists(name + "_start.txt"):
        start = float(open(name + "_start.txt").read())
    else:
        print(f"(no {name}_start.txt found, assuming it started at the same moment)")
    return audio, start


def main(name1, name2):
    a1, s1 = load(name1)
    a2, s2 = load(name2)
    t1, h1, _ = hum_trace(a1)
    t2, h2, _ = hum_trace(a2)

    # put both traces on one shared clock (seconds since the earlier start)
    t0 = min(s1, s2)
    t1 = t1 + (s1 - t0)
    t2 = t2 + (s2 - t0)
    print(f"Start time difference: {abs(s1 - s2):.2f} s")

    # keep only the overlap, resampled once per second
    lo, hi = max(t1[0], t2[0]), min(t1[-1], t2[-1])
    if hi - lo < 20:
        sys.exit("The recordings barely overlap in time. Did you start them together?")
    grid_t = np.arange(np.ceil(lo), np.floor(hi))
    x = np.interp(grid_t, t1, h1)
    y = np.interp(grid_t, t2, h2)

    # how similar is the wobble? (remove each device's average first)
    xc, yc = x - x.mean(), y - y.mean()
    r = np.corrcoef(xc, yc)[0, 1]

    # check small time offsets too, in case the clocks were a few seconds off
    best_lag, best_r = 0, r
    for lag in range(-5, 6):
        if lag == 0:
            continue
        xs, ys = (xc[lag:], yc[:-lag]) if lag > 0 else (xc[:lag], yc[-lag:])
        rl = np.corrcoef(xs, ys)[0, 1]
        if rl > best_r:
            best_lag, best_r = lag, rl

    print(f"Average hum: {name1} {x.mean():.3f} Hz, {name2} {y.mean():.3f} Hz")
    print(f"Correlation of the wobble: {r:.2f}  (best {best_r:.2f} at a {best_lag:+d} s shift)")
    # second-by-second check: do the small wiggles match, not just the slow trend?
    dr = np.corrcoef(np.diff(xc[::8]), np.diff(yc[::8]))[0, 1]   # compare changes every 8 s, not every 1 s
    print(f"Wiggle match (second-by-second changes): {dr:.2f}  (1.0 = identical, 0 = unrelated)")
    print("Note: a high correlation with a low wiggle match means the devices share the slow drift,")
    print("but one of them is too noisy to see the fine detail. Record somewhere quieter / closer to wiring.")
    if best_r > 0.7:
        print(">>> STRONG MATCH: both devices heard the same grid. GridPrint works.")
    elif best_r > 0.4:
        print(">>> Partial match: promising, but record longer or somewhere quieter.")
    else:
        print(">>> No match: the hum on at least one device isn't the grid (or is drowned in noise).")

    fig, ax = plt.subplots(2, 1, figsize=(11, 7))
    ax[0].plot(grid_t, x, "o-", label=name1)
    ax[0].plot(grid_t, y, "o-", label=name2)
    ax[0].set_title("Grid hum on both devices (Hz)")
    ax[0].set_ylabel("Hz"); ax[0].legend()
    ax[1].plot(grid_t, xc, label=name1 + " (average removed)")
    ax[1].plot(grid_t, yc, label=name2 + " (average removed)")
    ax[1].set_title(f"Do they wobble together?  correlation = {best_r:.2f}")
    ax[1].set_xlabel("seconds"); ax[1].legend()
    plt.tight_layout()
    plt.savefig(f"compare_{name1}_{name2}.png")
    plt.show()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: python compare.py laptop1 laptop2")
    main(sys.argv[1], sys.argv[2])
