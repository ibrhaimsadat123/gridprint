"""GridPrint server v4: receives readings from GridPrint Stations, stores them, and serves a
login-protected live + history explorer with AI (Gemini) reports.

Standard library only. Two ways to run:
  * stand-alone:   GRIDPRINT_TOKEN=... GRIDPRINT_USERS='[...]' PORT=12345 python3 gridprint_server.py
  * WSGI host:     point the host at `application` (PythonAnywhere, Nest + gunicorn, ...)

Accounts are created on the backend only (there is no sign-up page). Make a password hash with
    python3 gridprint_server.py hash
and add {"user": "<login name>", "name": "<display name>", "role": "admin"|"viewer", "hash": "<hash>"}
to the GRIDPRINT_USERS list. Login names are case-insensitive; passwords are not.

Security model
  * Every page, API, data file and script needs a valid session, except: the login page,
    POST /login, and POST /ingest (stations, which authenticate with their X-Token).
  * Passwords: PBKDF2-HMAC-SHA256, 310 000 iterations, per-user salt. Never stored in plain text.
  * Sessions: 256-bit random token in an HttpOnly, Secure, SameSite=Strict cookie scoped to /gridprint.
    Only a SHA-256 of the token is kept on disk. Viewer sessions last 12 h (idle); admins can tick
    "remember me" for 30 days. Admins can see and revoke sessions.
  * Brute force: 5 failed logins per account or 20 per IP in 15 min locks further attempts for 15 min.
  * CSRF: SameSite=Strict + Origin check on every POST + per-session CSRF token on admin actions.
  * Headers: strict Content-Security-Policy (no inline scripts), HSTS, no framing, no sniffing,
    no referrer, no caching of private data.
"""
import base64
import bisect
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from array import array
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("PORT", "8080"))
TOKEN = os.environ.get("GRIDPRINT_TOKEN", "")
DATA = os.path.expanduser(os.environ.get("GRIDPRINT_DATA", "~/gridprint-data"))
AUTH_DIR = os.path.expanduser(os.environ.get("GRIDPRINT_AUTH", "~/gridprint-auth"))
SECURE_COOKIES = os.environ.get("GRIDPRINT_INSECURE_COOKIES") != "1"     # "1" only for local http testing
PREFIX = "/gridprint"
HEADER = "unix_time,frequency_hz,pulses,avg_pulse_width_us,status,d2_low_pct\n"   # d2_low_pct: optional 6th column (newer firmware)
STATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
STATUS = {"OK": 0, "GLITCH": 1, "OUTAGE": 2}
STATUS_NAME = ["OK", "GLITCH", "OUTAGE"]
MAX_SPAN = 31 * 86400
HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE = "gp_session"
VIEWER_TTL = 12 * 3600          # idle timeout for normal sessions
REMEMBER_TTL = 30 * 86400       # absolute lifetime for "remember me" (admins only)
PBKDF2_ITER = 310_000
UPLOT_CDN = "https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/"
UPLOT_JS_SRI = "sha384-XMP23k7YSCFr13Xrkxh4IO0v1W+8fB6AP+3Vycxphp2Z0V2vocAZHyuGaFCysrtM"
UPLOT_CSS_SRI = "sha384-IfV0B7MIOYuO95kO9G5ySKPz/85zqFNOAs8iy4tkK5zd9izhJAB8b7lHrwYqqmYE"

lock = threading.RLock()
last_seen = {}                                   # station -> unix time of newest reading


# ================================================================== storage
def day_name(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")


def day_path(station, day):
    return os.path.join(DATA, station, day + ".csv")


def parse_line(line):
    p = line.strip().split(",")
    if len(p) not in (5, 6):
        return None
    try:
        t = float(p[0]); f = float(p[1]); n = int(p[2]); w = float(p[3])
        d2 = int(p[5]) if len(p) == 6 else 255          # % of the second pin D2 was LOW; 255 = not reported
    except ValueError:
        return None
    if not (1.6e9 < t < 4e9) or p[4] not in STATUS or not math.isfinite(f) or not math.isfinite(w):
        return None
    if not (0 <= d2 <= 100 or d2 == 255):
        return None
    return t, f, n, w, STATUS[p[4]], d2


def store(station, body):
    rows = sorted(r for r in (parse_line(x) for x in body.splitlines()) if r)
    by_day = {}
    for r in rows:
        by_day.setdefault(day_name(r[0]), []).append(r)
    with lock:
        for day, rs in by_day.items():
            path = day_path(station, day)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            new = not os.path.exists(path)
            with open(path, "a") as fh:
                if new:
                    fh.write(HEADER)
                for t, f, n, w, s, d2 in rs:
                    fh.write(f"{t:.3f},{f:.4f},{n},{w:.0f},{STATUS_NAME[s]}" + (f",{d2}\n" if d2 != 255 else "\n"))
        if rows:
            last_seen[station] = max(last_seen.get(station, 0), rows[-1][0])
    return len(rows)


class Day:
    """One day file, parsed into compact arrays and kept up to date incrementally."""
    __slots__ = ("path", "offset", "t", "f", "s", "p")

    def __init__(self, path):
        self.path = path; self.offset = 0
        self.t = array("d"); self.f = array("f"); self.s = array("B"); self.p = array("B")

    def refresh(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.offset:
            self.__init__(self.path)
        if size == self.offset:
            return
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            chunk = fh.read(size - self.offset)
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        self.offset += end + 1
        unsorted = False
        for line in chunk[:end].decode("utf-8", "ignore").splitlines():
            r = parse_line(line)
            if r:
                if self.t and r[0] < self.t[-1]:
                    unsorted = True
                self.t.append(r[0]); self.f.append(r[1]); self.s.append(r[4]); self.p.append(r[5])
        if unsorted:
            order = sorted(range(len(self.t)), key=self.t.__getitem__)
            self.t = array("d", (self.t[i] for i in order))
            self.f = array("f", (self.f[i] for i in order))
            self.s = array("B", (self.s[i] for i in order))
            self.p = array("B", (self.p[i] for i in order))


_days = OrderedDict()


def get_day(station, day):
    path = day_path(station, day)
    if not os.path.exists(path):
        return None
    with lock:
        d = _days.get(path)
        if d is None:
            d = _days[path] = Day(path)
        _days.move_to_end(path)
        while len(_days) > 10:
            _days.popitem(last=False)
        d.refresh()
        return d


def days_between(t0, t1):
    d = datetime.fromtimestamp(t0, tz=timezone.utc).date()
    end = datetime.fromtimestamp(t1, tz=timezone.utc).date()
    while d <= end:
        yield d.strftime("%Y-%m-%d")
        d += timedelta(days=1)


def select(station, t0, t1):
    for day in days_between(t0, t1):
        d = get_day(station, day)
        if not d or not d.t:
            continue
        i = bisect.bisect_left(d.t, t0)
        j = bisect.bisect_right(d.t, t1)
        t, f, s, pp = d.t, d.f, d.s, d.p
        for k in range(i, j):
            yield t[k], f[k], s[k], pp[k]


def scan_last_seen():
    if not os.path.isdir(DATA):
        return
    for station in os.listdir(DATA):
        folder = os.path.join(DATA, station)
        if not os.path.isdir(folder) or not STATION_RE.match(station):
            continue
        files = sorted(x for x in os.listdir(folder) if x.endswith(".csv"))
        if files:
            d = get_day(station, files[-1][:-4])
            if d and d.t:
                last_seen[station] = d.t[-1]


# ================================================================== queries
def range_query(station, t0, t1, points):
    points = max(20, min(6000, points))
    bucket = max(1, math.ceil((t1 - t0) / points))
    T, MEAN, MIN, MAX, OUT, GL, D2 = [], [], [], [], [], [], []
    ok_n = 0; ok_sum = 0.0; ok_sq = 0.0; lo = hi = None; lo_t = hi_t = None
    total = 0; outage_s = 0; glitches = 0
    events = []; ev_start = ev_last = None; ev_d2 = []
    cur = None; b_sum = 0.0; b_n = 0; b_lo = b_hi = None; b_out = b_gl = b_cnt = 0; b_d2 = []

    def flush():
        T.append(cur)
        if b_n:
            MEAN.append(round(b_sum / b_n, 4)); MIN.append(round(b_lo, 4)); MAX.append(round(b_hi, 4))
        else:
            MEAN.append(None); MIN.append(None); MAX.append(None)
        OUT.append(round(b_out / b_cnt, 3) if b_cnt else 0)
        GL.append(b_gl)
        D2.append(round(sum(b_d2) / len(b_d2)) if b_d2 else None)     # D2 low % during non-OK seconds (sensor diagnosis)

    def ev_close():
        events.append((ev_start, ev_last, round(sum(ev_d2) / len(ev_d2)) if ev_d2 else None))

    for t, f, s, d2 in select(station, t0, t1):
        total += 1
        if s == 0:
            ok_n += 1; ok_sum += f; ok_sq += f * f
            if lo is None or f < lo: lo, lo_t = f, t
            if hi is None or f > hi: hi, hi_t = f, t
        elif s == 2:
            outage_s += 1
            if ev_start is None or t - ev_last > 3:
                if ev_start is not None:
                    ev_close()
                ev_start = t; ev_d2 = []
            ev_last = t
            if d2 != 255:
                ev_d2.append(d2)
        else:
            glitches += 1
        key = t0 + ((t - t0) // bucket) * bucket if bucket > 1 else round(t, 3)
        if key != cur:
            if cur is not None:
                flush()
            cur = key; b_sum = 0.0; b_n = 0; b_lo = b_hi = None; b_out = b_gl = b_cnt = 0; b_d2 = []
        b_cnt += 1
        if s == 0:
            b_sum += f; b_n += 1
            if b_lo is None or f < b_lo: b_lo = f
            if b_hi is None or f > b_hi: b_hi = f
        elif s == 2:
            b_out += 1
        else:
            b_gl += 1
        if s != 0 and d2 != 255:
            b_d2.append(d2)
    if cur is not None:
        flush()
    if ev_start is not None:
        ev_close()
    mean = ok_sum / ok_n if ok_n else None
    std = math.sqrt(max(0.0, ok_sq / ok_n - mean * mean)) if ok_n else None
    return {
        "station": station, "from": t0, "to": t1, "bucket": bucket,
        "t": T, "mean": MEAN, "min": MIN, "max": MAX, "out": OUT, "gl": GL, "d2": D2,
        "stats": {"readings": total, "ok": ok_n, "mean": mean, "std": std, "min": lo, "max": hi,
                  "min_t": lo_t, "max_t": hi_t, "outage_s": outage_s, "glitches": glitches,
                  "coverage": round(total / max(1.0, t1 - t0), 4)},
        "events": [[round(a, 1), round(b, 1), c] for a, b, c in events[-300:]],
        "last_seen": last_seen.get(station),
    }


def export_csv(station, t0, t1):
    out = [HEADER]
    for day in days_between(t0, t1):
        path = day_path(station, day)
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            for line in fh:
                try:
                    t = float(line.split(",", 1)[0])
                except ValueError:
                    continue
                if t0 <= t <= t1:
                    out.append(line if line.endswith("\n") else line + "\n")
    return "".join(out).encode()


# ================================================================== accounts & sessions
def norm_user(u):
    return " ".join((u or "").split()).casefold()[:64]


def hash_password(pw, salt=None, it=PBKDF2_ITER):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, it)
    return f"pbkdf2_sha256${it}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(pw, stored):
    try:
        algo, it, salt, dk = stored.split("$")
        it = int(it)
        if algo != "pbkdf2_sha256" or not 100_000 <= it <= 5_000_000:
            return False
        cand = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt), it)
        return hmac.compare_digest(cand, base64.b64decode(dk))
    except Exception:
        return False


_DUMMY_HASH = hash_password(secrets.token_hex(8))   # equal timing for unknown usernames


def load_users():
    try:
        raw = json.loads(os.environ.get("GRIDPRINT_USERS", "[]"))
    except ValueError:
        raw = []
    users = {}
    for u in raw:
        if u.get("role") in ("admin", "viewer") and u.get("hash") and u.get("user"):
            users[norm_user(u["user"])] = {"name": u.get("name") or u["user"], "role": u["role"], "hash": u["hash"]}
    return users


USERS = {}


def _sha(x):
    return hashlib.sha256(x.encode()).hexdigest()


def _auth_path(name):
    return os.path.join(AUTH_DIR, name)


def _read_json(name, default):
    try:
        with open(_auth_path(name)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(name, obj):
    os.makedirs(AUTH_DIR, mode=0o700, exist_ok=True)
    tmp = _auth_path(name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh)
    os.replace(tmp, _auth_path(name))


def sessions_load():
    s = _read_json("sessions.json", {})
    now = time.time()
    return {k: v for k, v in s.items() if v.get("exp", 0) > now and norm_user(v.get("user")) in USERS}


def session_get(token):
    if not token or len(token) > 100:
        return None
    key = _sha(token)
    with lock:
        s = sessions_load()
        sess = s.get(key)
        if not sess:
            return None
        u = USERS[norm_user(sess["user"])]
        now = time.time()
        if not sess.get("remember") and now - sess.get("seen", 0) > 300:   # slide the idle timeout
            sess["exp"] = now + VIEWER_TTL; sess["seen"] = now
            s[key] = sess; _write_json("sessions.json", s)
        out = dict(sess)
        out.update(role=u["role"], name=u["name"], id=key[:12])            # role changes apply at once
        return out


def session_create(user, remember, ip, ua):
    token = secrets.token_urlsafe(32)
    now = time.time()
    sess = {"user": user, "created": now, "seen": now, "remember": remember,
            "exp": now + (REMEMBER_TTL if remember else VIEWER_TTL),
            "csrf": secrets.token_urlsafe(24), "ip": ip[:45], "ua": ua[:120]}
    with lock:
        s = sessions_load(); s[_sha(token)] = sess; _write_json("sessions.json", s)
    return token


def session_delete(token=None, prefix=None, everyone=False):
    with lock:
        s = sessions_load()
        for k in list(s):
            if everyone or (token and k == _sha(token)) or (prefix and k.startswith(prefix)):
                del s[k]
        _write_json("sessions.json", s)


# ---- login throttling
_fails = {}
FAIL_WINDOW = 900


def _recent(key):
    now = time.time()
    lst = [t for t in _fails.get(key, []) if now - t < FAIL_WINDOW]
    _fails[key] = lst
    return lst


def login_locked(user, ip):
    with lock:
        return len(_recent("u:" + user)) >= 5 or len(_recent("ip:" + ip)) >= 20


def login_failed(user, ip):
    with lock:
        now = time.time()
        _fails["u:" + user] = (_recent("u:" + user) + [now])[-20:]
        _fails["ip:" + ip] = (_recent("ip:" + ip) + [now])[-50:]


def login_ok(user):
    with lock:
        _fails.pop("u:" + user, None)


# ---- settings (Gemini key etc.), stored outside the web root with 0600 permissions
def settings_get():
    return _read_json("settings.json", {})


def settings_set(**kw):
    with lock:
        s = settings_get(); s.update({k: v for k, v in kw.items() if v is not None}); _write_json("settings.json", s)


# ================================================================== Gemini report
def sensor_diag(pct):
    """Explain an outage from how much of the time pin D2 was LOW (newer firmware reports it)."""
    if pct is None:
        return "unknown (older firmware)"
    if pct >= 90:
        return "D2 stuck LOW: fault on the sensor output side (PC817 or pull-up wiring), probably not a real power cut"
    if pct <= 10:
        return "D2 stuck HIGH: no mains signal reached the sensor (real power cut, or transformer side unplugged)"
    return "D2 flickering: loose connection or electrical noise"



ISB_LAT, ISB_LON = 33.6844, 73.0479


def sun_times_pkt(day):
    """Approximate sunrise/sunset in Islamabad (PKT, HH:MM) for a date, NOAA formula, good to ~2 min."""
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    dec = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
           + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    lat = math.radians(ISB_LAT)
    ha = math.degrees(math.acos(math.cos(math.radians(90.833)) / (math.cos(lat) * math.cos(dec)) - math.tan(lat) * math.tan(dec)))
    fmt = lambda m: f"{int(m // 60) % 24:02d}:{int(m % 60):02d}"
    return fmt(720 - 4 * (ISB_LON + ha) - eqt + 300), fmt(720 - 4 * (ISB_LON - ha) - eqt + 300)


def grid_features(station, t0, t1):
    """Shape analysis for the AI narrative: turning points, the moves between them, steepest minutes,
    time in each frequency band, repeating cycles and hourly averages. All numbers come from the data."""
    span = max(60, t1 - t0)
    r = range_query(station, t0, t1, int(min(2000, max(60, span / 10))))
    b = r["bucket"]
    pts = [(t, m) for t, m in zip(r["t"], r["mean"]) if m is not None]
    pk = lambda t: datetime.fromtimestamp(t, tz=timezone(timedelta(hours=5))).strftime("%m-%d %H:%M")
    out = {"analysis_bucket_seconds": b}
    if len(pts) < 5:
        return out
    w = max(1, round(60 / b))                                   # ~1-minute smoothing
    sm = []
    for i in range(len(pts)):
        seg = pts[max(0, i - w // 2): i + w // 2 + 1]
        sm.append((pts[i][0], sum(x[1] for x in seg) / len(seg)))
    vals = [v for _, v in sm]
    mean = sum(vals) / len(vals)
    std = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
    # zig-zag turning points: a peak/trough is confirmed once the line reverses by at least `th`
    th = max(0.05, 0.6 * std)
    turns, ext_i, direction = [], 0, 0
    for i in range(1, len(sm)):
        v, e = sm[i][1], sm[ext_i][1]
        if direction >= 0 and v > e or direction <= 0 and v < e:
            if direction == 0:
                direction = 1 if v > e else -1
            ext_i = i
        elif abs(v - e) >= th:
            turns.append((ext_i, "peak" if direction > 0 else "trough"))
            direction, ext_i = -direction, i
    turns = [(0, "start")] + turns + [(len(sm) - 1, "end")]
    moves = []
    for (i, k1), (j, k2) in zip(turns, turns[1:]):
        dt = (sm[j][0] - sm[i][0]) / 60
        if dt <= 0:
            continue
        dv = sm[j][1] - sm[i][1]
        st = datetime.fromtimestamp(sm[i][0], tz=timezone(timedelta(hours=5)))
        moves.append({"from": pk(sm[i][0]), "to": pk(sm[j][0]), "from_hz": round(sm[i][1], 3), "to_hz": round(sm[j][1], 3),
                      "change_hz": round(dv, 3), "minutes": round(dt, 1), "slope_mhz_per_min": round(dv * 1000 / dt, 1),
                      "starts_near_hour_or_half_hour": st.minute in (58, 59, 0, 1, 2, 28, 29, 30, 31, 32)})
    # steepest one-minute changes (rate of change of frequency), spaced at least 5 minutes apart
    lag = max(1, round(60 / b))
    steps = sorted(((sm[i][1] - sm[i - lag][1], i) for i in range(lag, len(sm))), key=lambda x: -abs(x[0]))
    steep, used = [], []
    for dv, i in steps:
        if all(abs(sm[i][0] - sm[u][0]) > 300 for u in used):
            used.append(i)
            steep.append({"at": pk(sm[i][0]), "change_hz_in_1min": round(dv, 3), "from_hz": round(sm[i - lag][1], 3), "to_hz": round(sm[i][1], 3)})
        if len(steep) >= 8:
            break
    # time spent in each band around 50 Hz
    dev = [abs(v - 50) for _, v in pts]
    bands = {"within_0.05": 0, "0.05_to_0.2": 0, "0.2_to_0.5": 0, "beyond_0.5": 0}
    for d in dev:
        bands["within_0.05" if d <= 0.05 else "0.05_to_0.2" if d <= 0.2 else "0.2_to_0.5" if d <= 0.5 else "beyond_0.5"] += 1
    bands = {k: round(100 * v / len(dev), 1) for k, v in bands.items()}
    # repeating cycles: autocorrelation of the detrended line between 3 and 90 minutes
    cycle = None
    trend_w = max(3, round(1800 / b))
    det = []
    for i in range(len(vals)):
        seg = vals[max(0, i - trend_w // 2): i + trend_w // 2 + 1]
        det.append(vals[i] - sum(seg) / len(seg))
    var = sum(x * x for x in det)
    if var > 0 and span >= 3600:
        best = None
        for L in range(max(1, round(180 / b)), min(len(det) // 2, round(5400 / b)) + 1):
            c = sum(det[i] * det[i - L] for i in range(L, len(det))) / var
            if best is None or c > best[0]:
                best = (c, L)
        if best and best[0] > 0.3:
            cycle = {"period_minutes": round(best[1] * b / 60, 1), "strength_0_to_1": round(best[0], 2)}
    hourly = {}
    for t, v in pts:
        h = datetime.fromtimestamp(t, tz=timezone(timedelta(hours=5))).strftime("%m-%d %H:00")
        hourly.setdefault(h, []).append(v)
    days = sorted({datetime.fromtimestamp(t, tz=timezone(timedelta(hours=5))).date() for t, _ in pts})
    if len(moves) > 60:                                         # long ranges: keep the 60 biggest moves, in time order
        keep = sorted(sorted(range(len(moves)), key=lambda i: -abs(moves[i]["change_hz"]))[:60])
        moves = [moves[i] for i in keep]
    tp = [{"at": pk(sm[i][0]), "hz": round(sm[i][1], 3), "kind": k} for i, k in turns]
    if len(tp) > 61:
        tp = [x for x in tp if x["at"] in {m["from"] for m in moves} | {m["to"] for m in moves}]
    out.update({
        "turning_points": tp,
        "moves_between_turning_points": moves,
        "steepest_one_minute_changes": steep,
        "percent_time_in_band": bands,
        "repeating_cycle": cycle,
        "hourly_mean_hz": {h: round(sum(a) / len(a), 3) for h, a in list(hourly.items())[-48:]},
        "islamabad_sun_pkt": {str(d): dict(zip(("sunrise", "sunset"), sun_times_pkt(d))) for d in days[-8:]},
    })
    return out


def build_digest(station, t0, t1):
    r = range_query(station, t0, t1, 96)
    s = r["stats"]
    pk = lambda t: datetime.fromtimestamp(t, tz=timezone(timedelta(hours=5))).strftime("%Y-%m-%d %H:%M:%S")
    series = [[pk(t)[5:16], m, lo, hi, o] for t, m, lo, hi, o in zip(r["t"], r["mean"], r["min"], r["max"], r["out"])]
    ramps, prev = [], None
    for row in series:
        if prev and row[1] is not None and prev[1] is not None and abs(row[1] - prev[1]) >= 0.05:
            ramps.append({"at": row[0], "from": prev[1], "to": row[1]})
        prev = row
    return {
        "station": station, "location": "Islamabad, Pakistan (NUST)", "nominal_hz": 50.0,
        "range_pkt": [pk(t0), pk(t1)], "bucket_seconds": r["bucket"],
        "stats": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in s.items() if k not in ("min_t", "max_t")},
        "lowest_at": pk(s["min_t"]) if s["min_t"] else None, "highest_at": pk(s["max_t"]) if s["max_t"] else None,
        "outages_pkt": [[pk(a), round(b - a + 1), sensor_diag(c)] for a, b, c in r["events"]][-40:],
        "big_steps_between_buckets": ramps[:30],
        "series_columns": ["time_pkt(MM-DD HH:MM)", "mean_hz", "min_hz", "max_hz", "outage_fraction"],
        "series": series,
        "shape_analysis": grid_features(station, t0, t1),
    }


REPORT_PROMPT = """You are a power-systems analyst writing for the GridPrint project, which measures the
Pakistani national grid's frequency (nominal 50.00 Hz) once per second from a mains socket in Islamabad.
The reader is a curious student who wants to SEE the story behind the graph: why each rise, dip and
slope happened and how one event led to the next. Use ONLY the data below for numbers and times.

How to read grid frequency (use this physics in your explanations):
- Frequency is the live balance of supply and demand on the whole interconnected grid. Above 50 Hz =
  more generation than load (surplus); below 50 Hz = load exceeds generation (deficit). Every socket in
  the country sees the same frequency.
- Gentle drifts (under ~5 mHz/min) usually mean demand slowly changing. Steady ramps (~10-40 mHz/min)
  mean a sustained mismatch: large blocks of load switching, or plants ramping up/down too slowly to
  keep up. Sharp drops (over ~100 mHz in a minute) usually mean a sudden loss of generation (a unit or
  line tripping). Sharp rises mean a big load suddenly disappearing (for example feeders being cut for
  load-shedding) or generation being added too fast.
- After a disturbance, turbine governors react within seconds (primary control) and the control centre
  (NPCC) re-dispatches plants over minutes (secondary control). That often gives a V or W shape and an
  overshoot past 50 Hz. A regular repeating cycle suggests control "hunting" or scheduled switching.
- Moves that start near the top of the hour or half hour (flag starts_near_hour_or_half_hour) fit
  scheduled feeder switching and load-shedding blocks, which in Pakistan often change on the hour.
- Daily rhythm: demand ramps up in the morning, rooftop solar (very widespread in Pakistan) cuts the
  grid's daytime load, and after sunset (see islamabad_sun_pkt) lighting, heating and the loss of
  solar push the evening peak; late night demand falls.

Write Markdown with these sections:
## Summary
3-4 sentences: overall state, average frequency, how far it strayed, outages.
## The story of the grid
The heart of the report. A chronological narrative in short paragraphs (one per phase), built from
shape_analysis.moves_between_turning_points, turning_points and steepest_one_minute_changes. For each
phase say what the line did (times, Hz, slope in mHz/min), what most likely caused it, and how it led
into the next phase ("this deficit pulled frequency down until reserves came online, which caused the
overshoot at..."). Tie phases to time of day, sunrise/sunset and hour boundaries where they fit. Tag
every cause with (likely), (possible) or (speculative) and never present a cause as certain.
## Reading the shapes
3-6 bullets explaining the patterns a reader can spot on the graph in this range (steepest changes,
any repeating_cycle, V/W shapes, overshoots), what each pattern means physically, and the time to zoom
into to see it.
## Timeline of notable events
A Markdown table: Time (PKT) | What the line did | Likely cause | Confidence. 5-12 rows.
## Grid stability assessment
Use percent_time_in_band: +/-0.05 Hz tight, +/-0.2 Hz normal band, beyond +/-0.5 Hz stressed.
## Caveats
Single station. Outages carry a sensor diagnosis: "D2 stuck HIGH" is a likely real power cut; "stuck
LOW" or "flickering" are hardware faults, not grid events, so leave them out of the story. Mention data
gaps. Causes are inferences from one frequency signal.

Be specific with numbers and times, do not invent data, about 600-900 words. Use plain text symbols
such as \u00b1 and never LaTeX or $...$.

DATA (JSON):
"""


FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-flash-lite-latest", "gemini-2.5-flash-lite"]


def gemini_report(station, t0, t1):
    st = settings_get()
    key = st.get("gemini_key")
    model = st.get("gemini_model") or "gemini-flash-latest"
    if not key:
        return None, "No Gemini API key set yet. An admin can add one under Admin > Gemini."
    if not re.match(r"^[A-Za-z0-9._-]{1,64}$", model):
        return None, "Invalid model name in settings."
    digest = build_digest(station, t0, t1)
    if not digest["stats"]["readings"]:
        return None, "There are no readings in this range."
    body = json.dumps({"contents": [{"role": "user", "parts": [{"text": REPORT_PROMPT + json.dumps(digest, separators=(",", ":"))}]}],
                       "generationConfig": {"temperature": 0.4, "maxOutputTokens": 8192}}).encode()
    text, used, err = gemini_call(key, body, [model] + [m for m in FALLBACK_MODELS if m != model])
    if text and used != model:
        text += f"\n\n_(Written by {used}; {model} was busy.)_"
    return text, err


def gemini_call(key, body, models, timeout=60):
    """POST to Gemini, retrying busy errors once and falling back through `models`. Returns (text, model, error)."""
    last_err = "Gemini did not answer."
    for m in models:
        for attempt in range(2):
            req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                                         data=body, method="POST",
                                         headers={"Content-Type": "application/json", "x-goog-api-key": key})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    out = json.loads(resp.read())
                text = "".join(p.get("text", "") for p in out["candidates"][0]["content"]["parts"]).strip()
                if text:
                    return text, m, None
                last_err = "Gemini returned an empty answer."
                break
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = json.loads(e.read()).get("error", {}).get("message", "")[:200]
                except Exception:
                    pass
                last_err = f"Gemini returned an error ({e.code}). {detail}"
                if e.code in (400, 401, 403):
                    return None, m, last_err                  # bad key / request: retrying won't help
                if e.code == 404:
                    break                                     # model not available: try the next one
                if e.code in (429, 500, 502, 503, 504) and attempt == 0:
                    time.sleep(2)
                    continue
                break
            except Exception as e:
                last_err = f"Could not reach Gemini ({type(e).__name__})."
                break
    return None, None, last_err + " All fallback models were busy too; try again in a minute."


# ================================================================== live insight (the orb)
INSIGHT_WINDOW = 1800          # look back 30 minutes
INSIGHT_AI_EVERY = 600         # refresh the AI sentence at most every 10 minutes per station
_insight_ai = {}               # station -> {"text", "at", "busy"}
INSIGHT_PROMPT = """You watch Pakistan's national grid frequency (nominal 50.00 Hz) from one socket in Islamabad.
Below 50 Hz means demand exceeds generation; above 50 Hz means surplus. Sharp drops suggest a generator or
line tripping; sharp rises suggest a big load dropping off (often load-shedding blocks, which switch on the
hour) or generation added too fast; slow ramps mean demand changing faster than plants can follow.
In 2 short sentences (max 45 words total), tell a student what the grid has been doing in the last 30 minutes
and the most likely cause of the biggest change. Mention one PKT time. Hedge causes with "likely" or
"possibly". Plain text only, no Markdown, no LaTeX.
DATA (JSON):
"""


def _classify(move):
    sl, up = abs(move["slope_mhz_per_min"]), move["change_hz"] > 0
    speed = "sharp" if sl >= 100 else "fast" if sl >= 40 else "steady" if sl >= 10 else "gentle"
    if speed in ("sharp", "fast"):
        cause = ("a big load dropping off (often a load-shedding block) or generation added too fast" if up
                 else "a generator or line tripping, or a big load switching on")
    elif speed == "steady":
        cause = "demand easing faster than plants ramp down" if up else "demand rising faster than plants can follow"
    else:
        cause = "slow drift in demand"
    if move.get("starts_near_hour_or_half_hour") and speed != "gentle":
        cause += "; it starts on the hour/half hour, which fits scheduled feeder switching"
    return speed, cause


def live_insight(station):
    now = time.time()
    t0 = now - INSIGHT_WINDOW
    cur = range_query(station, now - 300, now, 30)
    pts = [(t, m) for t, m in zip(cur["t"], cur["mean"]) if m is not None]
    res = {"station": station, "updated": now, "mood": "unknown", "headline": "No recent readings", "bullets": [], "ai": None}
    if len(pts) < 3 or (last_seen.get(station) or 0) < now - 120:
        res["headline"] = "Station offline" if pts == [] or (last_seen.get(station) or 0) < now - 120 else res["headline"]
        return res
    f_now = sum(m for _, m in pts[-3:]) / len(pts[-3:])
    slope5 = (pts[-1][1] - pts[0][1]) * 1000 / max(1, (pts[-1][0] - pts[0][0]) / 60)
    feat = grid_features(station, t0, now)
    moves = feat.get("moves_between_turning_points", [])
    bands = feat.get("percent_time_in_band", {})
    beyond02 = bands.get("0.2_to_0.5", 0) + bands.get("beyond_0.5", 0)
    big = max(moves, key=lambda m: abs(m["change_hz"])) if moves else None
    state = "surplus (above 50 Hz)" if f_now > 50.02 else "deficit (below 50 Hz)" if f_now < 49.98 else "balanced"
    trend = "rising" if slope5 > 8 else "falling" if slope5 < -8 else "holding steady"
    res["headline"] = f"{f_now:.3f} Hz, {trend} ({slope5:+.0f} mHz/min), {state}"
    if big and abs(big["slope_mhz_per_min"]) >= 40 or bands.get("beyond_0.5", 0) > 0 or beyond02 > 50:
        res["mood"] = "stressed" if bands.get("beyond_0.5", 0) > 0 or beyond02 > 50 else "active"
    else:
        res["mood"] = "calm"
    b = []
    if big:
        speed, cause = _classify(big)
        b.append({"text": f"Biggest move: a {speed} {'rise' if big['change_hz'] > 0 else 'fall'} "
                          f"of {abs(big['change_hz']):.2f} Hz from {big['from'][6:]} to {big['to'][6:]} "
                          f"({big['slope_mhz_per_min']:+.0f} mHz/min). Possible cause: {cause}.", "at": big["from"][6:]})
    st = feat.get("steepest_one_minute_changes") or []
    if st and (not big or st[0]["at"][6:] != big["from"][6:]):
        x = st[0]
        b.append({"text": f"Steepest minute: {x['change_hz_in_1min']:+.2f} Hz around {x['at'][6:]}.", "at": x["at"][6:]})
    if feat.get("repeating_cycle"):
        b.append({"text": f"Swinging in a repeating ~{feat['repeating_cycle']['period_minutes']:.0f}-minute cycle, "
                          "typical of control 'hunting' or scheduled switching.", "at": None})
    if bands:
        b.append({"text": f"Last 30 min: {bands.get('within_0.05', 0) + bands.get('0.05_to_0.2', 0):.0f}% inside the normal "
                          f"\u00b10.2 Hz band, {beyond02:.0f}% outside it.", "at": None})
    res["bullets"] = b[:4]
    # AI sentence: cached, refreshed at most every INSIGHT_AI_EVERY seconds, only if a key is set
    key = settings_get().get("gemini_key")
    c = _insight_ai.get(station) or {}
    if key and not c.get("busy") and now - c.get("at", 0) > INSIGHT_AI_EVERY:
        _insight_ai[station] = dict(c, busy=True)
        try:
            data = {"now_pkt": datetime.fromtimestamp(now, tz=timezone(timedelta(hours=5))).strftime("%H:%M"),
                    "current_hz": round(f_now, 3), "slope_last_5min_mhz_per_min": round(slope5),
                    **{k: feat.get(k) for k in ("moves_between_turning_points", "steepest_one_minute_changes",
                                                "percent_time_in_band", "repeating_cycle", "islamabad_sun_pkt")}}
            body = json.dumps({"contents": [{"role": "user", "parts": [{"text": INSIGHT_PROMPT + json.dumps(data, separators=(",", ":"))}]}],
                               "generationConfig": {"temperature": 0.4, "maxOutputTokens": 400}}).encode()
            model = settings_get().get("gemini_model") or "gemini-flash-latest"
            order = ["gemini-flash-lite-latest", model, "gemini-2.5-flash-lite", "gemini-2.5-flash"]
            text, used, err = gemini_call(key, body, list(dict.fromkeys(order)), timeout=25)
            _insight_ai[station] = {"text": (text or "").replace("*", "").strip()[:400] if text else c.get("text"),
                                    "at": now if text else c.get("at", 0), "busy": False, "retry": now}
            if not text:                                     # busy: try again in 2 minutes instead of 10
                _insight_ai[station]["at"] = c.get("at", 0) if c.get("text") else now - INSIGHT_AI_EVERY + 120
        except Exception:
            _insight_ai[station] = dict(c, busy=False)
    c = _insight_ai.get(station) or {}
    if c.get("text"):
        res["ai"] = {"text": c["text"], "age_s": round(now - c.get("at", now))}
    return res


# ================================================================== HTTP plumbing
SEC_HEADERS = [
    ("Content-Security-Policy", "default-src 'none'; script-src 'self' https://cdn.jsdelivr.net; "
     "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:; connect-src 'self'; "
     "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
    ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("X-Robots-Tag", "noindex, nofollow"),
    ("Cache-Control", "no-store"),
]


class Resp:
    def __init__(self, code=200, body=b"", ctype="text/plain; charset=utf-8", headers=None):
        self.code = code; self.body = body if isinstance(body, bytes) else body.encode()
        self.ctype = ctype; self.headers = list(headers or [])


def J(obj, code=200):
    return Resp(code, json.dumps(obj, separators=(",", ":")), "application/json")


def redirect(to, headers=None):
    return Resp(303, b"", "text/plain", [("Location", to)] + list(headers or []))


def cookie_header(token, max_age=None):
    parts = [f"{COOKIE}={token}", f"Path={PREFIX}", "HttpOnly", "SameSite=Strict"]
    if SECURE_COOKIES:
        parts.append("Secure")
    if max_age is not None:
        parts.append(f"Max-Age={int(max_age)}")
    return ("Set-Cookie", "; ".join(parts))


def num(q, key, default):
    try:
        v = float(q.get(key, [default])[0])
        return v if math.isfinite(v) else float(default)
    except (TypeError, ValueError):
        return float(default)


def same_origin(req):
    host = req["host"]
    allowed = {f"https://{host}"} | (set() if SECURE_COOKIES else {f"http://{host}"})
    origin = req["headers"].get("origin") or ""
    if origin:
        return origin in allowed
    ref = req["headers"].get("referer") or ""
    return any(ref.startswith(o + "/") for o in allowed)


# ================================================================== routing
def route(req):
    method, path, q = req["method"], req["path"], req["query"]
    if not path.startswith(PREFIX):
        return Resp(404, "Not found")

    # ---- stations (token-authenticated, no login)
    if path == PREFIX + "/ingest":
        if method != "POST":
            return Resp(405, "Method not allowed")
        if not TOKEN or not hmac.compare_digest(req["headers"].get("x-token", "").encode(), TOKEN.encode()):
            return J({"error": "bad token"}, 403)
        station = req["headers"].get("x-station", "")
        if not STATION_RE.match(station):
            return J({"error": "bad station id"}, 400)
        return J({"ok": True, "stored": store(station, req["body"].decode("utf-8", "ignore"))})

    # ---- login / logout
    if path == PREFIX + "/login":
        if method == "GET":
            if session_get(req["cookies"].get(COOKIE)):
                return redirect(PREFIX + "/")
            return Resp(200, login_page(q.get("e", [""])[0]), "text/html; charset=utf-8")
        if method != "POST" or not same_origin(req):
            return Resp(403, "Forbidden")
        form = parse_qs(req["body"].decode("utf-8", "ignore"), max_num_fields=5)
        user = norm_user(form.get("username", [""])[0])
        pw = form.get("password", [""])[0][:256]
        remember = form.get("remember", [""])[0] == "1"
        ip = req["ip"]
        if login_locked(user, ip):
            return redirect(PREFIX + "/login?e=locked")
        u = USERS.get(user)
        good = check_password(pw, u["hash"] if u else _DUMMY_HASH) and u is not None
        if not good:
            login_failed(user, ip)
            time.sleep(0.4)
            return redirect(PREFIX + "/login?e=bad")
        login_ok(user)
        remember = remember and u["role"] == "admin"
        token = session_create(user, remember, ip, req["headers"].get("user-agent", ""))
        return redirect(PREFIX + "/", [cookie_header(token, REMEMBER_TTL if remember else None)])

    sess = session_get(req["cookies"].get(COOKIE))
    if path == PREFIX + "/logout":
        if method != "POST" or not same_origin(req):
            return Resp(403, "Forbidden")
        if sess:
            session_delete(token=req["cookies"].get(COOKIE))
        return redirect(PREFIX + "/login", [cookie_header("", 0)])

    # ---- everything below needs a valid session
    if not sess:
        if method == "GET" and path in (PREFIX, PREFIX + "/"):
            return redirect(PREFIX + "/login")
        return J({"error": "login required"}, 401)
    admin = sess["role"] == "admin"

    if method == "POST":
        if not same_origin(req) or not hmac.compare_digest(req["headers"].get("x-csrf", "").encode(), sess["csrf"].encode()):
            return J({"error": "bad request origin"}, 403)
        if not admin:
            return J({"error": "admins only"}, 403)
        try:
            data = json.loads(req["body"] or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return J({"error": "bad json"}, 400)
        if path == PREFIX + "/api/report":
            station = str(data.get("station", ""))
            if not STATION_RE.match(station):
                return J({"error": "bad station"}, 400)
            try:
                t0, t1 = float(data.get("from", 0)), float(data.get("to", 0))
            except (TypeError, ValueError):
                return J({"error": "bad range"}, 400)
            if not (math.isfinite(t0) and math.isfinite(t1)) or t1 <= t0:
                return J({"error": "bad range"}, 400)
            text, err = gemini_report(station, max(t0, t1 - MAX_SPAN), t1)
            return J({"report": text}) if text else J({"error": err}, 502)
        if path == PREFIX + "/api/admin/settings":
            key = data.get("gemini_key"); model = data.get("gemini_model")
            if key is not None:
                key = str(key).strip()
            if key is not None and not re.match(r"^[A-Za-z0-9._\-]{20,200}$", key):
                return J({"error": "That does not look like a Gemini API key."}, 400)
            if model is not None and not re.match(r"^[A-Za-z0-9._-]{1,64}$", str(model)):
                return J({"error": "Invalid model name."}, 400)
            settings_set(gemini_key=key, gemini_model=model)
            return J({"ok": True})
        if path == PREFIX + "/api/admin/revoke":
            if data.get("all") is True:
                session_delete(everyone=True)
            elif re.match(r"^[0-9a-f]{12}$", str(data.get("id", ""))):
                session_delete(prefix=data["id"])
            return J({"ok": True})
        return J({"error": "not found"}, 404)

    if method != "GET":
        return Resp(405, "Method not allowed")
    if path in (PREFIX, PREFIX + "/"):
        return Resp(200, APP_PAGE, "text/html; charset=utf-8")
    if path == PREFIX + "/static/app.js":
        return Resp(200, APP_JS, "application/javascript; charset=utf-8")
    if path == PREFIX + "/static/uplot.js" and UPLOT_JS:
        return Resp(200, UPLOT_JS, "application/javascript")
    if path == PREFIX + "/static/uplot.css" and UPLOT_CSS:
        return Resp(200, UPLOT_CSS, "text/css")
    if path == PREFIX + "/api/me":
        return J({"name": sess["name"], "role": sess["role"], "csrf": sess["csrf"], "remember": sess.get("remember", False)})
    if path == PREFIX + "/api/stations":
        with lock:
            return J([{"station": s, "last_seen": t} for s, t in sorted(last_seen.items())])
    station = q.get("station", [""])[0]
    if path == PREFIX + "/api/insight":
        if not STATION_RE.match(station):
            return J({"error": "bad station"}, 400)
        return J(live_insight(station))
    if path == PREFIX + "/api/range":
        if not STATION_RE.match(station):
            return J({"error": "bad station"}, 400)
        t1 = num(q, "to", time.time()); t0 = num(q, "from", t1 - 900)
        if t1 <= t0:
            return J({"error": "empty range"}, 400)
        return J(range_query(station, max(t0, t1 - MAX_SPAN), t1, int(num(q, "points", 1500))))

    # ---- admin-only reads
    if not admin:
        return J({"error": "admins only"}, 403) if path.startswith((PREFIX + "/api/", PREFIX + "/data/")) else J({"error": "not found"}, 404)
    if path == PREFIX + "/api/days":
        if not STATION_RE.match(station):
            return J({"error": "bad station"}, 400)
        folder = os.path.join(DATA, station)
        days = sorted(x[:-4] for x in os.listdir(folder) if x.endswith(".csv")) if os.path.isdir(folder) else []
        return J({"station": station, "days": days})
    if path == PREFIX + "/api/export":
        if not STATION_RE.match(station):
            return J({"error": "bad station"}, 400)
        t1 = num(q, "to", time.time()); t0 = num(q, "from", t1 - 900)
        if t1 <= t0:
            return J({"error": "empty range"}, 400)
        return Resp(200, export_csv(station, max(t0, t1 - MAX_SPAN), t1), "text/csv",
                    [("Content-Disposition", f'attachment; filename="{station}_{int(t0)}_{int(t1)}.csv"')])
    m = re.fullmatch(PREFIX + r"/data/([A-Za-z0-9_-]{1,32})/(\d{4}-\d{2}-\d{2})\.csv", path)
    if m:
        p = day_path(m.group(1), m.group(2))
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                return Resp(200, fh.read(), "text/csv", [("Content-Disposition", f'attachment; filename="{m.group(1)}_{m.group(2)}.csv"')])
        return J({"error": "not found"}, 404)
    if path == PREFIX + "/api/admin/sessions":
        with lock:
            s = sessions_load()
        me = _sha(req["cookies"].get(COOKIE, ""))
        out = [{"id": k[:12], "user": USERS.get(norm_user(v["user"]), {}).get("name", v["user"]), "created": v["created"],
                "exp": v["exp"], "remember": v.get("remember", False), "ip": v.get("ip", ""), "ua": v.get("ua", ""),
                "you": k == me} for k, v in s.items()]
        st = settings_get()
        return J({"sessions": sorted(out, key=lambda x: -x["created"]),
                  "gemini_key_set": bool(st.get("gemini_key")),
                  "gemini_key_hint": ("..." + st["gemini_key"][-4:]) if st.get("gemini_key") else "",
                  "gemini_model": st.get("gemini_model") or "gemini-flash-latest"})
    return J({"error": "not found"}, 404)


_started = False


def start():
    global _started, TOKEN, USERS
    if _started:
        return
    TOKEN = os.environ.get("GRIDPRINT_TOKEN", TOKEN)
    USERS = load_users()
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(AUTH_DIR, mode=0o700, exist_ok=True)
    scan_last_seen()
    _started = True


def parse_cookies(raw):
    out = {}
    for part in (raw or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out.setdefault(k, v)
    return out


def finish(resp):
    return [("Content-Type", resp.ctype), ("Content-Length", str(len(resp.body)))] + SEC_HEADERS + resp.headers


REASONS = {200: "OK", 303: "See Other", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
           404: "Not Found", 405: "Method Not Allowed", 500: "Internal Server Error", 502: "Bad Gateway"}


def application(environ, start_response):
    """WSGI entry point (PythonAnywhere and similar hosts)."""
    start()
    method = environ["REQUEST_METHOD"]
    hdrs = {k[5:].replace("_", "-").lower(): v for k, v in environ.items() if k.startswith("HTTP_")}
    body = b""
    if method == "POST":
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > 400_000:
            r = J({"error": "bad length"}, 400)
            start_response("400 Bad Request", finish(r)); return [r.body]
        body = environ["wsgi.input"].read(length)
    req = {"method": method, "path": environ.get("PATH_INFO", "/"),
           "query": parse_qs(environ.get("QUERY_STRING", ""), max_num_fields=20),
           "headers": hdrs, "cookies": parse_cookies(hdrs.get("cookie")), "body": body,
           "host": hdrs.get("host", ""), "ip": hdrs.get("x-real-ip") or environ.get("REMOTE_ADDR", "")}
    try:
        r = route(req)
    except Exception:
        r = J({"error": "server error"}, 500)
    start_response(f"{r.code} {REASONS.get(r.code, 'OK')}", finish(r))
    return [r.body]


class Handler(BaseHTTPRequestHandler):
    server_version = "GridPrint"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def _go(self, method):
        url = urlparse(self.path)
        body = b""
        if method == "POST":
            n = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(n) if 0 < n <= 400_000 else b""
        hdrs = {k.lower(): v for k, v in self.headers.items()}
        req = {"method": method, "path": url.path, "query": parse_qs(url.query, max_num_fields=20), "headers": hdrs,
               "cookies": parse_cookies(hdrs.get("cookie")), "body": body, "host": hdrs.get("host", ""),
               "ip": self.client_address[0]}
        try:
            r = route(req)
        except Exception:
            r = J({"error": "server error"}, 500)
        self.send_response(r.code)
        for k, v in finish(r):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(r.body)

    def do_GET(self):
        self._go("GET")

    def do_POST(self):
        self._go("POST")

    def do_PUT(self):
        self._go("PUT")

    def do_DELETE(self):
        self._go("DELETE")


def _asset(name):
    p = os.path.join(HERE, name)
    if os.path.exists(p):
        with open(p, "rb") as fh:
            return fh.read()
    return None


UPLOT_JS = _asset("uplot.min.js")
UPLOT_CSS = _asset("uplot.min.css")


# ================================================================== pages
BASE_CSS = """
:root{--bg:#0b0b0e;--panel:#141419;--line:#24242c;--ink:#f2efe6;--mut:#8d8b84;--acc:#e0592a;--ok:#2ec27e;--bad:#e5484d;--warn:#f0b429}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,"Segoe UI",sans-serif}
.k{color:var(--acc);font-weight:700;letter-spacing:.14em;font-size:11px}
.sub{color:var(--mut);font-size:12px}
button,select,input{font:inherit;color:var(--ink);background:#1c1c23;border:1px solid #2e2e38;border-radius:7px;padding:6px 11px;cursor:pointer}
input{cursor:text;color-scheme:dark}button:hover{border-color:#4a4a58}button:disabled{opacity:.5;cursor:wait}
button.on,button.pri{background:var(--acc);border-color:var(--acc);color:#111;font-weight:700}
a{color:var(--acc)}
"""


def login_page(err):
    msgs = {"bad": "Wrong username or password.", "locked": "Too many attempts. Try again in 15 minutes."}
    m = msgs.get(err, "")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>GridPrint · sign in</title><style>{BASE_CSS}
body{{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}}
form{{width:100%;max-width:360px;background:var(--panel);border:1px solid var(--line);border-top:3px solid var(--acc);border-radius:12px;padding:26px}}
h1{{margin:4px 0 18px;font-size:22px}}label{{display:block;margin:12px 0 5px;color:var(--mut);font-size:12px}}
input[type=text],input[type=password]{{width:100%;padding:10px 12px}}.row{{display:flex;align-items:center;gap:8px;margin:14px 0 18px;color:var(--mut);font-size:13px}}
.row label{{margin:0}}button{{width:100%;padding:10px}}
.err{{background:#2a1416;color:#ff9a9e;border:1px solid #5a2328;border-radius:8px;padding:8px 10px;margin-bottom:6px;font-size:13px}}
</style></head><body><form method="post" action="{PREFIX}/login" autocomplete="on">
<div class="k">GRIDPRINT</div><h1>Sign in</h1>{f'<div class="err">{m}</div>' if m else ''}
<label for="u">Username</label><input id="u" type="text" name="username" autocomplete="username" required maxlength="64" autofocus>
<label for="p">Password</label><input id="p" type="password" name="password" autocomplete="current-password" required maxlength="256">
<div class="row"><input id="r" type="checkbox" name="remember" value="1"><label for="r">Remember me on this device (admins, 30 days)</label></div>
<button class="pri" type="submit">Sign in</button>
<p class="sub" style="margin:14px 0 0">Private dashboard. Accounts are issued by the GridPrint team.</p>
</form></body></html>"""


APP_PAGE = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>GridPrint · Pakistan grid explorer</title>
<link rel="stylesheet" href="{UPLOT_CDN}uPlot.min.css" integrity="{UPLOT_CSS_SRI}" crossorigin="anonymous">
<style>{BASE_CSS}
header{{padding:16px 22px 12px;border-bottom:2px solid var(--acc);display:flex;flex-wrap:wrap;gap:10px 24px;align-items:flex-end;justify-content:space-between}}
h1{{margin:2px 0 0;font-size:24px;letter-spacing:-.01em}}
main{{padding:16px 22px 40px;max-width:1280px;margin:0 auto}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:14px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:0}}
.lbl{{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.09em}}
.big{{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap}}
.mid{{font-size:16px;font-weight:600;font-variant-numeric:tabular-nums}}
.bar{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}}
.grp{{display:flex;gap:4px;flex-wrap:wrap;align-items:center;background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:4px}}
.grp button,.grp input{{padding:5px 10px}}
#chart{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 6px 2px;position:relative;min-height:400px}}
#msg{{position:absolute;inset:0;display:none;align-items:center;justify-content:center;color:var(--mut);font-size:15px;pointer-events:none}}
#tip{{position:absolute;display:none;pointer-events:none;z-index:10;background:#0d0d11ee;border:1px solid #3a3a46;border-radius:8px;padding:7px 10px;font-size:12px;line-height:1.5;font-variant-numeric:tabular-nums;white-space:nowrap;box-shadow:0 6px 18px #0008}}
#tip b{{color:var(--acc);font-size:15px}}#tip .tt{{color:var(--mut);font-size:12px}}
#slider{{position:relative;height:34px;margin:8px 4px 2px;user-select:none;touch-action:none}}
#slTrack{{position:absolute;left:0;right:0;top:13px;height:8px;border-radius:6px;background:#1f1f27;border:1px solid var(--line)}}
#slWin{{position:absolute;top:5px;height:24px;border-radius:7px;background:rgba(224,89,42,.22);border:1px solid var(--acc);cursor:grab;min-width:14px}}
#slWin:active{{cursor:grabbing}}
.slH{{position:absolute;top:-1px;width:12px;height:24px;border-radius:5px;background:var(--acc);cursor:ew-resize}}
.slH::after{{content:"";position:absolute;left:5px;top:6px;width:2px;height:10px;background:#111;border-radius:1px}}
#slL{{left:-7px}}#slR{{right:-7px}}
.slLbl{{display:flex;justify-content:space-between;color:var(--mut);font-size:11px;margin:0 4px 10px;font-variant-numeric:tabular-nums}}
.orb{{position:absolute;top:10px;right:10px;width:38px;height:38px;padding:0;border-radius:50%;border:1px solid var(--line);background:#121218;cursor:pointer;display:grid;place-items:center;transition:transform .25s}}
.orb svg{{width:28px;height:28px}}.orb .ring{{fill:none;stroke:currentColor;stroke-width:1.6;opacity:.35}}
.orb .wave{{fill:none;stroke:currentColor;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:80;stroke-dashoffset:0}}
.orb.calm{{color:#3fbf7f}}.orb.active{{color:#e6a23c}}.orb.stressed{{color:#e5484d}}.orb.unknown{{color:#8d8b84}}
.orb{{animation:breathe 3.2s ease-in-out infinite}}.orb.stressed{{animation-duration:1.4s}}.orb.active{{animation-duration:2.2s}}
@keyframes breathe{{0%,100%{{box-shadow:0 0 0 0 currentColor}}50%{{box-shadow:0 0 14px -2px currentColor}}}}
.orb:hover,.orb.open{{transform:scale(1.12)}}.orb:hover .wave,.orb.open .wave{{animation:trace 1.1s ease-in-out infinite}}
@keyframes trace{{0%{{stroke-dashoffset:80}}60%{{stroke-dashoffset:0}}100%{{stroke-dashoffset:-80}}}}
.orbpop{{display:none;position:absolute;top:54px;left:6px;z-index:30;width:min(360px,86vw);background:#15151c;border:1px solid var(--line);border-radius:12px;padding:12px 14px;box-shadow:0 12px 32px rgba(0,0,0,.5);font-size:13px;line-height:1.5;text-transform:none;letter-spacing:0}}
.orbpop.show{{display:block;animation:pop .18s ease-out}}@keyframes pop{{from{{opacity:0;transform:translateY(-4px)}}to{{opacity:1;transform:none}}}}
.orbpop h4{{margin:0 0 6px;font-size:14px;color:var(--ink)}}.orbpop .ai{{border-left:2px solid var(--acc);padding:2px 0 2px 9px;margin:8px 0;color:var(--ink)}}
.orbpop .tag{{font-size:11px;color:var(--mut);display:block;margin-top:2px}}.orbpop ul{{margin:6px 0 0;padding-left:16px;color:var(--mut)}}.orbpop li{{margin:3px 0}}
@media (prefers-reduced-motion:reduce){{.orb,.orb .wave{{animation:none!important}}}}
.tjump{{color:var(--acc);text-decoration:none;border-bottom:1px dotted var(--acc);cursor:pointer}}.tjump:hover{{background:rgba(224,89,42,.15)}}
.tw{{overflow-x:auto;margin:8px 0}}table.rt{{border-collapse:collapse;width:100%;font-size:13px}}table.rt th,table.rt td{{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}}table.rt th{{color:var(--mut);font-weight:600}}
#report h3{{font-size:14px;margin:14px 0 4px;color:var(--ink)}}
#details{{margin-bottom:12px}}#details .row{{display:flex;justify-content:space-between;gap:12px;padding:4px 0;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}}
#details .row span:first-child{{color:var(--mut)}}#details ul{{margin:8px 0 0;padding-left:18px;color:var(--mut);font-size:12px;line-height:1.6}}
.x{{padding:2px 9px;font-size:16px;line-height:1}}
.two{{display:grid;grid-template-columns:3fr 2fr;gap:12px}}@media(max-width:860px){{.two{{grid-template-columns:1fr}}}}
table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}td,th{{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left}}
th{{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.08em}}
tr.ev{{cursor:pointer}}tr.ev:hover td{{background:#1c1c23}}
.dot{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle;background:var(--bad)}}
.u-select{{background:rgba(224,89,42,.18)}}
.who{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}.badge{{font-size:11px;padding:2px 8px;border-radius:20px;border:1px solid var(--line);color:var(--mut)}}
.badge.admin{{color:var(--acc);border-color:var(--acc)}}
.adminonly{{display:none!important}}body.admin .adminonly{{display:revert!important}}
#reportCard{{display:none;margin-bottom:12px}}#report{{line-height:1.6;color:var(--ink);font-size:14px}}
#report h2{{font-size:15px;margin:14px 0 6px;color:var(--acc)}}#report ul{{margin:4px 0;padding-left:20px}}#report p{{margin:6px 0}}
dialog{{background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:12px;max-width:820px;width:94%}}
dialog::backdrop{{background:#000a}}
</style></head><body>
<header>
 <div><div class="k">GRIDPRINT · GRID FREQUENCY EXPLORER</div><h1>Pakistan grid heartbeat</h1>
 <div class="sub">Measured straight from the mains by GridPrint Stations · all times PKT (UTC+5)</div></div>
 <div class="who"><label class="lbl">Station&nbsp;<select id="station"></select></label>
  <span id="me" class="sub"></span><span id="role" class="badge"></span>
  <button id="adminBtn" class="adminonly">Admin</button>
  <form method="post" action="{PREFIX}/logout" style="margin:0"><button type="submit">Sign out</button></form></div>
</header>
<main>
<div class="cards">
 <div class="card" style="position:relative"><div class="lbl">Frequency now</div><div class="big" id="cNow">–</div>
  <button id="orb" class="orb unknown" aria-label="What is the grid doing right now?" title="What is the grid doing right now?">
   <svg viewBox="0 0 40 40" aria-hidden="true"><circle class="ring" cx="20" cy="20" r="17"/><path class="wave" d="M5 20 H11 L14 12 L18 28 L22 9 L26 25 L29 20 H35"/></svg>
  </button>
  <div id="orbPop" class="orbpop" role="tooltip"><div class="sub">Reading the grid…</div></div></div>
 <div class="card"><div class="lbl">Status</div><div class="big" id="cStatus">–</div><div class="sub" id="cCause"></div></div>
 <div class="card"><div class="lbl">Last reading</div><div class="mid" id="cAgo">–</div></div>
 <div class="card"><div class="lbl">Showing</div><div class="mid" id="cRange">–</div></div>
 <div class="card"><div class="lbl">Mean ± std dev</div><div class="mid" id="cMean">–</div></div>
 <div class="card"><div class="lbl">Outage time · glitches</div><div class="mid" id="cOut">–</div></div>
</div>
<div class="bar">
 <div class="grp" id="presets">
  <button data-p="live" class="on">Live 15 min</button><button data-p="3600">1 h</button><button data-p="21600">6 h</button>
  <button data-p="86400">24 h</button><button data-p="today">Today</button><button data-p="yesterday">Yesterday</button><button data-p="604800">7 days</button>
 </div>
 <div class="grp"><button id="back" title="Step back">◀</button><input type="date" id="day" title="Pick a day"><button id="fwd" title="Step forward">▶</button></div>
 <div class="grp"><input type="datetime-local" id="from"><span class="sub">→</span><input type="datetime-local" id="to"><button id="go">Show</button></div>
 <div class="grp"><button id="zIn" title="Zoom in">＋</button><button id="zOut" title="Zoom out">－</button><button id="reset">Reset zoom</button><button id="detBtn" title="Show point details">ⓘ Details</button><button id="dl" class="adminonly">Download CSV</button><button id="ai" class="adminonly pri">✦ AI report</button></div>
</div>
<div id="chart"><div id="msg"></div><div id="tip"></div></div>
<div id="slider" title="Drag the window to move along the timeline; drag its edges to zoom"><div id="slTrack"></div><div id="slWin"><div class="slH" id="slL"></div><div class="slH" id="slR"></div></div></div>
<div class="slLbl"><span id="slA"></span><span id="slB"></span></div>
<div class="card" id="details"><div class="bar" style="justify-content:space-between;margin:0 0 4px"><div class="lbl">Point details</div><button class="x" id="detClose" title="Close">×</button></div>
 <div id="detBody"><div class="sub">Hover over the graph to see the full reading here.</div></div>
 <ul><li>Drag across the graph to zoom into that part, or use ＋ / － and the slider. Scroll zooms gently; Shift+scroll or a sideways swipe moves along the timeline. Double-click resets.</li>
 <li>Red shading = outage · yellow ticks = glitch seconds · shaded band = min to max inside each point.</li>
 <li>Click an outage in the table below to jump to it.</li></ul></div>
<div class="card" id="reportCard"><div class="bar" style="justify-content:space-between;margin:0 0 4px"><div class="lbl">AI report · Gemini</div><button id="copyRep">Copy</button></div><div id="report"></div></div>
<div class="two">
 <div class="card"><div class="lbl" style="margin-bottom:6px">Outages in this range</div>
  <table><thead><tr><th>Start</th><th>End</th><th>Duration</th><th>Likely cause</th></tr></thead><tbody id="events"><tr><td colspan="4" class="sub">–</td></tr></tbody></table></div>
 <div class="card"><div class="lbl" style="margin-bottom:6px">Range statistics</div>
  <table><tbody id="stats"></tbody></table>
  <div class="sub adminonly" style="margin-top:8px">Raw daily files (UTC days): <span id="days"></span></div></div>
</div>
</main>
<dialog id="adminDlg"><div class="bar" style="justify-content:space-between"><div class="k">ADMIN</div><button id="closeAdmin">Close</button></div>
 <div class="lbl" style="margin:10px 0 6px">Gemini (AI reports)</div>
 <div class="bar"><input id="gKey" type="password" placeholder="Paste Gemini API key" style="flex:1;min-width:220px" autocomplete="off">
  <input id="gModel" type="text" placeholder="model" style="width:200px"><button id="saveKey" class="pri">Save</button></div>
 <div id="keyState" class="sub"></div>
 <div class="lbl" style="margin:16px 0 6px">Signed-in devices</div>
 <table><thead><tr><th>User</th><th>Since</th><th>Until</th><th>Device</th><th></th></tr></thead><tbody id="sessRows"></tbody></table>
 <div class="bar" style="margin-top:10px"><button id="revokeAll">Sign out every device</button></div>
</dialog>
<script src="{UPLOT_CDN}uPlot.iife.min.js" integrity="{UPLOT_JS_SRI}" crossorigin="anonymous"></script>
<script src="static/app.js"></script>
</body></html>"""


APP_JS = r"""
'use strict';
(async function(){
if(!window.uPlot){ await new Promise(res=>{const s=document.createElement('script');s.src='static/uplot.js';s.onload=res;s.onerror=res;document.head.appendChild(s);});
  const l=document.createElement('link');l.rel='stylesheet';l.href='static/uplot.css';document.head.appendChild(l); }
const PKT=5*3600, $=id=>document.getElementById(id), base=location.pathname.replace(/\/$/,'');
let full=null, detOpen=true, sliding=false;
let me={}, station='', win={mode:'live',from:0,to:0}, view=null, plot=null, data=null, liveTimer=null, refetchT=null, seq=0;
const pad=n=>String(n).padStart(2,'0'), now=()=>Date.now()/1000, pk=t=>new Date((t+PKT)*1000);
const fmtT=(t,sec=true)=>{const d=pk(t);return pad(d.getUTCHours())+':'+pad(d.getUTCMinutes())+(sec?':'+pad(d.getUTCSeconds()):'')};
const fmtD=t=>{const d=pk(t);return d.getUTCFullYear()+'-'+pad(d.getUTCMonth()+1)+'-'+pad(d.getUTCDate())};
const fmtDT=t=>fmtD(t)+' '+fmtT(t,false), toInput=t=>fmtD(t)+'T'+fmtT(t,false);
const fromInput=v=>{if(!v)return null;const[d,h='00:00']=v.split('T');const[Y,M,D]=d.split('-').map(Number);const[hh,mm]=h.split(':').map(Number);return Date.UTC(Y,M-1,D,hh,mm)/1000-PKT};
const dayStart=t=>{const d=pk(t);return Date.UTC(d.getUTCFullYear(),d.getUTCMonth(),d.getUTCDate())/1000-PKT};
const dur=s=>{s=Math.round(s);if(s<60)return s+' s';if(s<3600)return Math.floor(s/60)+' min '+(s%60)+' s';return Math.floor(s/3600)+' h '+Math.round(s%3600/60)+' min'};
const fmt12=t=>{const d=pk(t);let h=d.getUTCHours();const ap=h<12?'AM':'PM';h=h%12||12;return h+':'+pad(d.getUTCMinutes())+':'+pad(d.getUTCSeconds())+' '+ap};
const fmt12s=t=>{const d=pk(t);let h=d.getUTCHours();const ap=h<12?'AM':'PM';h=h%12||12;return h+':'+pad(d.getUTCMinutes())+' '+ap};
const diag=p=>p==null?['–','No sensor check (older firmware)']:p>=90?['Sensor fault','Pin D2 stuck LOW: problem on the sensor output side (PC817 or pull-up wiring). Probably not a real power cut.']
  :p<=10?['Power cut','Pin D2 stuck HIGH: no mains signal reached the sensor (real power cut, or transformer side unplugged).']:['Loose wire / noise','Pin D2 flickering: loose connection or electrical noise.'];
const rangeLabel=(a,b)=>fmtDT(a)+' → '+(fmtD(a)===fmtD(b)?fmtT(b,false):fmtDT(b));
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const msg=t=>{const m=$('msg');m.textContent=t;m.style.display=t?'flex':'none'};
async function api(url,opt={}){const r=await fetch(url,Object.assign({cache:'no-store',credentials:'same-origin'},opt));
  if(r.status===401){location.href=base+'/login';throw new Error('login')} return r}
const getJSON=u=>api(u).then(r=>r.json());
const postJSON=(u,b)=>api(u,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF':me.csrf},body:JSON.stringify(b)}).then(async r=>({ok:r.ok,body:await r.json()}));

function setWindow(mode,from,to){
  win={mode,from,to}; view=null;
  document.querySelectorAll('#presets button').forEach(b=>b.classList.toggle('on',b.dataset.p===mode));
  $('day').value=fmtD(from); $('from').value=toInput(from); $('to').value=toInput(to);
  clearInterval(liveTimer); liveTimer=null;
  if(mode==='live') liveTimer=setInterval(()=>{ if(view) return; const t=now(); win.from=t-900; win.to=t; load(win.from,win.to,true); },5000);
  load(from,to);
}
function preset(p){const t=now();
  if(p==='live') return setWindow('live',t-900,t);
  if(p==='today') return setWindow('today',dayStart(t),t);
  if(p==='yesterday'){const s=dayStart(t)-86400;return setWindow('yesterday',s,s+86400)}
  setWindow(p,t-(+p),t)}
const apiRange=(a,b)=>`${base}/api/range?station=${encodeURIComponent(station)}&from=${a}&to=${b}&points=${Math.round(($('chart').clientWidth||900)*1.5)}`;

async function load(from,to,quiet){
  if(!station) return;
  const id=++seq; if(!quiet) msg('Loading…');
  let r; try{ r=await getJSON(apiRange(from,to)) }catch(e){ return msg('Could not reach the server') }
  if(id!==seq) return;
  data=r; full=r; msg(r.t.length?'':'No readings in this range');
  if(plot&&quiet){plot.setData([r.t,r.mean,r.min,r.max],false);plot.setScale('x',{min:from,max:to})} else draw(from,to);
  fillPanels(from,to);
}
async function refine(a,b){const id=++seq;const r=await getJSON(apiRange(a,b)).catch(()=>null);
  if(!r||id!==seq||sliding) return; data=r; plot.setData([r.t,r.mean,r.min,r.max],false); plot.redraw(); fillPanels(a,b)}

// keep every zoom inside the loaded window and never narrower than one minute, so the graph can't get lost
function clampRange(a,b){
  const W0=win.from, W1=win.to, full=Math.max(1,W1-W0), minS=Math.min(60,full);
  const s=Math.max(minS,Math.min(full,b-a)), m=(a+b)/2; let x=m-s/2, y=m+s/2;
  if(x<W0){x=W0;y=W0+s} if(y>W1){y=W1;x=W1-s} return [x,y];
}
function zoomBy(f){ if(!plot) return; const a=plot.scales.x.min,b=plot.scales.x.max,m=(a+b)/2,h=(b-a)*f/2;
  const c=clampRange(m-h,m+h); plot.setScale('x',{min:c[0],max:c[1]}) }

function details(i){
  const d=data; if(!detOpen) return;
  if(i==null||!d||d.t[i]==null){return}
  const t=d.t[i], m=d.mean[i], lo=d.min[i], hi=d.max[i], o=d.out[i], g=d.gl[i];
  const rows=[['Date',fmtD(t)],['Time (PKT)',fmt12(t)],
    ['Frequency',m==null?'no grid signal':m.toFixed(4)+' Hz'],
    ['Offset from 50 Hz',m==null?'–':(m>=50?'+':'')+((m-50)*1000).toFixed(1)+' mHz']];
  if(d.bucket>1&&m!=null) rows.push(['Range in this point',lo.toFixed(4)+' – '+hi.toFixed(4)+' Hz'],['Point covers',dur(d.bucket)]);
  rows.push(['Outage',o>0?(o*100).toFixed(0)+' % of this point':'none'],['Glitch seconds',g||0]);
  if((o>0||g>0)&&d.d2&&d.d2[i]!=null) rows.push(['Sensor check',diag(d.d2[i])[1]+' (D2 low '+d.d2[i]+' % of the time)']);
  $('detBody').innerHTML=rows.map(r=>`<div class="row"><span>${esc(r[0])}</span><span>${esc(r[1])}</span></div>`).join('');
}
function tipPlugin(){
  const tip=$('tip');
  return {hooks:{setCursor:[u=>{
    const i=u.cursor.idx, d=data;
    if(i==null||!d||d.t[i]==null||u.cursor.left<0){tip.style.display='none';return}
    const t=d.t[i], m=d.mean[i];
    tip.innerHTML=`<div class="tt">${fmt12(t)}</div>`+(m!=null?`<b>${m.toFixed(3)} Hz</b>`:`<b style="color:var(--bad)">no signal</b>`);
    tip.style.display='block'; details(i);
    const L=u.over.offsetLeft+u.valToPos(t,'x'), T=u.over.offsetTop+(m!=null?u.valToPos(m,'y'):u.cursor.top);
    const W=tip.offsetWidth, H=tip.offsetHeight, cw=$('chart').clientWidth;
    let x=L+14; if(x+W>cw-4) x=L-W-14; tip.style.left=Math.max(4,x)+'px';
    tip.style.top=Math.max(4,T-H-12)+'px';
  }]}};
}

// ---- horizontal range slider: the track is the whole loaded window, the orange box is what the graph shows
function syncSlider(){
  const span=win.to-win.from; if(!plot||span<=0) return;
  const a=Math.max(win.from,plot.scales.x.min), b=Math.min(win.to,plot.scales.x.max);
  const W=$('slider').clientWidth;
  $('slWin').style.left=((a-win.from)/span*W)+'px'; $('slWin').style.width=Math.max(14,(b-a)/span*W)+'px';
  $('slA').textContent=fmt12s(win.from); $('slB').textContent=fmt12s(win.to);
}
function initSlider(){
  const box=$('slWin'), sl=$('slider'); let mode=null, x0=0, a0=0, b0=0;
  const down=(e,m)=>{e.preventDefault();e.stopPropagation();if(!plot)return;mode=m;x0=e.clientX;a0=plot.scales.x.min;b0=plot.scales.x.max;
    sliding=true; if(full&&data!==full){data=full;plot.setData([full.t,full.mean,full.min,full.max],false)}
    window.addEventListener('pointermove',move);window.addEventListener('pointerup',up,{once:true})};
  const move=e=>{const span=win.to-win.from, dv=(e.clientX-x0)/sl.clientWidth*span, minW=Math.max(15,span*0.005);
    let a=a0,b=b0;
    if(mode==='pan'){const w=b0-a0;a=Math.min(Math.max(win.from,a0+dv),win.to-w);b=a+w}
    else if(mode==='L'){a=Math.min(Math.max(win.from,a0+dv),b0-minW)}
    else if(mode==='R'){b=Math.max(Math.min(win.to,b0+dv),a0+minW)}
    plot.setScale('x',{min:a,max:b})};
  const up=()=>{window.removeEventListener('pointermove',move);mode=null;sliding=false;
    if(view) {clearTimeout(refetchT);refetchT=setTimeout(()=>refine(view.from,view.to),50)}};
  box.addEventListener('pointerdown',e=>down(e,'pan'));
  $('slL').addEventListener('pointerdown',e=>down(e,'L'));
  $('slR').addEventListener('pointerdown',e=>down(e,'R'));
  sl.addEventListener('pointerdown',e=>{if(e.target!==sl&&e.target!==$('slTrack'))return;if(!plot)return;   // click the track: jump there
    const span=win.to-win.from, w=plot.scales.x.max-plot.scales.x.min, r=sl.getBoundingClientRect();
    const c=win.from+(e.clientX-r.left)/r.width*span; let a=Math.min(Math.max(win.from,c-w/2),win.to-w);
    if(full&&data!==full){data=full;plot.setData([full.t,full.mean,full.min,full.max],false)}
    plot.setScale('x',{min:a,max:a+w})});
  window.addEventListener('resize',syncSlider);
}
function setDetails(open){detOpen=open;$('details').style.display=open?'':'none';$('detBtn').style.display=open?'none':'';
  try{localStorage.setItem('gp_details',open?'1':'0')}catch(e){}}

function draw(from,to){
  const el=$('chart'); if(plot){plot.destroy();plot=null}
  const fmtHz=(u,v)=>v==null?'–':v.toFixed(4)+' Hz';
  plot=new uPlot({
    width:el.clientWidth-12, height:Math.max(360,Math.min(540,innerHeight*0.52)), plugins:[tipPlugin()],
    tzDate:ts=>uPlot.tzDate(new Date(ts*1e3),'Asia/Karachi'),
    legend:{show:false},
    cursor:{drag:{x:true,y:false,setScale:true},points:{size:9,fill:'#e0592a',stroke:'#fff',width:1.5}},
    scales:{x:{time:true,min:from,max:to},y:{range:(u,mn,mx)=>{if(mn==null)return[49.5,50.5];const p=Math.max(0.01,(mx-mn)*0.12);return[mn-p,mx+p]}}},
    axes:[{stroke:'#8d8b84',grid:{stroke:'#1f1f26'},ticks:{stroke:'#2a2a33'}},
          {stroke:'#8d8b84',grid:{stroke:'#1f1f26'},ticks:{stroke:'#2a2a33'},size:64,values:(u,v)=>v.map(x=>x.toFixed(3))}],
    series:[{label:'Time (PKT)',value:(u,v)=>v==null?'–':fmtD(v)+' '+fmtT(v)},
            {label:'Frequency',stroke:'#e0592a',width:1.6,value:fmtHz},
            {label:'Min',stroke:'rgba(224,89,42,.3)',width:0,value:fmtHz,points:{show:false}},
            {label:'Max',stroke:'rgba(224,89,42,.3)',width:0,value:fmtHz,points:{show:false}}],
    bands:[{series:[3,2],fill:'rgba(224,89,42,.16)'}],
    hooks:{
      drawClear:[u=>{const d=data;if(!d||!d.t.length)return;
        const ctx=u.ctx,{top,height}=u.bbox,x0=u.valToPos(d.t[0],'x',true),bw=Math.max(1.5,u.valToPos(d.t[0]+d.bucket,'x',true)-x0),tick=8*devicePixelRatio;
        ctx.save();for(let i=0;i<d.t.length;i++){
          if(d.out[i]>0){ctx.fillStyle=`rgba(229,72,77,${0.18+0.4*d.out[i]})`;ctx.fillRect(u.valToPos(d.t[i],'x',true),top,bw,height)}
          if(d.gl[i]>0){ctx.fillStyle='#f0b429';ctx.fillRect(u.valToPos(d.t[i],'x',true),top+height-tick,Math.max(1.5,bw*0.7),tick)}}
        ctx.restore()}],
      setScale:[(u,key)=>{if(key!=='x')return;const a=u.scales.x.min,b=u.scales.x.max;
        const c=clampRange(a,b); if(Math.abs(c[0]-a)>0.5||Math.abs(c[1]-b)>0.5){setTimeout(()=>u.setScale('x',{min:c[0],max:c[1]}),0);return}
        syncSlider();
        if(Math.abs(a-win.from)<1&&Math.abs(b-win.to)<1){view=null;return}
        view={from:a,to:b};$('cRange').textContent=rangeLabel(a,b);
        if(sliding) return; clearTimeout(refetchT);refetchT=setTimeout(()=>refine(a,b),350)}]
    }
  },[data.t,data.mean,data.min,data.max],el);
  el.appendChild($('msg')); el.appendChild($('tip')); syncSlider();
  plot.over.addEventListener('mouseleave',()=>{$('tip').style.display='none'});
  plot.over.addEventListener('dblclick',()=>{view=null;load(win.from,win.to)});
  // gentle wheel: deltas are gathered and applied once per frame, zoom is proportional and capped,
  // sideways scroll (or Shift+scroll) moves along the timeline instead of zooming
  let wz=0, wp=0, wx=null, wRaf=0;
  plot.over.addEventListener('wheel',e=>{e.preventDefault();
    const k=e.deltaMode===1?33:e.deltaMode===2?400:1;
    if(e.shiftKey||Math.abs(e.deltaX)>Math.abs(e.deltaY)) wp+=(e.shiftKey?e.deltaY:e.deltaX)*k; else wz+=e.deltaY*k;
    wx=plot.posToVal(e.offsetX,'x');
    if(!wRaf) wRaf=requestAnimationFrame(()=>{wRaf=0; const u=plot,a=u.scales.x.min,b=u.scales.x.max,span=b-a;
      if(wp&&Math.abs(wp)>=Math.abs(wz)){const c=clampRange(a+wp*span*0.0008,b+wp*span*0.0008); u.setScale('x',{min:c[0],max:c[1]})}
      else if(wz){const f=Math.exp(Math.max(-120,Math.min(120,wz))*0.0011);   // at most ~14 % per frame
        const c=clampRange(wx-(wx-a)*f, wx+(b-wx)*f); u.setScale('x',{min:c[0],max:c[1]})}
      wz=0; wp=0});
  },{passive:false});
}

function fillPanels(a,b){
  const d=data,s=d.stats;
  $('cRange').textContent=rangeLabel(a,b);
  $('cMean').textContent=s.mean==null?'–':s.mean.toFixed(3)+' ± '+s.std.toFixed(3)+' Hz';
  $('cOut').textContent=dur(s.outage_s)+' · '+s.glitches;
  const rows=[['Readings',s.readings.toLocaleString()],['Coverage',Math.min(100,s.coverage*100).toFixed(1)+' %'],
    ['Lowest',s.min==null?'–':s.min.toFixed(4)+' Hz at '+fmtT(s.min_t)],['Highest',s.max==null?'–':s.max.toFixed(4)+' Hz at '+fmtT(s.max_t)],
    ['Mean',s.mean==null?'–':s.mean.toFixed(4)+' Hz'],['Std dev',s.std==null?'–':(s.std*1000).toFixed(1)+' mHz'],
    ['Outage time',dur(s.outage_s)],['Glitch seconds',s.glitches],['Detail',d.bucket===1?'every second':'1 point per '+dur(d.bucket)]];
  $('stats').innerHTML=rows.map(r=>`<tr><td class="sub">${esc(r[0])}</td><td>${esc(r[1])}</td></tr>`).join('');
  const ev=d.events.slice().reverse();
  $('events').innerHTML=ev.length?ev.slice(0,80).map(e=>`<tr class="ev" data-a="${+e[0]}" data-b="${+e[1]}"><td><span class="dot"></span>${fmtDT(e[0])}</td><td>${fmtT(e[1])}</td><td>${dur(e[1]-e[0]+1)}</td><td title="${esc(diag(e[2])[1])}">${esc(diag(e[2])[0])}</td></tr>`).join('')
    :'<tr><td colspan="4" class="sub">No outages in this range</td></tr>';
  document.querySelectorAll('tr.ev').forEach(tr=>tr.onclick=()=>{const a=+tr.dataset.a,b=+tr.dataset.b,p=Math.max(60,(b-a)*0.6);plot.setScale('x',{min:a-p,max:b+p})});
}

// ---- the live insight orb
let insight=null, insightAt=0, orbHide=0;
async function loadInsight(){ if(!station) return;
  const r=await getJSON(`${base}/api/insight?station=${encodeURIComponent(station)}`).catch(()=>null); if(!r) return;
  insight=r; insightAt=Date.now(); $('orb').className='orb '+(r.mood||'unknown')+($('orbPop').classList.contains('show')?' open':''); renderInsight() }
function ago(s){return s<90?'just now':s<3600?Math.round(s/60)+' min ago':Math.round(s/3600)+' h ago'}
function renderInsight(){ const r=insight, el=$('orbPop'); if(!r){el.innerHTML='<div class="sub">Reading the grid…</div>';return}
  const mood={calm:'Calm',active:'Active',stressed:'Under stress',unknown:'No data'}[r.mood]||'';
  let h=`<h4>${esc(mood)} · ${esc(r.headline)}</h4>`;
  if(r.ai) h+=`<div class="ai">${md(r.ai.text).replace(/^<p>|<\/p>$/g,'')}<span class="tag">✦ AI · ${esc(ago(r.ai.age_s))} · may be wrong</span></div>`;
  if(r.bullets&&r.bullets.length) h+='<ul>'+r.bullets.map(b=>'<li>'+md(b.text).replace(/^<p>|<\/p>$/g,'')+'</li>').join('')+'</ul>';
  h+='<span class="tag">Measured from the last 30 minutes · click a time to see it</span>';
  el.innerHTML=h }
function showOrb(on){ clearTimeout(orbHide); const p=$('orbPop');
  if(on){ p.classList.add('show'); $('orb').classList.add('open'); if(Date.now()-insightAt>60000) loadInsight(); else renderInsight() }
  else orbHide=setTimeout(()=>{p.classList.remove('show');$('orb').classList.remove('open')},250) }
function initOrb(){
  const o=$('orb'), p=$('orbPop');
  o.addEventListener('mouseenter',()=>showOrb(true)); o.addEventListener('mouseleave',()=>showOrb(false));
  p.addEventListener('mouseenter',()=>showOrb(true)); p.addEventListener('mouseleave',()=>showOrb(false));
  o.addEventListener('click',e=>{e.stopPropagation(); showOrb(!p.classList.contains('show'))});
  o.addEventListener('focus',()=>showOrb(true)); o.addEventListener('blur',()=>showOrb(false));
  document.addEventListener('click',e=>{ if(!p.contains(e.target)&&e.target!==o) { p.classList.remove('show'); o.classList.remove('open') } });
  p.addEventListener('click',e=>{ const a=e.target.closest('a.tjump'); if(!a) return; e.preventDefault();
    const t=now(); if(win.from>t-1800||win.to<t-120){ document.querySelector('button[data-p="3600"]').click(); setTimeout(()=>{repRange={from:t-1800,to:t};jumpTo(a.dataset.md,a.dataset.hm)},1800) }
    else { repRange={from:t-1800,to:t}; jumpTo(a.dataset.md,a.dataset.hm) } });
  loadInsight(); setInterval(loadInsight,60000);
}

async function liveCards(){
  if(!station) return; const t=now();
  const r=await getJSON(`${base}/api/range?station=${encodeURIComponent(station)}&from=${t-30}&to=${t+5}&points=60`).catch(()=>null); if(!r) return;
  const ls=r.last_seen, st=$('cStatus');
  $('cAgo').textContent=ls?(t-ls<120?Math.round(t-ls)+' s ago':t-ls<7200?Math.round((t-ls)/60)+' min ago':fmtDT(ls)):'–';
  const i=r.t.length-1;
  if(i<0||!ls||t-ls>60){$('cNow').textContent='–';st.textContent=ls?'OFFLINE':'–';st.style.color='var(--mut)';return}
  const out=r.out[i]>0.5,gl=r.gl[i]>0;
  $('cNow').textContent=r.mean[i]==null?'–':r.mean[i].toFixed(3)+' Hz';
  st.textContent=out?'OUTAGE':gl?'GLITCH':'OK'; st.style.color=out?'var(--bad)':gl?'var(--warn)':'var(--ok)';
  const dg=(out||gl)&&r.d2&&r.d2[i]!=null?diag(r.d2[i]):null; st.title=dg?dg[1]:'';
  $('cCause').textContent=dg?dg[0]:''; $('cCause').title=dg?dg[1]:'';
}

async function loadStations(){
  const a=await getJSON(base+'/api/stations').catch(()=>[]);
  const sel=$('station'),cur=station;
  sel.innerHTML=a.map(x=>`<option>${esc(x.station)}</option>`).join('');
  station=cur&&a.some(x=>x.station===cur)?cur:((a.find(x=>x.station==='isb-01')||a[0]||{}).station||'');
  sel.value=station;
  if(station&&me.role==='admin'){const d=await getJSON(`${base}/api/days?station=${encodeURIComponent(station)}`).catch(()=>({days:[]}));
    $('days').innerHTML=d.days.slice(-14).reverse().map(x=>`<a href="${base}/data/${encodeURIComponent(station)}/${esc(x)}.csv">${esc(x)}</a>`).join(' · ')+(d.days.length>14?' …':'')}
}

function md(src){
  const lines=esc(src).split('\n'); let html='', list=false, tbl=null;
  const timeLink=t=>t.replace(/\b(?:(\d{2})-(\d{2})\s+)?([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?\b/g,
    (m,mo,d,h,mi)=>`<a href="#" class="tjump" data-md="${mo?mo+'-'+d:''}" data-hm="${h.padStart(2,'0')}:${mi}">${m}</a>`);
  const inline=x=>timeLink(x.replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/\*(.+?)\*/g,'<i>$1</i>').replace(/`(.+?)`/g,'<code>$1</code>'));
  const cells=ln=>ln.trim().replace(/^\||\|$/g,'').split('|').map(c=>c.trim());
  const flush=()=>{ if(list){html+='</ul>';list=false}
    if(tbl){ html+='<div class="tw"><table class="rt"><thead><tr>'+tbl[0].map(c=>'<th>'+inline(c)+'</th>').join('')+'</tr></thead><tbody>'
      +tbl.slice(1).map(r=>'<tr>'+r.map(c=>'<td>'+inline(c)+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>'; tbl=null } };
  for(const ln of lines){
    if(/^\s*\|.*\|\s*$/.test(ln)){ if(list){html+='</ul>';list=false}
      if(/^\s*\|?[\s:|-]+\|?\s*$/.test(ln)&&ln.includes('-')) continue;      // separator row
      (tbl=tbl||[]).push(cells(ln)); continue }
    if(tbl) flush();
    if(/^\s*[-*] /.test(ln)){ if(!list){html+='<ul>';list=true} html+='<li>'+inline(ln.replace(/^\s*[-*] /,''))+'</li>'; continue }
    if(list){html+='</ul>';list=false}
    if(/^#{1,2} /.test(ln)) html+='<h2>'+inline(ln.replace(/^#{1,2} /,''))+'</h2>';
    else if(/^#{3,4} /.test(ln)) html+='<h3>'+inline(ln.replace(/^#{3,4} /,''))+'</h3>';
    else if(ln.trim()) html+='<p>'+inline(ln)+'</p>';
  }
  flush(); return html;
}
// clicking a time in the report zooms the graph to it and points at that moment
let repRange=null;
function jumpTo(mdStr,hm){
  if(!repRange||!plot) return;
  const [h,mi]=hm.split(':').map(Number); let best=null;
  for(let d=Math.floor((repRange.from+5*3600)/86400)*86400; d<=repRange.to+5*3600; d+=86400){
    const day=new Date(d*1000), t=Date.UTC(day.getUTCFullYear(),day.getUTCMonth(),day.getUTCDate(),h,mi)/1000-5*3600;
    if(mdStr&&pad(day.getUTCMonth()+1)+'-'+pad(day.getUTCDate())!==mdStr) continue;
    if(t>=repRange.from-120&&t<=repRange.to+120){best=t;break}
  }
  if(best==null) return;
  if(best<win.from||best>win.to){ msg('That time is outside the range on screen. Pick the report range again.'); setTimeout(()=>msg(''),3000); return }
  const c=clampRange(best-900,best+900); plot.setScale('x',{min:c[0],max:c[1]});
  $('chart').scrollIntoView({behavior:'smooth',block:'center'});
  setTimeout(()=>{ if(!plot) return; const i=data.t.findIndex(x=>x>=best); if(i<0) return;
    plot.setCursor({left:plot.valToPos(data.t[i],'x'),top:data.mean[i]!=null?plot.valToPos(data.mean[i],'y'):10}) },900);
}
let lastReport='';
async function aiReport(){
  const w=view||win, card=$('reportCard'); card.style.display='block';
  $('report').innerHTML='<p class="sub">Asking Gemini to analyse '+esc(rangeLabel(w.from,w.to))+' … (up to a minute)</p>'; $('ai').disabled=true;
  try{ const r=await postJSON(base+'/api/report',{station,from:w.from,to:w.to});
    if(r.ok){lastReport=r.body.report;repRange={from:w.from,to:w.to};$('report').innerHTML='<p class="sub">'+esc(station)+' · '+esc(rangeLabel(w.from,w.to))+'</p>'+md(r.body.report)}
    else $('report').innerHTML='<p style="color:var(--bad)">'+esc(r.body.error||'Report failed')+'</p>';
  }catch(e){} finally{$('ai').disabled=false; card.scrollIntoView({behavior:'smooth',block:'nearest'})}
}

async function openAdmin(){
  const r=await getJSON(base+'/api/admin/sessions');
  $('keyState').textContent=r.gemini_key_set?('Key saved ('+r.gemini_key_hint+'). Paste a new one to replace it.'):'No key saved yet. Get one free at aistudio.google.com, then paste it here.';
  $('gModel').value=r.gemini_model;
  $('sessRows').innerHTML=r.sessions.map(s=>`<tr><td>${esc(s.user)}${s.you?' <span class="badge">this device</span>':''}</td><td>${fmtDT(s.created)}</td><td>${s.remember?fmtD(s.exp):'12 h idle'}</td><td class="sub" title="${esc(s.ua)}">${esc(s.ip)} · ${esc(s.ua.slice(0,38))}</td><td><button data-id="${esc(s.id)}">Sign out</button></td></tr>`).join('');
  $('sessRows').querySelectorAll('button').forEach(b=>b.onclick=async()=>{await postJSON(base+'/api/admin/revoke',{id:b.dataset.id});openAdmin()});
  if(!$('adminDlg').open) $('adminDlg').showModal();
}

me=await getJSON(base+'/api/me');
document.body.classList.toggle('admin',me.role==='admin');
$('me').textContent=me.name; $('role').textContent=me.role==='admin'?'admin':'viewer'; $('role').classList.toggle('admin',me.role==='admin');
document.querySelectorAll('#presets button').forEach(b=>b.onclick=()=>preset(b.dataset.p));
$('station').onchange=async()=>{station=$('station').value;await loadStations();preset('live');liveCards()};
$('day').onchange=()=>{const v=$('day').value;if(!v)return;const s=fromInput(v);setWindow('day',s,s+86400)};
$('go').onclick=()=>{const a=fromInput($('from').value),b=fromInput($('to').value);if(a&&b&&b>a)setWindow('custom',a,b)};
$('back').onclick=()=>{const w=view||win,s=w.to-w.from;setWindow('custom',w.from-s,w.to-s)};
$('fwd').onclick=()=>{const w=view||win,s=w.to-w.from;const b=Math.min(now(),w.to+s);setWindow('custom',b-s,b)};
$('reset').onclick=()=>{view=null;load(win.from,win.to)};
window.__gp={get plot(){return plot},get win(){return win},get data(){return data},md,setRep:r=>{repRange=r}};   // read-only handle for debugging/tests
$('zIn').onclick=()=>zoomBy(0.6); $('zOut').onclick=()=>zoomBy(1.6);
initSlider(); initOrb();
$('report').addEventListener('click',e=>{const a=e.target.closest('a.tjump'); if(!a) return; e.preventDefault(); jumpTo(a.dataset.md,a.dataset.hm)});
$('detClose').onclick=()=>setDetails(false); $('detBtn').onclick=()=>setDetails(true);
{let saved='1';try{saved=localStorage.getItem('gp_details')||'1'}catch(e){} setDetails(saved==='1');}
$('dl').onclick=()=>{const w=view||win;location.href=`${base}/api/export?station=${encodeURIComponent(station)}&from=${w.from}&to=${w.to}`};
$('ai').onclick=aiReport;
$('copyRep').onclick=()=>navigator.clipboard&&navigator.clipboard.writeText(lastReport);
$('adminBtn').onclick=openAdmin; $('closeAdmin').onclick=()=>$('adminDlg').close();
$('saveKey').onclick=async()=>{const k=$('gKey').value.trim(),m=$('gModel').value.trim();const body={};if(k)body.gemini_key=k;if(m)body.gemini_model=m;
  const r=await postJSON(base+'/api/admin/settings',body);$('gKey').value='';$('keyState').textContent=r.ok?'Saved.':(r.body.error||'Could not save');if(r.ok)openAdmin()};
$('revokeAll').onclick=async()=>{if(!window.confirm('Sign out every device, including this one?'))return;await postJSON(base+'/api/admin/revoke',{all:true});location.href=base+'/login'};
addEventListener('resize',()=>{clearTimeout(window._rz);window._rz=setTimeout(()=>{if(plot)plot.setSize({width:$('chart').clientWidth-12,height:plot.height})},150)});
await loadStations(); preset('live'); liveCards(); loadInsight(); setInterval(liveCards,5000); setInterval(loadStations,60000);
})();
"""


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "hash":
        import getpass
        print(hash_password(getpass.getpass("Password to hash: ")))
        raise SystemExit
    if not TOKEN:
        raise SystemExit("Set GRIDPRINT_TOKEN first")
    start()
    print(f"GridPrint server on port {PORT}, data in {DATA}, {len(USERS)} users")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
