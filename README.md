# GridPrint

**Verify when and where a recording was made, using the hum of the power grid.**

Every electrical grid runs at a nominal 50 Hz (60 Hz in the Americas), but the real frequency wanders slightly from second to second as demand changes. That wander is shared across the whole grid and can't be faked after the fact. Recordings made near mains electricity quietly capture it, through lights, fans, transformers and chargers. GridPrint extracts that hidden "grid fingerprint" from audio and video and matches it against a reference log to check a recording's time and place.

Built for [Hack Club Crossroads](https://crossroads.hackclub.com).

## Status

Phase 0: proving a cheap laptop mic can hear the grid at all.

## Dev journal

### Day 1 (24 Nov 2026): hum test

- Wrote `hum_test.py`: records audio, computes the spectrum, and tracks the exact hum frequency every second using an STFT with parabolic peak interpolation.
- **50 Hz: nothing.** The laptop mic (Intel Smart Sound array) filters out low frequencies, so the 50 Hz trace was pure noise.
- **100 Hz: a signal.** The second harmonic (100 Hz) showed a steady tone that drifts slowly, which is what grid frequency should look like. 100 Hz is the channel to track from here.
- Turned off Windows "Audio enhancements", since its noise suppression is designed to remove steady hums.
- **Plugged vs unplugged:** both recordings showed the 100 Hz tone. Plugged sat around 50.00 Hz (after halving), unplugged drifted smoothly from about 50.3 down to 50.15 Hz. The hum reaches the mic through the air from the room's wiring, not only through the charger, so unplugging doesn't remove it.
- A fixed spike near 95 Hz appears in both recordings and never moves. Probably a fan or motor, not the grid.

**Next:** record on two devices in different places at the same moment. If both hum traces rise and fall together, it's the grid.

## Run it

```
python -m pip install numpy scipy matplotlib sounddevice
python hum_test.py
```

Keep the room quiet while it records.
