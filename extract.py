# GridPrint: extractor
# Is the power grid hiding in this recording? Works on our .npy recordings and on any
# audio or video file: WhatsApp voice notes, phone videos, .mp3, .m4a, .mp4 ...
#
# Usage:  python extract.py some_file.mp4
#         python extract.py voice_note.opus laptop1_b.npy ...   (several at once)

import sys
import numpy as np
import matplotlib.pyplot as plt
from gridprint_core import FS, load_audio, hum_trace, grid_report

ICON = {"STRONG": "STRONG grid signal", "WEAK": "weak grid signal", "NONE": "no grid signal found"}


def analyse(path, show_plot=True):
    audio = load_audio(path)
    seconds = len(audio) / FS
    rows, verdict, best = grid_report(audio)

    print(f"\n=== {path}  ({seconds:.0f} s) ===")
    print(" echo     grid Hz   wobble   strength   note")
    for r in rows:
        note = ("device tone, not grid" if r["device"] else
                "out of grid range" if not r["plausible"] else
                "too jumpy (noise)" if not r["steady"] else
                "too faint" if r["snr"] < 5 else "grid-like")
        mark = "*" if r is best and verdict != "NONE" else " "
        print(f"{mark}{r['harmonic']:>4} Hz  {r['grid_hz']:8.3f}   {r['spread']:6.3f}   {r['snr']:7.1f}   {note}")
    print(f">>> {ICON[verdict]}", end="")
    if verdict != "NONE":
        backed = f", backed up by {best['support']} Hz" if best.get("support") else ""
        print(f"  (best echo {best['harmonic']} Hz, grid around {best['grid_hz']:.3f} Hz{backed})")
    else:
        print()
    if seconds < 60:
        print("    note: under a minute is short; longer clips give much more reliable results")

    if show_plot and verdict != "NONE":
        t, trace, strength = hum_trace(audio, centre=best["harmonic"])
        fig, ax = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
        ax[0].plot(t, trace, "o-", ms=3)
        ax[0].set_title(f"{path}: grid frequency from the {best['harmonic']} Hz echo ({ICON[verdict]})")
        ax[0].set_ylabel("grid Hz")
        ax[1].plot(t, strength, color="gray")
        ax[1].axhline(4, color="r", ls="--", lw=0.8)
        ax[1].set_title("Signal strength over time (above the red line = usable)")
        ax[1].set_xlabel("seconds")
        plt.tight_layout()
        plt.savefig(path.rsplit(".", 1)[0] + "_extract.png")
        plt.show()
    return verdict, best


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python extract.py <audio or video file> [more files...]")
    many = len(sys.argv) > 2
    results = [(p, *analyse(p, show_plot=not many)) for p in sys.argv[1:]]
    if many:
        print("\n=== summary ===")
        for p, v, b in results:
            print(f"{ICON[v]:<22} {p}")
