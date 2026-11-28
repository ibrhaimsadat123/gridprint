# GridPrint: full project context (handover file)

This file is the single source of context for GridPrint. It is written so that a new Claude session (or any developer) can read it once and continue the work without the earlier chat history. It covers what the project is, what exists, how every part works, what went wrong and how it was fixed, how to test and deploy, and what comes next.

Last updated: 28 Nov 2026, about 18:30 PKT.

---

## 1. Who and what

- **Builders:** Ibrahim Sadat (B.S. Mathematics, NUST Islamabad, first year, 18) and his roommate Abdullah Habib (mechanical engineering). Ibrahim does the code, maths and hardware; Abdullah is the engineer on the team.
- **Context:** GridPrint is Ibrahim's **Hack Club Crossroads** project. It is open source. Ibrahim tracks coding time with **Hackatime** (in VS Code), so some code is intentionally left for him to type himself (see section 11).
- **GitHub:** https://github.com/ibrhaimsadat123/gridprint (note the username spelling: `ibrhaimsadat123`). Default branch `master`.
- **Live site:** https://ibrahimsadat123.pythonanywhere.com/gridprint (PythonAnywhere free account, username `ibrahimsadat123`; note this spelling is different from the GitHub one). The account must be renewed before **27 Dec 2026** or the web app is disabled. Planned move to Hack Club Nest once approved.
- **Ibrahim's working style:** casual and direct, wants depth and original, dramatic visual design (not generic templates). **No em-dashes in documents.** He is a beginner with git and tooling, so explain commands. Commit or push only when he asks.

### The idea in one paragraph

Every electrical grid runs at a nominal 50 Hz (Pakistan) but the real frequency wanders by tens of mHz from second to second as supply and demand change. That wander is the same everywhere on one synchronous grid and cannot be predicted or faked afterwards. Recordings made near mains electricity pick it up as a faint hum (and its harmonics at 100, 150, 200, 250, 300 Hz). This is called **ENF (Electric Network Frequency) analysis**. GridPrint logs the true grid frequency with a hardware reference station, extracts the hum from a recording, and slides it along the log to find **when** the recording was made (and, with more stations, which grid it was made on). Uses: verifying videos and audio, detecting edits and splices, dating evidence.

---

## 2. Current status (28 Nov 2026)

| Piece | State |
|---|---|
| Phase 0: can a mic hear the grid? | Done. Two laptops on different floors, correlation 0.99. Phone videos carry it too. |
| Station isb-01 (Islamabad hostel room) | Built and was live from 05:51 PKT 28 Nov. **Went offline around 10:08 AM 28 Nov.** Its last state was "on inverter since ~9:00", most likely the inverter battery or the Wi-Fi router lost power. Not caused by any deploy. Needs a physical check (power, router, USB). |
| Server + private explorer | Live on PythonAnywhere. Login only. |
| Public homepage with WebGL hero | Live at `/gridprint`. |
| Team / contact page + wave page transitions | Live (deployed 28 Nov evening). |
| Backup power (inverter/UPS) detection | Live. |
| Probable-cause engine (~80 rules) | Live, shown in the insight orb hover. |
| AI reports (Gemini) | Live, admin feature. |
| **Proof of matching, original phone video** | Done: A1 iPhone video (50 min) matched to the station: fit 0.92, start within 1 s of the file's metadata. |
| **Proof of matching, WhatsApp-compressed copy** | Done: 19:34 WhatsApp copy, no metadata, blind search over 07:40 to 09:00 found 08:18:44 with fit 0.995 (runner-up 0.786). |
| `match.py` (user-facing time-finding tool) | **Not written yet**; designed, waiting for Ibrahim to type it (Hackatime). |
| Second station (Lahore) | Not started. |

---

## 3. Repository map

```
README.md                      public README with the dev journal (Days 1 to 6) and screenshots
PROJECT.md                     this file
gridprint_core.py              shared DSP engine: load any audio/video, track the hum (hum_trace), judge strength
hum_test.py                    record from the laptop mic and plot the hum (Day 1)
compare.py                     do two simultaneous recordings share the same wobble? (Day 2)
extract.py                     is the grid in this file? checks 5 harmonics, verdict strong/weak/none (Day 4)
gridprint_station_uno/         ATmega328P firmware: counts grid cycles, one line per second
gridprint_uplink/              ESP8266 firmware: NTP time, offline flash buffer, HTTPS upload every 5 s
  secrets.example.h            template; real secrets.h is gitignored and must NEVER be committed
server/gridprint_server.py     the whole backend + all pages in ONE file (stdlib only, WSGI + standalone)
server/uplot.min.js/.css       uPlot 1.6.31 (also loaded from jsDelivr with SRI)
server/web/                    editable sources of the pages that are embedded into the server file:
  home.html, home.js           public homepage (WebGL terrain, scroll story, proof chart)
  explorer.html, app-fx.js     private explorer layout + motion layer
  team.html, team.js           public team/contact page
  wave.js                      shared wave page transition
  proof.json                   data for the homepage proof chart (A1 phone vs station)
  ibrahim.webp                 Ibrahim's profile photo (520x520, ~15 KB)
tools/build_server.py          injects server/web/* into server/gridprint_server.py
docs/                          README screenshots
compare_*.png                  Day 2 result plots
```

**Not in git (on purpose):** recordings (`*.MOV *.mp4 *.m4a *.wav *.ogg *.opus`), `*.npy` arrays, `secrets.h`, the PythonAnywhere WSGI header (holds the token, password hashes and Gemini key). Ibrahim's private test videos live on his Mac in `~/Desktop/gridprint/videos/` (`a1_quiet.MOV` 952 MB, `a1_quiet_audio.m4a`, `a1_quiet_wa.mp4`, `a1_quiet_wa_audio.flac`).

---

## 4. Hardware: GridPrint Station v0 (isb-01)

```
mains socket -> boxed 230 V / 12 V transformer (375 mA fuse, 14.4 V AC unloaded)
  -> 3.3 kOhm resistor -> PC817 optocoupler LED (1N4007 reverse diode across the LED)
  -> optocoupler transistor pulls Arduino D2 LOW once per grid cycle (galvanic isolation)
  -> Arduino UNO + WiFi clone (ATmega328P + ESP8266 on one board, CH340 USB)
```

- **ATmega sketch** (`gridprint_station_uno.ino`): interrupt on D2 (`INPUT_PULLUP`, the internal ~35 kOhm pull-up; the external 10 kOhm pull-up was removed after the loose-wire bug). Frequency from exact cycle timing. Prints every second:
  `seconds,frequency_hz,pulses,avg_pulse_width_us,status,d2_low_pct`
  status is `OK`, `GLITCH` (missing/extra cycles) or `OUTAGE`. `d2_low_pct` is the share of the second D2 was LOW: ~50 healthy, ~100 stuck LOW, ~0 stuck HIGH.
- **ESP8266 sketch** (`gridprint_uplink.ino`): prefixes real UTC time from NTP, sends 5-second packets over one kept-alive HTTPS connection to `POST /gridprint/ingest` with an `X-Token` header. Offline: writes 10-minute files to LittleFS (~22 h), uploads backlog oldest-first when back.
- **Board DIP switches:** upload to ESP8266 with 5, 6, 7 ON; run with 1, 2, 3, 4 ON (3+4 let the laptop listen in). Upload to ATmega with the USB-to-ATmega setting. Use a data USB cable (a charge-only cable gave "Device Descriptor Request Failed").
- **Wire format received by the server** (one line per second): `unix_time,frequency_hz,pulses,avg_pulse_width_us,status[,d2_low_pct]`.
- **Station facts learnt:** Pakistan's grid swings much more than Europe's: 49.5 to 50.5 Hz in 10 to 15 minute cycles in the evening. The room's socket switches to an **inverter** during load-shedding; the inverter runs at a dead-flat **50.045 Hz** (crystal), which is how backup detection works.

---

## 5. Server: `server/gridprint_server.py`

One file, Python standard library only, runs as WSGI (`application`) on PythonAnywhere or standalone (`python3 gridprint_server.py`, uses `PORT`). About 3,000 lines including embedded pages.

### Environment variables

| Var | Meaning |
|---|---|
| `GRIDPRINT_TOKEN` | shared secret the station sends as `X-Token` |
| `GRIDPRINT_DATA` | data dir, daily CSVs per station: `DATA/<station>/<YYYY-MM-DD>.csv` (PKT days) |
| `GRIDPRINT_AUTH` | sessions, lockouts, settings JSON |
| `GRIDPRINT_USERS` | JSON list `[{"user","hash","role":"admin"|"viewer"}]`, PBKDF2 hashes made with `python3 gridprint_server.py hash` |
| `GRIDPRINT_INSECURE_COOKIES=1` | local http testing only |

The **Gemini API key** is not an env var: an admin sets it (and the model) in the explorer's Admin panel, and it is stored in `GRIDPRINT_AUTH/settings.json` on the server. Change it there.

On PythonAnywhere these are set in the first **992 characters** of the WSGI file (`/var/www/ibrahimsadat123_pythonanywhere_com_wsgi.py`), followed by the full server code. **Never copy that header into git or chat.**

### Accounts

Three accounts: two admins (Ibrahim Sadat, Abdullah Habib) and one view-only guest. No sign-up; accounts are made by hand (the login page says "New here? Create an account: contact the GridPrint team" and links to `/gridprint/team`). **The current passwords and the Gemini key were pasted into a chat earlier and must be treated as exposed: change all of them** (generate new hashes, update the WSGI header, revoke sessions via the admin panel).

### Routes

Public:
- `GET /gridprint`, `/gridprint/`, `/gridprint/home` homepage
- `GET /gridprint/team`, `/gridprint/contact` team page
- `GET /gridprint/login`, `POST /gridprint/login` (redirects to `/gridprint/explorer`)
- `GET /gridprint/static/home.js | team.js | wave.js | ibrahim.webp`
- `GET /gridprint/api/pulse` coarse public data: last 6 h at 10 s steps, cached 20 s (the only data the public can see)
- `POST /gridprint/ingest` stations only (X-Token)

Session required (unauthenticated `GET /explorer` redirects to login, everything else 401):
- `GET /gridprint/explorer` the app; `/static/app.js`, `/static/app-fx.js`, `/static/uplot.js|css`
- `GET /api/me`, `/api/stations`, `/api/days`, `/api/range?station&t0&t1&points`, `/api/export` (CSV), `/api/insight`, `POST /api/report` (Gemini)
- Admin only (403 for viewers): `/api/admin/sessions`, `/api/admin/revoke`, `/api/admin/settings`
- `POST /logout`

### Security design

PBKDF2 password hashes; equal-time check for unknown users; session tokens stored hashed; HttpOnly + Secure + SameSite cookies; viewer idle timeout 12 h, admin "remember me" 30 days; login lockout after repeated failures (15-minute window, per user and IP); same-origin check on POSTs (CSRF); strict headers:

```
CSP: default-src 'self'; script-src 'self' https://cdn.jsdelivr.net;
     style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; font-src https://cdn.jsdelivr.net;
     img-src 'self' data:; connect-src 'self'
```

**No inline scripts are allowed**, so every script is a separate route. All CDN files are pinned with SRI hashes. A probe script tried ~80 route/path tricks (`//explorer`, `%65xplorer`, `/home/../explorer`, forged cookies, etc.); all are refused. Re-run this kind of probe after any routing change.

### Data path

- `store()` appends parsed lines to daily CSVs. `Day` objects cache parsed days; `range_query()` downsamples to N points (min/max per bucket so spikes survive), returns outages, glitches, **backup spans** and stats (stats exclude backup readings, `stats.backup_s`).
- `sensor_diag()` labels each outage: power cut vs sensor fault vs loose wire, using `d2_low_pct`.

### Backup (inverter) detection: `BackupDetector`

The real grid never holds still; an inverter is crystal-flat. Readings are grouped in 30 s blocks. A block is "flat" if its trimmed peak-to-peak (dropping 1 or 2 outliers) is at most 0.003 Hz with at least 15 readings. Four or more consecutive flat blocks at the same level (within 0.003 Hz) is a backup span; edges are walked back/forward to the exact switch-over second. Cached per day incrementally (`_bk_cache`). `range_query` returns `backup: [[a, b, mean_hz, live], ...]`; the explorer shows an "ON BACKUP" pill and shading.

### Live insight and probable cause

- `grid_features()` measures the window: turning points, rises/falls with slopes, steepest minutes, cycles, sunrise/sunset (`sun_times_pkt`).
- `_context()` builds a context object (current frequency, 5-minute slope, the biggest recent event, time of day, backup state, etc.).
- `CAUSE_RULES`: ~80 weighted rules (lambdas over the context, helpers `E, DN, UP, STEP, RAMP`), each producing a plain-English likely cause (for example evening peak load pickup, a sudden loss of generation followed by reserve overshoot, load shedding blocks switching, Fajr/morning ramp, inverter running).
- `probable_cause(c)` picks the best-scoring sentence with a confidence. `live_insight()` returns `{mood, cause, basis, confidence, at}`; mood is calm / active / stressed / backup. The orb hover shows **only** the probable cause (Ibrahim asked to keep it short).

### AI reports

`build_digest()` turns measured features into a compact digest; `REPORT_PROMPT` asks Gemini to tell the story of what the grid did and why; `gemini_call()` tries a list of models (`gemini-2.5-flash`, then fallbacks). Every time mentioned in a report is clickable and jumps the chart.

---

## 6. Front end

Design system (dark "night grid"): background `#0A1120`, panel `#121D33`/`#15213A`, line `#24324F`, ink `#EAF0F8`, muted `#8494B2`, sodium amber accent `#F2A23A`, ok `#3DD68C`, bad `#F2555A`, warn `#FF8F6B`, cyan `#6FD6FF`. Fonts from fontsource on jsDelivr (SRI pinned): **Bricolage Grotesque Variable** (display, narrow `font-stretch` ~78 to 84%) and **IBM Plex Sans** 400/600.

Libraries (all from jsDelivr with SRI): GSAP 3.15.1 + ScrollTrigger + SplitText + Flip, Lenis 1.3.29, uPlot 1.6.31. The 3D is **hand-written WebGL** (no three.js).

- **Homepage** (`home.html`, `home.js`): a 520vh scroll sequence. WebGL terrain of the live grid (each row is a time slice from `/api/pulse`, rendered as particles; vertex shader uses the same projection as the CPU `projector()`), a 2D overlay canvas `#fx` draws the newest ridge, and the scroll morphs it into the **proof chart** (A1 phone hum vs station, from `proof.json`). SplitText headline, number count-ups (skips values like "8:22" where the remainder starts with ":"), Lenis smooth scroll driven by `gsap.ticker`. Section "Built by two roommates, one socket at a time." with "Meet the team". Footer links: Team and contact, Sign in.
- **Explorer** (`explorer.html` + generated `app.js` + motion layer `app-fx.js`): app bar (brand to home, station select, status pill, user, Admin, Sign out), meter deck (odometer digits, SVG gauge with needle and bands, deviation readout, insight orb), status panel with `#why` (probable cause), presets segmented control, uPlot chart (`data-lenis-prevent`), side details panel, event and stats tables, report drawer, admin dialog. `app-fx.js` animates by **watching DOM changes** that `app.js` makes (MutationObservers on cNow, cStatus, cRange, events, stats, orbPop, details, reportCard, adminDlg). It is mobile responsive (wrapping, scrollable segment control).
- **Team page** (`team.html`, `team.js`): canvas background of perspective wave strings that drift with the pointer; two 3D cards that tilt toward the pointer (GSAP `quickTo`) with a moving glare, and flip (rotateY) to a back face with a `mailto:` button. Ibrahim: photo + bio, ibrahimsadat716@gmail.com. Abdullah: default SVG avatar, abdullahhabibsaleemi786@gmail.com (his bio is minimal on purpose; ask Ibrahim for details before adding any).
- **Wave transition** (`wave.js`, loaded synchronously in `<head>` of every page): clicking any `a[data-go]` or submitting a `form[data-go]` starts a full-screen canvas wave that rises and floods the screen while the "camera" pushes in (wavelength tightens, speed rises); after ~620 ms navigation starts while the wave keeps oscillating with an oscilloscope trace and "tuning in…". A `sessionStorage` flag (`gp_wave`, 8 s validity) tells the next page to start covered and drain the wave away after `load` (hard cap 2.5 s). Respects `prefers-reduced-motion`; ignores modified clicks, new tabs and other origins; handles bfcache via `pageshow`. Canvas `shadowBlur` was removed (it froze software rendering); glow is done with stacked translucent strokes.

---

## 7. Signal processing and matching (the science)

- `gridprint_core.hum_trace(audio, centre, window_s, half)`: STFT with 1 s hop, search a ±`half` Hz band around a harmonic, parabolic interpolation between bins, scale back to 50 Hz (`trace * 50 / centre`), median filter. Returns times, grid frequency, strength (peak over median).
- **What worked best on phone audio:** combine the **200 Hz and 300 Hz** harmonics weighted by strength (use the average where they agree within 0.03 Hz, otherwise the stronger one), then reject outliers where `|v - medfilt(v, 61)| >= 0.03`. Window 8 s, half-band 3 Hz for the WhatsApp copy.
- **Matching:** resample the station log to a 1 s grid, slide the recording's trace over every candidate start second, Pearson correlation on the kept points, report the best start, the best score elsewhere (more than 120 s away) and a z-score. This is a **blind** search: no metadata used.
- **Results (28 Nov 2026, station data 07:40 to 09:00 PKT):**
  - A1 original iPhone video (`a1_quiet.MOV`, 50 min, created 08:02:05 PKT): fit **0.92** (best wrong time 0.49), mean absolute error 0.005 Hz, start within 1 s of the metadata.
  - WhatsApp copy (`a1_quiet_wa.mp4`, 19:34, AAC 63 kbps, no timestamp): best start **08:18:44**, fit **0.995**, runner-up 0.786, MAE 0.003 Hz. That is 999 s into the original; an independent audio-to-audio alignment said 1000 s. The **power cut** in the clip was heard at 50.0454 Hz (inverter).
  - **Caveat:** the power cut gives the hum a very distinctive shape, which makes matching easy. The next honest test is a clip **without** a cut, searched against a **full day**.
- **Calibration:** at one point the station looked 0.045 Hz high. It was checked two ways (3000 readings in 3000 s, and phone audio vs station median difference 0.0000 Hz): the station is correct, the inverter really runs at 50.045 Hz. No calibration offset is applied.

---

## 8. Problems hit and how they were solved

| Problem | Cause | Fix |
|---|---|---|
| Fake OUTAGE at 10:28 PM 27 Nov; pulses stretched 10 to 19 ms then vanished | 5 V wire to the external pull-up came loose; a floating pin picks up mains hum through the air and fingers, giving convincing fake 50 Hz | Added `d2_low_pct` diagnostic; switched to `INPUT_PULLUP`; server labels outage causes |
| First PC817 never switched | dead part | replaced |
| Board would not enumerate | charge-only USB cable, CH340 driver, DIP switch positions | data cable, driver, documented switches |
| "Station reads 0.045 Hz high" (wrong claim) | was the inverter, not the sensor | verified, no change |
| WebGL hero rendered upside-down rows | projection sign | `Y = y*cp + Z0*sp`, `Z = Z0*cp - y*sp` |
| Odometer offset doubled | inline transform and GSAP yPercent both set | only GSAP sets it |
| Gauge needle rotated around the wrong point | CSS transform-origin combined with SVG `rotate(a cx cy)` | removed the CSS rule |
| Report drawer stuck off-screen | its own style MutationObserver retriggered its tween | `repOpen` / `detWas` state flags |
| Mobile overflow (546 px wide page on 390 px) | long rows | wrapping + scrollable segment control |
| Count-up turned "8:22 AM" into nonsense | counted the "8" | skip values whose remainder starts with ":" |
| Wave transition froze screenshots | canvas `shadowBlur` on a full-screen path is very slow in software rendering | stacked translucent strokes instead |
| Staging a 75 MB video from the Mac timed out | file size over the bridge | extracted FLAC audio on the Mac with ffmpeg first |
| PythonAnywhere editor Save sometimes did nothing | editor quirk | click the visible Save button again; check the tab title loses its `*` |
| Station offline since ~10:08 AM 28 Nov | inverter battery / router power (suspected) | **open**: physical check |

---

## 9. How to develop, test and deploy

### Edit and rebuild

1. Edit the page sources in `server/web/` or the Python in `server/gridprint_server.py`.
2. `python3 tools/build_server.py` injects the pages, `proof.json` and the photo into the server file (it refuses content containing `'''`).

### Run locally

```bash
cd server
export GRIDPRINT_TOKEN=devtoken GRIDPRINT_INSECURE_COOKIES=1 \
       GRIDPRINT_DATA=/tmp/gp/data GRIDPRINT_AUTH=/tmp/gp/auth PORT=8772
export GRIDPRINT_USERS="[{\"user\":\"Ibrahim Sadat\",\"role\":\"admin\",\"hash\":\"$(python3 -c 'import gridprint_server as g;print(g.hash_password("testpass-local"))')\"}]"
python3 gridprint_server.py
# open http://127.0.0.1:8772/gridprint/
```

Feed fake station data by POSTing lines to `/gridprint/ingest` with header `X-Token: devtoken`, for example a line `1795834925.0,50.0123,50,10400,OK,50` per second (or generate a day of CSV directly into `GRIDPRINT_DATA/isb-01/YYYY-MM-DD.csv` with the header `unix_time,frequency_hz,pulses,avg_pulse_width_us,status,d2_low_pct`).

### Browser tests (Playwright, headless)

- Chromium WebGL in a container needs: `--use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader`.
- Route `https://cdn.jsdelivr.net/**` to local npm copies of gsap, lenis, uplot and the fontsource packages (abort anything unmapped, or the route handler crashes).
- **Do not** freeze the clock with `page.clock.set_fixed_time`; it stops GSAP and the page looks blank.
- Check desktop 1440x900 and mobile 390x844, collect `pageerror` and console errors (ignore swiftshader "GPU stall due to ReadPixels" warnings).
- Security probe: request every private route without a cookie, with forged cookies, and with path tricks; expect 401/403/303 only.

### Deploy to PythonAnywhere (current method)

The live code is the WSGI file. Header (first 992 characters, secrets) + server code.

- Simple way for a human: open the WSGI file in PythonAnywhere's editor, keep the header lines, paste the new `server/gridprint_server.py` below them, Save, then press **Reload** on the Web tab. Check `/gridprint`, `/gridprint/team`, `/gridprint/login`, and that `/gridprint/static/app.js` returns 401 when signed out.
- The automated way used so far (from a browser-automation session): build a line diff (difflib opcodes) between the live code and the new code, gzip + base64 it, push it in ~10 KB chunks into `window.__pz` in the editor tab, verify the **SHA-256 of the live code** before patching and of the patched result after, then `ace.edit(...).setValue(header + newCode)`, click Save, click Reload, wait ~15 s, verify routes.
- **Current live code hash (code after the 992-char header):** `829aefb140915ba808bcc0b3ce8aceda163ba009445f7dfdf1271356093342c7` (this equals the `server/gridprint_server.py` committed with this file).

---

## 10. Rules that must keep holding

- Never commit or paste: `secrets.h`, the station token, Wi-Fi password, password hashes, the WSGI header or the Gemini key. Private recordings stay out of GitHub.
- Public pages expose only the coarse pulse (`/api/pulse`). The guest password never appears on public pages.
- Keep every script external (CSP has no `unsafe-inline` for scripts) and every CDN file SRI-pinned.
- Re-run the security probe after any routing change.
- No em-dashes in documents. Commit/push only when Ibrahim asks.

---

## 11. What is next (prioritised)

1. **Bring station isb-01 back online** (power, router, USB, check the uplink's offline buffer uploads the gap).
2. **Rotate exposed secrets:** all three passwords, the Gemini API key; revoke sessions. Renew PythonAnywhere before 27 Dec 2026.
3. **`match.py`, the time-finding tool** (Ibrahim types it himself for Hackatime; give him pieces to type, not a finished file). Planned pieces:
   1. load a recording (reuse `gridprint_core` loaders) and the station CSVs for a date range;
   2. build the combined 200 + 300 Hz trace with outlier rejection (section 7);
   3. blind correlation search over every start second, with best, runner-up and z-score;
   4. verdict and confidence (thresholds from the A1 tests), plus edit/splice detection by matching sliding sub-windows separately;
   5. a plot (recording trace over the matched station segment) and a short text report.
4. **Harder test:** a clip with **no power cut**, searched against a **full day** of station data, to measure real-world false-match rates.
5. **Public verify page:** upload a clip, get "matches the grid at HH:MM:SS with confidence X" (server-side processing; mind PythonAnywhere CPU limits, maybe do the DSP in the browser with WebAudio + FFT).
6. **Second station in Lahore** (same national grid, ~300 km away) to prove the signal is grid-wide, not local.
7. **Speech-proof extraction:** hum between words, voice-harmonic filtering, and light flicker in video frames (rolling-shutter banding).
8. **Move hosting to Hack Club Nest** (update `SERVER_URL` in the ESP's `secrets.h`).
9. Crossroads devlog update and README journal Day 7 (inverter detection, homepage, A1 and WhatsApp matches, team page).
10. Possible extras discussed: a flicker experiment, push notifications on big grid events, Abdullah's bio and photo once he provides them.

---

## 12. Prompt for a new Claude session

> I'm Ibrahim Sadat. Read PROJECT.md in https://github.com/ibrhaimsadat123/gridprint first; it has the full context of my GridPrint project (ENF / grid-hum verification, Hack Club Crossroads). Keep my rules: no em-dashes in documents, never expose secrets, commit and push only when I ask, and for `match.py` give me code in pieces so I can type it in VS Code for Hackatime. Let's continue with: <next task>.
