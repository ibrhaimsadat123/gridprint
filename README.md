# GridPrint

**Verify when and where a recording was made, using the hum of the power grid.**

Every electrical grid runs at a nominal 50 Hz (60 Hz in the Americas), but the real frequency wanders slightly from second to second as demand changes. That wander is shared across the whole grid and can't be faked after the fact. Recordings made near mains electricity quietly capture it, through lights, fans, transformers and chargers. GridPrint extracts that hidden "grid fingerprint" from audio and video and matches it against a reference log to check a recording's time and place.

Built for [Hack Club Crossroads](https://crossroads.hackclub.com).

## Status

Phase 0: proving a cheap laptop mic can hear the grid. Two laptops on different floors tracked the same grid frequency for 5 minutes (correlation 0.99).

## Dev journal

### Day 1 (24 Nov 2026): hum test

- Wrote `hum_test.py`: records audio, computes the spectrum, and tracks the exact hum frequency every second using an STFT with parabolic peak interpolation.
- **50 Hz: nothing.** The laptop mic (Intel Smart Sound array) filters out low frequencies, so the 50 Hz trace was pure noise.
- **100 Hz: a signal.** The second harmonic (100 Hz) showed a steady tone that drifts slowly, which is what grid frequency should look like. 100 Hz is the channel to track from here.
- Turned off Windows "Audio enhancements", since its noise suppression is designed to remove steady hums.
- **Plugged vs unplugged:** both recordings showed the 100 Hz tone. Plugged sat around 50.00 Hz (after halving), unplugged drifted smoothly from about 50.3 down to 50.15 Hz. The hum reaches the mic through the air from the room's wiring, not only through the charger, so unplugging doesn't remove it.
- A fixed spike near 95 Hz appears in both recordings and never moves. Probably a fan or motor, not the grid.

**Next:** record on two devices in different places at the same moment. If both hum traces rise and fall together, it's the grid.

### Day 2 (25 Nov 2026): two laptops, two floors

- Added a start timestamp to every recording so recordings from different devices can be lined up.
- Wrote `compare.py`: tracks the 100 Hz hum on two recordings, aligns them in time, and scores how closely they move together.
- **Sanity check first:** the plugged and unplugged recordings from Day 1 (made minutes apart) scored 0.04. No match, as expected. The script doesn't just say yes.
- **The experiment:** my laptop (`laptop1`) in my hostel room and a friend's laptop (`laptop2`) on the floor above, both plugged in, recording at the same time for 2 minutes. His started 15.8 s later; the timestamps line them up.
- **Result:** both laptops heard the same frequency, **50.308 Hz vs 50.301 Hz**, and both climbed together from about 50.28 to 50.36 Hz over the two minutes. Correlation **0.83**. Recordings made minutes apart had given 50.02 and 50.2, so matching numbers on two separate laptops at the same moment points to one shared source: the grid.
- **Bug caught:** the first run said 0.96, but that was inflated. The search window was only +/-1 Hz, so when the grid rose above 50.375 Hz, both traces got stuck at the window edge and looked identical. Widened it to +/-3 Hz.
- **Not there yet:** the second-by-second wiggles don't match (wiggle score about 0). His signal was about 3x weaker than mine, so his fine detail is mostly noise. The devices agree on the slow drift, not yet on the details. Added a "wiggle match" check to `compare.py` so this can't hide again.

**Next:**
- Rematch with the weaker laptop right next to a charger or tube light, recording 5 minutes instead of 2. Goal: wiggle match above 0.5.
- Record in Lahore and Islamabad at the same moment. Same national grid, about 300 km apart. If the same drift shows up in both cities, nothing local can explain it.

### Day 3 (26 Nov 2026): grid match confirmed

![Two laptops, two floors, same grid](compare_laptop1_b_laptop2_b.png)

- Rematch, with three changes: recordings of 5 minutes instead of 2, charger brick next to each laptop's mic, and Audio enhancements off on both.
- **First result: no match (0.02).** Looked like a failure. The raw recordings showed otherwise: `compare.py` measured the hum in 4-second chunks, and with a faint signal each chunk was mostly noise.
- **Fix: 16-second chunks.** Longer chunks let the hum build up over the noise. Also a stronger median filter, and the wiggle check now compares changes every 8 seconds.
- **Result: average hum 50.336 Hz vs 50.335 Hz, correlation 0.96 to 0.99, wiggle match 0.79.** Both laptops trace the same path for 5 minutes: the climb to 50.36, the dip at ~145 s, the bump at ~170 s, the final climb to 50.39.
- **Fake-out check:** sliding one recording 30 to 120 seconds out of line drops the correlation to 0.04 at most. The match only exists when the timing is real.
- Day 2's pair, re-analysed with 16-second chunks, scores 0.99 too.
- **The grid caught a clock error:** the best match is always at a 4-second shift. My two laptops' clocks are about 4 seconds apart.
- **Brick next to the mic made my signal weaker,** about 3x versus Day 2 without it. One run only, but it hints the hum enters mostly through the charging cable, not the brick's magnetic field. To test: brick near vs far.
- **Device tones vs grid:** the second laptop shows tones at exactly 200.000, 300.000 and 400.000 Hz. Round numbers that never move are the laptop's own electronics. The grid never sits exactly on 50.000.

**Next:** brick near vs far test, Lahore + Islamabad at the same moment, and an extractor that works on any audio or video file.

## Run it

```
python -m pip install numpy scipy matplotlib sounddevice
python hum_test.py
python compare.py laptop1 laptop2
```

Keep the room quiet while it records.
