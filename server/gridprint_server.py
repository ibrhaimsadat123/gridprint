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
from types import SimpleNamespace
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


# ================================================================== backup (inverter/UPS) detection
class BackupDetector:
    """The real grid never holds still: it wanders by several mHz every minute. An inverter or UPS makes its
    own 50 Hz from a crystal, so the reading goes flat (within ~2 mHz). We look at 30-second blocks; four or
    more flat blocks in a row at the same level (2 minutes) mean the socket is on backup power."""
    B, NEED, TOL, MINN = 30, 4, 0.003, 15

    def __init__(self):
        self.blk = None; self.raw = []; self.prev_raw = []; self.last_closed = None
        self.run = []; self.run_mean = None; self.run_prev = []; self.spans = []

    @staticmethod
    def _med(raw):
        v = sorted(f for _, f in raw); return v[len(v) // 2]

    def _flat(self, raw):
        if len(raw) < self.MINN:
            return False
        v = sorted(f for _, f in raw); k = 2 if len(v) >= 20 else 1        # ignore one or two stray readings
        return v[-1 - k] - v[k] <= self.TOL

    def feed(self, t, f):
        b = int(t // self.B)
        if self.blk is not None and b != self.blk:
            self._close(self.raw, self.blk); self.raw = []
        self.blk = b; self.raw.append((t, f))

    def _close(self, raw, blk):
        contiguous = self.last_closed is not None and blk == self.last_closed + 1
        self.last_closed = blk
        if self.run and not contiguous:
            self._end(None)
        med = self._med(raw) if raw else None
        if raw and self._flat(raw) and (not self.run or abs(med - self.run_mean) <= self.TOL):
            if not self.run:
                self.run_mean = med; self.run_prev = self.prev_raw if contiguous else []
            self.run.append(raw)
        else:
            if self.run:
                self._end(raw)
            if raw and self._flat(raw):                                   # a flat block at a new level starts a new run
                self.run_mean = med; self.run_prev = self.prev_raw if contiguous else []; self.run = [raw]
        self.prev_raw = raw

    def _bounds(self, nxt):
        m = self.run_mean; start = self.run[0][0][0]; end = self.run[-1][-1][0]
        for t, f in reversed(self.run_prev):                             # walk back to the exact switch-over second
            if abs(f - m) > self.TOL: break
            start = t
        for t, f in (nxt or []):
            if abs(f - m) > self.TOL: break
            end = t
        return start, end

    def _end(self, nxt):
        if len(self.run) >= self.NEED:
            a, b = self._bounds(nxt)
            self.spans.append((a, b, round(self.run_mean, 4), False))
        self.run = []

    def current(self):
        """Closed spans plus the one still running (if the socket is on backup right now)."""
        out = list(self.spans)
        if self.run and len(self.run) + (1 if self._flat(self.raw) else 0) >= self.NEED:
            a, b = self._bounds(self.raw)
            out.append((a, b, round(self.run_mean, 4), True))
        return out


_bk_cache = {}


def day_backup_spans(station, day):
    d = get_day(station, day)
    if not d or not d.t:
        return []
    with lock:
        c = _bk_cache.get((station, day))
        if c is None or c["t"] is not d.t or c["n"] > len(d.t):
            c = _bk_cache[(station, day)] = {"t": d.t, "n": 0, "det": BackupDetector()}
        det, t, f, s = c["det"], d.t, d.f, d.s
        for k in range(c["n"], len(t)):
            if s[k] == 0:
                det.feed(t[k], f[k])
        c["n"] = len(t)
        if len(_bk_cache) > 40:
            _bk_cache.pop(next(iter(_bk_cache)))
        return det.current()


def backup_spans(station, t0, t1):
    out = []
    for day in days_between(t0, t1):
        for a, b, m, live in day_backup_spans(station, day):
            if b >= t0 and a <= t1:
                out.append((a, b, m, live))
    return out


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
    bks = backup_spans(station, t0, t1); bk_i = 0; backup_s = 0      # inverter/UPS stretches: shown, but kept out of grid stats

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
        while bk_i < len(bks) and bks[bk_i][1] < t:
            bk_i += 1
        on_bk = s == 0 and bk_i < len(bks) and bks[bk_i][0] <= t
        if on_bk:
            backup_s += 1
        elif s == 0:
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
                  "min_t": lo_t, "max_t": hi_t, "outage_s": outage_s, "glitches": glitches, "backup_s": backup_s,
                  "coverage": round(total / max(1.0, t1 - t0), 4)},
        "events": [[round(a, 1), round(b, 1), c] for a, b, c in events[-300:]],
        "backup": [[round(a, 1), round(b, 1), m, live] for a, b, m, live in bks[-200:]],
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
    bk = [(a - 5 - b, e + 5) for a, e, _, _ in r.get("backup", [])]      # inverter stretches are not the grid
    pts = [(t, m) for t, m in zip(r["t"], r["mean"]) if m is not None and not any(a <= t <= e for a, e in bk)]
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
        "on_backup_power_pkt": [{"from": pk(a), "to": pk(b) + (" (still on backup)" if live else ""), "seconds": round(b - a),
                                 "inverter_reading_hz": m} for a, b, m, live in r.get("backup", [])][-40:],
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
LOW" or "flickering" are hardware faults, not grid events, so leave them out of the story.
on_backup_power_pkt lists stretches where the mains was off locally and the socket ran on an inverter/UPS:
the reading went flat because it was the inverter's own clock, NOT the grid. Never describe those flat
stretches as grid behaviour; mention them as local power cuts (likely load-shedding) instead. Mention data
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


# ================================================================== live insight (the orb): one probable cause
INSIGHT_WINDOW = 1800          # look back 30 minutes
PKT = timezone(timedelta(hours=5))


def _hm12(t):
    d = datetime.fromtimestamp(t, tz=PKT)
    return f"{d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def _dur(s):
    s = int(s)
    return f"{s} s" if s < 90 else f"{round(s / 60)} min" if s < 5400 else f"{s / 3600:.1f} h"


# Each rule: (weight, test, sentence). The highest-weight rule that matches wins. Tests read the context `c`:
#   c.ev   the move being explained (None when the grid is quiet): dir (+1 rise / -1 fall), d (Hz), sl (mHz/min),
#          mins, h (PKT hour as a float), on_hour, on_half, f0, f1, step, ramp, rebound, after (an earlier opposite
#          move this one answers), stays (still away from 50 Hz at the end)
#   c.h, c.wd (0 = Monday), c.mo, c.winter, c.summer, c.monsoon, c.sr / c.ss (minutes from sunrise / sunset,
#   negative = before), c.f (now), c.s5 (mHz/min over the last 5 min), c.cyc (repeating cycle minutes), c.wide (%)
def _between(x, a, b):
    return x is not None and a <= x < b


E = lambda c: c.ev
DN = lambda c: c.ev and c.ev.dir < 0
UP = lambda c: c.ev and c.ev.dir > 0
STEP = lambda c: c.ev and c.ev.step
RAMP = lambda c: c.ev and c.ev.ramp

CAUSE_RULES = [
    # ---- sudden falls
    (99, lambda c: DN(c) and STEP(c) and c.ev.f1 < 49.5 and c.ev.d >= 0.25,
     "Frequency crashed to {f1} Hz, into the zone where automatic under-frequency relays start shedding load. Something large left the grid at once: most likely a big power plant or an HVDC pole tripping while reserves were thin."),
    (97, lambda c: DN(c) and STEP(c) and c.ev.d >= 0.4,
     "A {d} Hz collapse in about {mins} is far too fast for normal demand. Most likely a large unit (nuclear, coal or RLNG) or one pole of the Matiari-Lahore HVDC link tripped."),
    (86, lambda c: DN(c) and STEP(c) and c.winter and _between(c.ev.h, 0, 10) and c.ev.d >= 0.15,
     "A sudden {d} Hz drop on a winter morning. Fog and smog often cause insulator flashovers on 500/220 kV lines in Punjab at this time of year, and a tripped line can cut a power plant off from the grid."),
    (86, lambda c: DN(c) and STEP(c) and c.monsoon and c.ev.d >= 0.15,
     "A sudden {d} Hz drop in the monsoon season. Storms commonly trip transmission lines, cutting generation off from the load."),
    (93, lambda c: DN(c) and STEP(c) and c.ev.rebound and c.ev.d >= 0.15,
     "A V-shaped dip: a generator or line probably tripped at {t}, frequency fell {d} Hz, and spinning reserve plus turbine governors pulled it back within minutes."),
    (92, lambda c: DN(c) and STEP(c) and c.ev.on_hour and 0.08 <= c.ev.d < 0.4,
     "Load-shedding blocks ending on the hour: at {t} feeders were switched back on together, and the returning load pulled frequency down {d} Hz."),
    (91, lambda c: DN(c) and STEP(c) and c.ev.stays and c.ev.f1 < 49.9 and c.ev.d >= 0.15,
     "A generator likely tripped at {t} and has not been replaced yet: frequency fell {d} Hz and is still sitting low at {f1} Hz, so reserves look thin."),
    (90, lambda c: DN(c) and STEP(c) and c.ev.on_half and 0.08 <= c.ev.d < 0.3,
     "Feeders reconnected on the half hour: a fast {d} Hz fall at {t} fits a load-shedding block ending and its load coming back at once."),
    (90, lambda c: DN(c) and STEP(c) and _between(c.ev.h, 4.5, 6),
     "A pre-dawn drop at {t}: the night load-shedding block ending, or household water pumps starting for the day, brought load on quickly."),
    (89, lambda c: DN(c) and STEP(c) and _between(c.ss, -20, 50) and c.ev.d >= 0.1,
     "A sharp drop at {t}, right around sunset: lights coming on and rooftop solar vanishing at the same moment hit the grid faster than plants could respond."),
    (88, lambda c: DN(c) and STEP(c) and _between(c.ev.h, 6, 9),
     "Morning surge: water pumps, heaters and geysers switching on (often right after the morning load-shedding block) pulled frequency down {d} Hz."),
    (86, lambda c: DN(c) and STEP(c) and any(_between(c.ev.h, a, b) for a, b in ((7.75, 8.25), (15.75, 16.25), (23.75, 24), (0, 0.25))),
     "Industrial shift change around {t}: factories restarting heavy motors and furnaces add load in a block, pulling frequency down {d} Hz."),
    (84, lambda c: DN(c) and STEP(c) and c.ev.d >= 0.15,
     "A sudden {d} Hz drop at {t}: most likely a power plant or a major transmission line tripping offline."),
    (82, lambda c: DN(c) and STEP(c) and _between(c.ev.h, 22, 24),
     "A late-night drop at {t}: a power plant tripping, or plants ramping down for the night faster than planned."),
    (80, lambda c: DN(c) and STEP(c) and c.ev.d >= 0.08,
     "A quick {d} Hz dip at {t}: probably a mid-size generator tripping or a large industrial load (a steel mill or arc furnace) switching on."),
    (78, lambda c: DN(c) and STEP(c) and _between(c.ev.h, 11, 14.5),
     "A quick midday dip at {t}: possibly clouds passing over rooftop-solar areas, so homes suddenly pull more power from the grid."),
    (74, lambda c: DN(c) and STEP(c),
     "A small, quick dip at {t}: likely one big load starting or a small generator easing off. Nothing serious."),
    # ---- sudden rises
    (98, lambda c: UP(c) and STEP(c) and c.ev.f1 > 50.5 and c.ev.d >= 0.25,
     "Frequency shot up to {f1} Hz: a large block of demand vanished, most likely a 500/220 kV grid station or line tripping and disconnecting a whole area's customers."),
    (96, lambda c: UP(c) and STEP(c) and c.ev.d >= 0.4,
     "A {d} Hz surge in about {mins}: a big chunk of demand disappeared at once, probably a major line or grid-station trip taking customers offline."),
    (93, lambda c: UP(c) and STEP(c) and c.ev.on_hour and c.ev.d >= 0.08,
     "Load-shedding block starting: at {t} distribution companies cut feeders together, and the lost load pushed frequency up {d} Hz."),
    (92, lambda c: UP(c) and STEP(c) and c.ev.rebound,
     "A spike and return at {t}: a large load or a line dropped off briefly, then reconnected (or plants backed down) within minutes."),
    (90, lambda c: UP(c) and STEP(c) and c.ev.on_half and c.ev.d >= 0.08,
     "Feeders switched off on the half hour: a {d} Hz jump at {t} fits a load-shedding block starting."),
    (90, lambda c: UP(c) and STEP(c) and c.ev.stays and c.ev.f1 > 50.15,
     "Frequency jumped {d} Hz at {t} and is staying high at {f1} Hz: load was lost and plants have not yet been told to back down."),
    (89, lambda c: UP(c) and STEP(c) and _between(c.ss, 0, 150),
     "An evening jump at {t}: utilities often shed feeders during the evening peak, and the sudden loss of load pushes frequency up."),
    (88, lambda c: UP(c) and STEP(c) and c.wd == 4 and _between(c.ev.h, 12.75, 13.75),
     "Friday prayers: shops, offices and factories shut for Jumu'ah around {t}, and the sudden fall in demand pushed frequency up."),
    (83, lambda c: UP(c) and STEP(c) and c.winter and _between(c.ev.h, 0, 10) and c.ev.d >= 0.15,
     "A sudden jump on a winter morning: fog may have tripped a line feeding a load centre, cutting its customers off the grid."),
    (84, lambda c: UP(c) and STEP(c) and _between(c.ev.h, 0, 6),
     "A night-time jump at {t}: a group of feeders switched off (scheduled night load-shedding) or a large industrial load stopping at shift end."),
    (82, lambda c: UP(c) and STEP(c) and c.ev.d >= 0.15,
     "A sudden {d} Hz rise at {t}: a large block of load dropped off, from a line trip or feeders being shed."),
    (78, lambda c: UP(c) and STEP(c) and c.ev.d >= 0.08,
     "A quick {d} Hz rise at {t}: a large industrial load stopping, or a group of feeders being switched off."),
    (75, lambda c: UP(c) and STEP(c) and c.ev.d < 0.1 and c.ev.rebound,
     "A brief blip at {t}: a large motor or load stopping and restarting."),
    (72, lambda c: UP(c) and STEP(c),
     "A small, quick rise at {t}: one big load switching off or a plant briefly overshooting. Nothing serious."),
    # ---- answers to an earlier move (recovery, overshoot)
    (94, lambda c: UP(c) and c.ev.after and c.ev.f1 > 50.05,
     "Recovery with overshoot: after the fall at {pt}, plants raised output and reserves came in, pushing frequency past 50 Hz to {f1} Hz before it settles."),
    (92, lambda c: UP(c) and c.ev.after,
     "Recovery: generators and the control centre (NPCC) are making up the shortfall from {pt}, pulling frequency back toward 50 Hz."),
    (93, lambda c: DN(c) and c.ev.after and c.ev.f1 < 49.95,
     "Overcorrection: after the surplus at {pt}, plants backed down too hard and frequency undershot to {f1} Hz."),
    (91, lambda c: DN(c) and c.ev.after,
     "Settling back: plants are being backed down after the surplus at {pt}."),
    # ---- slow falls (demand outpacing generation)
    (93, lambda c: DN(c) and RAMP(c) and _between(c.ss, -10, 60),
     "Evening peak building: lights come on around sunset ({ss}) while rooftop solar disappears, and demand is rising faster ({sl} mHz/min) than plants can follow."),
    (92, lambda c: DN(c) and RAMP(c) and _between(c.ev.h, 5, 7.5),
     "Early-morning ramp: household water pumps, geysers and heaters switch on around dawn, and demand is rising faster ({sl} mHz/min) than plants ramp up."),
    (91, lambda c: DN(c) and RAMP(c) and _between(c.ev.h, 7.5, 9.5),
     "Morning ramp: offices, schools, shops and factories opening between 8 and 9 push demand up faster than generation follows ({sl} mHz/min)."),
    (90, lambda c: DN(c) and RAMP(c) and _between(c.ss, -100, -10),
     "Solar fade: rooftop solar output drops in the late afternoon, so homes pull more from the grid, and frequency sags while plants ramp up to cover it."),
    (88, lambda c: DN(c) and RAMP(c) and c.winter and _between(c.ev.h, 18, 21.5),
     "Winter evening peak: lighting, electric heaters and cooking keep demand climbing faster than plants follow."),
    (88, lambda c: DN(c) and RAMP(c) and c.summer and _between(c.ev.h, 18, 22.5),
     "Summer evening peak: air-conditioners plus lighting push demand up after sunset."),
    (87, lambda c: DN(c) and RAMP(c) and c.ev.d >= 0.25 and c.ev.mins >= 10,
     "A long, deep slide of {d} Hz over {mins}: demand climbed steadily while generation lagged behind, typical of a peak building up."),
    (86, lambda c: DN(c) and RAMP(c) and c.summer and _between(c.ev.h, 12, 15.5),
     "Afternoon air-conditioning load: demand is rising in the summer heat faster than plants ramp up."),
    (85, lambda c: DN(c) and RAMP(c) and c.wd == 4 and _between(c.ev.h, 14, 15.5),
     "After Friday prayers, shops and offices reopening raise demand again."),
    (85, lambda c: DN(c) and RAMP(c) and _between(c.ev.h, 9.5, 11),
     "Late-morning demand still climbing (commercial and industrial load) before rooftop solar reaches its peak."),
    (84, lambda c: DN(c) and RAMP(c) and c.ev.sl >= 40,
     "A steep slide ({sl} mHz/min): a big block of load is coming on, or a plant is losing output steadily (for example a drop in gas supply)."),
    (80, lambda c: DN(c) and RAMP(c) and _between(c.ev.h, 0, 4.5),
     "A slow night-time sag: a plant may be ramping down for the night faster than demand falls, or a unit went out for maintenance."),
    (78, lambda c: DN(c) and RAMP(c) and _between(c.ev.h, 22, 24),
     "Late evening: a power plant cutting output for the night ahead of demand."),
    (76, lambda c: DN(c) and RAMP(c),
     "Demand is gradually outpacing generation ({sl} mHz/min); the control centre will bring more plants up."),
    # ---- slow rises (generation outpacing demand)
    (92, lambda c: UP(c) and RAMP(c) and _between(c.sr, 0, 120),
     "Sunrise effect: rooftop solar waking up after sunrise ({sr}) cuts how much homes and businesses draw from the grid, so frequency drifts up."),
    (91, lambda c: UP(c) and RAMP(c) and _between(c.ev.h, 10.5, 14),
     "Midday solar surplus: rooftop solar is near its peak, demand on the grid falls, and plants have not backed down enough."),
    (90, lambda c: UP(c) and RAMP(c) and _between(c.ev.h, 22, 24),
     "Late-night demand falling as homes and shops switch off; plants are ramping down more slowly than load drops."),
    (90, lambda c: UP(c) and RAMP(c) and _between(c.ev.h, 0, 4.5),
     "Night-time valley: demand is at its lowest and generation has not been trimmed enough."),
    (87, lambda c: UP(c) and RAMP(c) and c.ev.d >= 0.25 and c.ev.mins >= 10,
     "A long climb of {d} Hz over {mins}: demand fell away for a sustained period (the end of a peak) and plants were slow to back down."),
    (86, lambda c: UP(c) and RAMP(c) and _between(c.ev.h, 9, 10.5),
     "The morning rush is over: demand is flattening while plants started up for it are still running."),
    (85, lambda c: UP(c) and RAMP(c) and _between(c.ev.h, 19.5, 22),
     "Evening demand easing past its peak as people switch off lights and appliances."),
    (84, lambda c: UP(c) and RAMP(c) and c.wd == 4 and _between(c.ev.h, 12.5, 14),
     "Friday prayers: demand eases as shops and offices close for Jumu'ah."),
    (84, lambda c: UP(c) and RAMP(c) and c.ev.sl >= 40,
     "A steep climb ({sl} mHz/min): load is falling away quickly, possibly a large group of feeders being shed, or plants were dispatched too hard."),
    (83, lambda c: UP(c) and RAMP(c) and c.wd == 6 and _between(c.ev.h, 9, 15),
     "Sunday lull: with offices and many factories shut, demand stays low and frequency drifts high."),
    (80, lambda c: UP(c) and RAMP(c) and _between(c.ev.h, 16, 17.5),
     "Offices and factories closing in the late afternoon take load off the grid before the evening peak starts."),
    (76, lambda c: UP(c) and RAMP(c),
     "Generation is gradually outpacing demand ({sl} mHz/min); the control centre will start backing plants down."),
    # ---- rhythms and swings
    (88, lambda c: c.cyc and 25 <= c.cyc <= 35,
     "A half-hourly rhythm: the swings match feeders being rotated on a half-hour load-shedding schedule."),
    (88, lambda c: c.cyc and 55 <= c.cyc <= 65,
     "An hourly rhythm: the swings match load-shedding blocks and dispatch changes that happen on the hour."),
    (87, lambda c: c.cyc and 2 <= c.cyc < 8,
     "The line is swinging in a regular ~{cyc}-minute cycle, typical of automatic generation control or a big plant's governor 'hunting' around its setpoint."),
    (86, lambda c: c.cyc and 8 <= c.cyc < 25,
     "A ~{cyc}-minute rhythm suggests scheduled switching, such as rotating agricultural tube-well feeders or dispatch instructions every quarter hour."),
    (80, lambda c: c.wide >= 30,
     "Frequency is swinging wide (outside ±0.2 Hz {wide}% of the time): the grid is short of fast reserve, so every load change shows up strongly."),
    # ---- quiet states (no big move)
    (85, lambda c: not E(c) and c.f > 50.3,
     "Holding high at {f} Hz: a large surplus. Usually low demand (night, a holiday or strong rooftop solar) with plants not yet backed down."),
    (85, lambda c: not E(c) and c.f < 49.7,
     "Holding low at {f} Hz: a sustained shortfall, likely fuel limits (gas or RLNG) or plants out of service. More load-shedding is likely."),
    (82, lambda c: not E(c) and 50.15 < c.f <= 50.3,
     "Running a little high at {f} Hz: generation slightly exceeds demand, which the grid often allows in quiet periods."),
    (82, lambda c: not E(c) and 49.7 <= c.f < 49.85,
     "Running a little low at {f} Hz: demand slightly exceeds generation."),
    (70, lambda c: not E(c) and c.s5 > 8,
     "Drifting up slowly ({s5} mHz/min): demand is easing a little faster than plants follow."),
    (70, lambda c: not E(c) and c.s5 < -8,
     "Drifting down slowly ({s5} mHz/min): demand is creeping up a little faster than plants follow."),
    (60, lambda c: True,
     "Calm: no big changes in the last 30 minutes. Generation is tracking demand closely."),
]


def _context(station, now, feat, f_now, slope5):
    c = SimpleNamespace()
    d = datetime.fromtimestamp(now, tz=PKT)
    c.h, c.wd, c.mo = d.hour + d.minute / 60, d.weekday(), d.month
    c.winter, c.summer, c.monsoon = c.mo in (11, 12, 1, 2), c.mo in (5, 6, 7, 8), c.mo in (7, 8, 9)
    sr, ss = sun_times_pkt(d.date())
    tomin = lambda s: int(s[:2]) * 60 + int(s[3:])
    c.sr_s, c.ss_s = sr, ss
    c.f, c.s5 = f_now, slope5
    cyc = feat.get("repeating_cycle")
    c.cyc = cyc["period_minutes"] if cyc else None
    b = feat.get("percent_time_in_band", {})
    c.wide = round(b.get("0.2_to_0.5", 0) + b.get("beyond_0.5", 0))
    moves = [m for m in feat.get("moves_between_turning_points", []) if abs(m["change_hz"]) >= 0.04]
    c.ev = None; c.sr = c.ss = None
    if moves:
        tsec = lambda s: datetime.strptime(f"{d.year}-{s}", "%Y-%m-%d %H:%M").replace(tzinfo=PKT).timestamp()
        recent = [m for m in moves if now - tsec(m["to"]) <= 720]
        m = recent[-1] if recent else max(moves, key=lambda x: abs(x["change_hz"]))
        i = moves.index(m)
        e = SimpleNamespace()
        e.dir = 1 if m["change_hz"] > 0 else -1
        e.d, e.sl, e.mins = abs(m["change_hz"]), abs(m["slope_mhz_per_min"]), m["minutes"]
        t0 = tsec(m["from"]); st = datetime.fromtimestamp(t0, tz=PKT)
        e.t, e.h = _hm12(t0), st.hour + st.minute / 60
        e.on_hour, e.on_half = st.minute in (58, 59, 0, 1, 2), st.minute in (28, 29, 30, 31, 32)
        e.f0, e.f1 = m["from_hz"], m["to_hz"]
        e.step = e.sl >= 60 and e.mins <= 3
        e.ramp = not e.step
        nxt = moves[i + 1] if i + 1 < len(moves) else None
        prv = moves[i - 1] if i > 0 else None
        e.rebound = bool(nxt and (nxt["change_hz"] > 0) != (e.dir > 0) and abs(nxt["change_hz"]) >= 0.5 * e.d
                         and tsec(nxt["from"]) - tsec(m["to"]) <= 120 and nxt["minutes"] <= 6)
        e.after = bool(prv and (prv["change_hz"] > 0) != (e.dir > 0) and abs(prv["change_hz"]) >= 0.08
                       and t0 - tsec(prv["to"]) <= 180 and abs(prv["change_hz"]) >= 0.6 * e.d)
        e.pt = _hm12(tsec(prv["from"])) if prv else ""
        e.stays = not nxt
        c.ev = e
        c.sr = (st.hour * 60 + st.minute) - tomin(sr)
        c.ss = (st.hour * 60 + st.minute) - tomin(ss)
    else:
        c.sr = d.hour * 60 + d.minute - tomin(sr)
        c.ss = d.hour * 60 + d.minute - tomin(ss)
    return c


def probable_cause(c):
    best = None
    for w, test, text in CAUSE_RULES:
        try:
            if test(c) and (best is None or w > best[0]):
                best = (w, text)
        except Exception:
            continue
    w, text = best
    e = c.ev
    sr12 = lambda s: f"{int(s[:2]) % 12 or 12}:{s[3:]} {'AM' if int(s[:2]) < 12 else 'PM'}"
    vals = {"f": f"{c.f:.3f}", "s5": f"{c.s5:+.0f}", "cyc": f"{c.cyc:.0f}" if c.cyc else "", "wide": c.wide,
            "sr": sr12(c.sr_s), "ss": sr12(c.ss_s)}
    if e:
        vals.update({"d": f"{e.d:.2f}", "sl": f"{e.sl:.0f}", "mins": _dur(e.mins * 60), "t": e.t,
                     "f0": f"{e.f0:.3f}", "f1": f"{e.f1:.3f}", "pt": e.pt or "earlier"})
    try:
        sentence = text.format(**vals)
    except (KeyError, IndexError):
        sentence = text
    conf = "fairly likely" if w >= 90 else "possible" if w >= 78 else "a rough guess"
    return sentence, conf, w


def live_insight(station):
    now = time.time()
    res = {"station": station, "updated": now, "mood": "unknown", "cause": None, "basis": "", "confidence": ""}
    ls = last_seen.get(station) or 0
    if ls < now - 120:
        res["cause"] = "The station is offline: no readings in the last 2 minutes, so there is nothing to explain yet."
        return res
    bks = backup_spans(station, now - INSIGHT_WINDOW, now)
    if bks and bks[-1][3] and bks[-1][1] >= ls - 40:
        a, b, m, _ = bks[-1]
        res.update(mood="backup", confidence="almost certain", at=_hm12(a),
                   cause=f"The mains went off at {_hm12(a)} and this socket switched to an inverter or UPS, most likely a "
                         f"local power cut or load-shedding block. The flat {m:.3f} Hz line is the inverter's own clock, not the grid.",
                   basis=f"Reading flat within ±2 mHz for {_dur(b - a)} · real grid never holds that still")
        return res
    cur = range_query(station, now - 300, now, 30)
    pts = [(t, v) for t, v in zip(cur["t"], cur["mean"]) if v is not None]
    if len(pts) < 3:
        res["cause"] = "Not enough recent readings to judge."
        return res
    f_now = sum(v for _, v in pts[-3:]) / len(pts[-3:])
    slope5 = (pts[-1][1] - pts[0][1]) * 1000 / max(1, (pts[-1][0] - pts[0][0]) / 60)
    feat = grid_features(station, now - INSIGHT_WINDOW, now)
    c = _context(station, now, feat, f_now, slope5)
    sentence, conf, w = probable_cause(c)
    back = [x for x in bks if not x[3] and now - x[1] <= 900]
    if back:
        a, b, _, _ = back[-1]
        sentence = f"Mains power came back at {_hm12(b)} after {_dur(b - a)} on backup. " + sentence
    e = c.ev
    big = e and (e.d >= 0.3 or e.f1 < 49.6 or e.f1 > 50.4)
    res["mood"] = ("stressed" if big or f_now < 49.7 or f_now > 50.3 or c.wide >= 40
                   else "active" if e and e.d >= 0.1 else "calm")
    res.update(cause=sentence, confidence=conf, at=e.t if e else None,
               basis=(f"{'Rise' if e.dir > 0 else 'Fall'} of {e.d:.2f} Hz from {e.t} over {_dur(e.mins * 60)} "
                      f"({e.sl:.0f} mHz/min)" if e else f"Now {f_now:.3f} Hz, {slope5:+.0f} mHz/min"))
    return res


# ================================================================== HTTP plumbing
SEC_HEADERS = [
    ("Content-Security-Policy", "default-src 'none'; script-src 'self' https://cdn.jsdelivr.net; "
     "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; font-src https://cdn.jsdelivr.net; img-src 'self' data:; connect-src 'self'; "
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
                return redirect(PREFIX + "/explorer")
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
        return redirect(PREFIX + "/explorer", [cookie_header(token, REMEMBER_TTL if remember else None)])

    sess = session_get(req["cookies"].get(COOKIE))
    if path == PREFIX + "/logout":
        if method != "POST" or not same_origin(req):
            return Resp(403, "Forbidden")
        if sess:
            session_delete(token=req["cookies"].get(COOKIE))
        return redirect(PREFIX + "/login", [cookie_header("", 0)])

    # ---- public pages: the home page, its script and a coarse live pulse
    if method == "GET" and path in (PREFIX, PREFIX + "/", PREFIX + "/home"):
        return Resp(200, HOME_HTML.replace("__PREFIX__", PREFIX), "text/html; charset=utf-8")
    if method == "GET" and path == PREFIX + "/static/home.js":
        return Resp(200, HOME_JS, "application/javascript; charset=utf-8")
    if method == "GET" and path in (PREFIX + "/team", PREFIX + "/contact"):
        return Resp(200, TEAM_HTML.replace("__PREFIX__", PREFIX), "text/html; charset=utf-8")
    if method == "GET" and path == PREFIX + "/static/team.js":
        return Resp(200, TEAM_JS, "application/javascript; charset=utf-8")
    if method == "GET" and path == PREFIX + "/static/wave.js":
        return Resp(200, WAVE_JS, "application/javascript; charset=utf-8")
    if method == "GET" and path == PREFIX + "/static/ibrahim.webp":
        return Resp(200, IBRAHIM_WEBP, "image/webp")
    if method == "GET" and path == PREFIX + "/api/pulse":
        return J(public_pulse())

    # ---- everything below needs a valid session
    if not sess:
        if method == "GET" and path in (PREFIX + "/explorer", PREFIX + "/explorer/"):
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
    if path in (PREFIX + "/explorer", PREFIX + "/explorer/"):
        return Resp(200, APP_PAGE, "text/html; charset=utf-8")
    if path == PREFIX + "/static/app.js":
        return Resp(200, APP_JS, "application/javascript; charset=utf-8")
    if path == PREFIX + "/static/app-fx.js":
        return Resp(200, APP_FX, "application/javascript; charset=utf-8")
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
:root{--bg:#0A1120;--panel:#121D33;--line:#24324F;--ink:#EAF0F8;--mut:#8494B2;--acc:#F2A23A;--ok:#3DD68C;--bad:#F2555A;--warn:#FF8F6B;--disp:'Bricolage Grotesque Variable',system-ui,sans-serif}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 'IBM Plex Sans',system-ui,"Segoe UI",sans-serif}
.k{color:var(--acc);font:700 18px/1 var(--disp);font-stretch:80%;text-decoration:none;view-transition-name:brand}
.sub{color:var(--mut);font-size:12px}
button,select,input{font:inherit;color:var(--ink);background:#17233D;border:1px solid #2A3A5C;border-radius:8px;padding:6px 11px;cursor:pointer}
input{cursor:text;color-scheme:dark}button:hover{border-color:#4A5F8A}:focus-visible{outline:2px solid var(--acc);outline-offset:2px}button:disabled{opacity:.5;cursor:wait}
button.on,button.pri{background:var(--acc);border-color:var(--acc);color:#1A1205;font-weight:600}
a{color:var(--acc)}
"""


HOME_HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GridPrint · the hum that dates a recording</title>
<script src="__PREFIX__/static/wave.js"></script>
<meta name="description" content="GridPrint logs Pakistan's grid frequency every second, so a recording's mains hum can show when it was really made.">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource-variable/bricolage-grotesque@5.3.0/wdth.css" integrity="sha384-WQqnp9hDfoH3W8tz8N27r/2KCPxaxoS9L8KDqmXZdjhQOsnqVEV0GNrH+R6M0y4M" crossorigin="anonymous">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-sans@5.3.0/400.css" integrity="sha384-by8hCwJDxTOUHMGBI4elNkybAoX7Gs45s1pbgL9fdUTDy+tXTA+tZmiU+0LkwckA" crossorigin="anonymous">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-sans@5.3.0/600.css" integrity="sha384-1dcQop1Fsaw4/4DmZbwqeTSgod1K+Zkz3QWtbAUGxmw7ECfIGKOHUIRKcGtpdNHx" crossorigin="anonymous">
<style>
:root{--night:#0E1729;--deep:#0A1120;--panel:#15213A;--line:#24324F;--ink:#EAF0F8;--mut:#8494B2;--sodium:#F2A23A;--cyan:#6FD6FF;--violet:#A493FF;
 --disp:'Bricolage Grotesque Variable','Bricolage Grotesque',system-ui,sans-serif;--body:'IBM Plex Sans',system-ui,'Segoe UI',sans-serif}
*{box-sizing:border-box}
html{background:var(--deep);color:var(--ink);scroll-behavior:smooth}
body{margin:0;font:17px/1.6 var(--body);font-variant-numeric:tabular-nums}
a{color:inherit}
:focus-visible{outline:2px solid var(--sodium);outline-offset:3px;border-radius:4px}
.nav{position:fixed;inset:0 0 auto;z-index:20;display:flex;justify-content:space-between;align-items:center;padding:18px clamp(18px,4vw,48px);
 background:linear-gradient(var(--deep) 10%,transparent);pointer-events:none}
.nav>*{pointer-events:auto}
.mark{font:700 22px/1 var(--disp);font-stretch:80%;letter-spacing:-.01em;text-decoration:none;display:flex;align-items:center;gap:10px;view-transition-name:brand}
.mark svg{width:30px;height:18px}
.nav nav{display:flex;gap:6px}
.btn{display:inline-flex;align-items:center;gap:8px;font:600 15px/1 var(--body);padding:12px 18px;border-radius:999px;border:1px solid var(--line);
 background:rgba(21,33,58,.7);color:var(--ink);text-decoration:none;cursor:pointer;backdrop-filter:blur(6px);transition:border-color .2s,background .2s}
.btn:hover{border-color:var(--sodium)}
.btn.hot{background:var(--sodium);border-color:var(--sodium);color:#1A1205}
.btn.hot:hover{background:#FFB650}

/* the scroll sequence: one sticky stage, four steps scrolling past it */
.seq{position:relative;height:520vh}
.stage{position:sticky;top:0;height:100vh;overflow:hidden}
#terrain,#fx{position:absolute;inset:0;width:100%;height:100%;display:block}#fx{z-index:1;pointer-events:none}
.stage{background:radial-gradient(ellipse at 62% 55%,#13213D 0,#0A1120 70%)}
.w{display:inline-block;perspective:600px}.ln{overflow:hidden;padding-bottom:.06em}
html.lenis,html.lenis body{height:auto}.lenis.lenis-smooth{scroll-behavior:auto!important}
#wipe{position:fixed;inset:0;z-index:100;display:none;background:#0A1120;clip-path:inset(0 100% 0 0)}#wipe i{position:absolute;left:0;right:0;top:50%;height:2px;background:#F2A23A;box-shadow:0 0 24px #F2A23A;transform-origin:left}
.stage::after{content:"";position:absolute;inset:auto 0 0;height:30vh;background:linear-gradient(transparent,var(--deep));pointer-events:none}
.hero{position:absolute;z-index:2;left:clamp(18px,6vw,88px);bottom:clamp(40px,12vh,130px);max-width:min(860px,88vw);z-index:2;transition:opacity .5s,transform .5s}
.hero h1{margin:0;font:760 clamp(44px,7.4vw,112px)/.92 var(--disp);font-stretch:78%;letter-spacing:-.025em;text-wrap:balance}
.hero p{margin:22px 0 28px;max-width:34em;color:#C4CEE0;font-size:clamp(16px,1.5vw,19px)}
.cta{display:flex;flex-wrap:wrap;gap:10px}
.live{position:absolute;right:clamp(18px,4vw,48px);top:92px;z-index:2;text-align:right;color:var(--mut);font-size:14px;line-height:1.35}
.live b{display:block;color:var(--ink);font:650 clamp(30px,3.2vw,46px)/1.05 var(--disp);font-stretch:90%;margin-top:4px}
.live .st{display:inline-flex;align-items:center;gap:7px;margin-top:6px}
.live .st i{width:8px;height:8px;border-radius:50%;background:var(--sodium);box-shadow:0 0 0 0 rgba(242,162,58,.6);animation:ping 2s infinite}
.live.backup .st i{background:var(--violet);box-shadow:0 0 0 0 rgba(164,147,255,.6)}
@keyframes ping{70%{box-shadow:0 0 0 9px rgba(242,162,58,0)}100%{box-shadow:0 0 0 0 rgba(242,162,58,0)}}
.axis{position:absolute;left:clamp(18px,6vw,88px);top:96px;z-index:2;color:var(--mut);font-size:13px;max-width:24em}

.steps{position:absolute;inset:0;pointer-events:none;z-index:3}
.step{position:absolute;left:clamp(18px,6vw,88px);bottom:clamp(40px,14vh,140px);max-width:min(500px,86vw);opacity:0;transform:translateY(24px);transition:opacity .45s,transform .45s}
.step.on{opacity:1;transform:none}
.step .n{font:700 15px/1 var(--disp);color:var(--sodium);display:block;margin-bottom:12px}
.step h2{margin:0 0 12px;font:720 clamp(32px,4.4vw,60px)/1 var(--disp);font-stretch:82%;letter-spacing:-.02em}
.step p{margin:0;color:#C4CEE0}
.step.r{left:auto;right:clamp(18px,6vw,88px)}
.legend{position:absolute;right:clamp(18px,4vw,48px);bottom:28px;z-index:3;display:flex;gap:16px;color:var(--mut);font-size:13px;opacity:0;transition:opacity .4s}
.legend.on{opacity:1}
.legend span::before{content:"";display:inline-block;width:18px;height:2px;vertical-align:middle;margin-right:7px;background:currentColor}
.legend .s{color:var(--sodium)}.legend .c{color:var(--cyan)}

section.block{padding:clamp(70px,12vh,140px) clamp(18px,6vw,88px);max-width:1280px;margin:0 auto}
.block h2{margin:0 0 18px;font:740 clamp(36px,5vw,72px)/.98 var(--disp);font-stretch:80%;letter-spacing:-.02em;max-width:15em;text-wrap:balance}
.block>p{max-width:38em;color:#C4CEE0;margin:0 0 34px}
.figs{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:28px 40px;margin:0 0 36px;padding:0;list-style:none}
.figs li{border-top:1px solid var(--line);padding-top:14px;color:var(--mut);font-size:15px}
.figs b{display:block;color:var(--ink);font:680 clamp(34px,3.6vw,52px)/1 var(--disp);font-stretch:85%;margin-bottom:8px}
.chartwrap{position:relative;border:1px solid var(--line);border-radius:18px;background:var(--night);padding:16px 10px 8px}
#proof{width:100%;height:clamp(280px,42vw,460px);display:block}
.chartkey{display:flex;flex-wrap:wrap;gap:18px;padding:4px 12px 10px;color:var(--mut);font-size:14px}
.chartkey span::before{content:"";display:inline-block;width:20px;height:3px;border-radius:2px;vertical-align:middle;margin-right:8px;background:currentColor}
.chartkey .s{color:var(--sodium)}.chartkey .c{color:var(--cyan)}.chartkey .v::before{height:12px;width:14px;background:rgba(164,147,255,.35)}
.chartkey .v{color:var(--mut)}
#ptip{position:absolute;pointer-events:none;display:none;background:#0A1120ee;border:1px solid var(--line);border-radius:10px;padding:8px 11px;font-size:13px;line-height:1.5;white-space:nowrap}
.note{color:var(--mut);font-size:14px;margin:14px 4px 0;max-width:60em}

.split{display:grid;grid-template-columns:1.1fr 1fr;gap:clamp(30px,6vw,90px);align-items:start}
.split p{color:#C4CEE0;margin:0 0 16px}
.fine{list-style:none;margin:0;padding:0}
.fine li{padding:16px 0;border-top:1px solid var(--line)}
.fine li b{display:block;font:650 20px/1.2 var(--disp);margin-bottom:4px}
.fine li span{color:var(--mut);font-size:15px}
footer{padding:50px clamp(18px,6vw,88px) 60px;color:var(--mut);font-size:14px;display:flex;flex-wrap:wrap;gap:12px 30px;justify-content:space-between;border-top:1px solid var(--line)}
footer a{color:var(--ink)}
@media (max-width:760px){.nav .wide{display:none}.nav .btn{padding:10px 14px;font-size:14px}.hero h1{font-size:clamp(40px,11vw,56px)}.split{grid-template-columns:1fr}.live{top:76px}.axis{display:none}.step.r{left:clamp(18px,6vw,88px);right:auto}.legend{left:18px;right:auto}}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}html{scroll-behavior:auto}}
</style></head>
<body>
<header class="nav">
 <a class="mark" href="#top" aria-label="GridPrint home"><svg viewBox="0 0 30 18" aria-hidden="true"><path d="M1 9 Q4.5 1 8 9 T15 9 T22 9 T29 9" fill="none" stroke="#F2A23A" stroke-width="2.4" stroke-linecap="round"/></svg>GridPrint</a>
 <nav><a class="btn wide" href="#proof-sec">Proof</a><a class="btn wide" href="__PREFIX__/team" data-go>Team</a><a class="btn hot" href="__PREFIX__/login" data-go>Open the explorer</a></nav>
</header>
<main id="top">
<div class="seq" id="seq">
 <div class="stage">
  <canvas id="fx" aria-hidden="true"></canvas>
  <canvas id="terrain" aria-label="The last six hours of Pakistan's grid frequency, drawn as a landscape: each ridge is ten minutes, newest in front." role="img"></canvas>
  <div class="axis" id="axis">The last 6 hours of the grid, one ridge per 10 minutes. Newest in front.</div>
  <div class="live" id="live" aria-live="polite">Islamabad right now<b id="lv">–</b><span class="st"><i></i><span id="ls">connecting</span></span></div>
  <div class="hero" id="hero">
   <h1>Every plug in Pakistan hums the same changing note.</h1>
   <p>GridPrint writes that note down every second. A video or voice note that caught the hum can be checked against it: when it was really recorded, and whether anything was cut out.</p>
   <div class="cta"><a class="btn hot" href="#proof-sec">See it match a real video</a><a class="btn" href="__PREFIX__/login" data-go>Open the explorer</a></div>
  </div>
  <div class="steps" aria-hidden="false">
   <div class="step" data-s="1"><span class="n">1 of 4</span><h2>The grid never sits still.</h2><p>Power stations and the whole country's demand never balance exactly, so the frequency drifts around 50 Hz, by a few thousandths every second. That wandering line never repeats.</p></div>
   <div class="step r" data-s="2"><span class="n">2 of 4</span><h2>A station writes it down.</h2><p>A GridPrint station on a wall socket in Islamabad times every cycle of the mains and logs the frequency once a second. When the mains goes off and an inverter takes over, it notices that too.</p></div>
   <div class="step" data-s="3"><span class="n">3 of 4</span><h2>Recordings overhear it.</h2><p>Lights, fans and chargers leak the same hum into any microphone in the room. It is faint, but the wiggles are all there, carried at 100, 200 and 300 Hz.</p></div>
   <div class="step r" data-s="4"><span class="n">4 of 4</span><h2>Slide until the wiggles fit.</h2><p>Move the recording's hum along the station's log. Where the two lines lock together is when it was recorded. A cut or a splice breaks the fit.</p></div>
  </div>
  <div class="legend" id="legend"><span class="s">Station log</span><span class="c">Hum heard in a recording</span></div>
 </div>
</div>

<section class="block" id="proof-sec">
 <h2>A 50-minute iPhone video, matched to the second.</h2>
 <p>Recorded in a hostel room in Islamabad on 28 November 2026, default camera settings, nobody talking. The phone's microphone heard the grid; the station logged it from the wall. Then the power went out.</p>
 <ul class="figs">
  <li><b>0.92</b>how well the two lines agree (1.0 is identical; the best wrong time scores 0.49)</li>
  <li><b>0.005 Hz</b>average gap between the video's hum and the station</li>
  <li><b>&lt;1 s</b>between the time the hum points to and the time stored in the file</li>
  <li><b>8:22 AM</b>power cut: the video heard the inverter's flat 50.045 Hz too</li>
 </ul>
 <div class="chartwrap">
  <div class="chartkey"><span class="s">GridPrint station, from the wall socket</span><span class="c">Hum heard by the iPhone video</span><span class="v">Mains off, room on an inverter</span></div>
  <canvas id="proof" role="img" aria-label="Line chart from 8:02 to 8:52 AM. The station's frequency and the hum in the video follow the same path between 49.9 and 50.33 Hz, and both go flat at 50.045 Hz during the power cut from 8:22 to 8:27."></canvas>
  <div id="ptip"></div>
 </div>
 <p class="note">Hover the chart to compare the two at any second. Points where the phone's tracker lost the hum (about 7%) are left out rather than smoothed over.</p>
</section>

<section class="block">
 <div class="split">
  <div><h2>Built by two roommates, one socket at a time.</h2>
   <p>GridPrint started in a NUST hostel room in Islamabad, where Ibrahim Sadat (maths) and Abdullah Habib (mechanical engineering) asked whether the electricity itself could act as a clock that nobody can fake. The station is an Arduino, an optocoupler and a small transformer. The explorer, the inverter detection and the matching are open source.</p>
   <p style="display:flex;flex-wrap:wrap;gap:10px"><a class="btn hot" href="__PREFIX__/team" data-go>Meet the team</a><a class="btn" href="https://github.com/ibrhaimsadat123/gridprint">Read the code on GitHub</a></p></div>
  <ul class="fine">
   <li><b>Live explorer</b><span>Every second since the station went live, with zoom, outage and backup-power detection, and a short probable cause for every big swing.</span></li>
   <li><b>Recording check</b><span>Pull the hum out of an audio or video file and find where it fits in the log. WhatsApp-compressed copies are being tested next.</span></li>
   <li><b>More stations</b><span>A second station in Lahore would show whether both cities see the same line, which is what makes the clock trustworthy.</span></li>
  </ul>
 </div>
</section>
</main>
<footer><span>GridPrint, by Ibrahim Sadat and Abdullah Habib (NUST, Islamabad), for Hack Club Crossroads.</span><span style="display:flex;gap:22px"><a href="__PREFIX__/team" data-go>Team and contact</a><a href="__PREFIX__/login" data-go>Sign in</a></span></footer>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.15.1/dist/gsap.min.js" integrity="sha384-3vCAhD+a3BOliP3QqjQINn6ZWiZxoqu0WFxX5ZQZgqekDZuSb1mJBrrLMmu94H8f" crossorigin="anonymous" defer></script>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.15.1/dist/ScrollTrigger.min.js" integrity="sha384-H2WL1iy/jKQQ7MTHU2hskZKNHTYWWD/KgKE1vCNnaWY8An2ESTh23KN/fTp8XS3j" crossorigin="anonymous" defer></script>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.15.1/dist/SplitText.min.js" integrity="sha384-e98MX/fxZrUXA/6T+aCptryLfHgR2FpEmWxDbbPloHT0Qg79dEXKdLJ/iwD2lzxs" crossorigin="anonymous" defer></script>
<script src="https://cdn.jsdelivr.net/npm/lenis@1.3.29/dist/lenis.min.js" integrity="sha384-OHGPFWKcc+jP8Q7LpOJtmcyBSz6L1L/nH2wKWNQ456cvisoFPacxtBsDQPmwEjLu" crossorigin="anonymous" defer></script>
<script src="__PREFIX__/static/home.js" defer></script>
</body></html>
'''
HOME_JS = r'''// GridPrint home. WebGL landscape of the live grid (hand-written, ~5 KB instead of a 3D library),
// GSAP ScrollTrigger drives the camera, Lenis smooths the scroll, SplitText animates the type.
(()=>{
'use strict';
const PREFIX=document.querySelector('script[src*="/static/home.js"]').getAttribute('src').replace(/\/static\/home\.js.*$/,'');
const PROOF={"t0":1795834925,"dt":4.0,"st":[462,799,1163,1478,1747,1947,2104,2240,2339,2325,2253,2139,2022,1930,1876,1802,1716,1624,1526,1452,1425,1404,1390,1382,1430,1438,1387,1325,1310,1284,1260,1215,1169,1131,1059,959,881,841,798,808,797,789,812,861,861,834,762,667,581,496,403,360,242,179,154,119,103,120,112,102,149,202,251,218,150,64,8,-65,-114,-167,-215,-195,-193,-226,-261,-283,-362,-377,-372,-364,-312,-214,-103,2,119,204,298,345,351,434,559,671,758,816,816,847,894,901,921,921,869,835,771,679,598,584,552,588,592,595,765,857,959,1035,1087,1131,1169,1208,1251,1257,1350,1430,1506,1569,1596,1623,1672,1750,1778,1804,1780,1709,1618,1518,1390,1229,1030,854,692,516,335,190,76,-6,-34,25,98,133,207,288,358,433,439,503,556,690,790,872,884,896,912,928,930,928,911,884,923,1002,1026,1071,1112,1151,1171,1147,1131,1115,1084,1031,982,899,812,750,705,706,712,705,711,692,680,666,626,622,643,691,748,782,840,895,955,995,977,937,912,966,1003,1017,1021,1016,974,959,956,983,1024,1124,1221,1304,1371,1453,1503,1483,1454,1408,1334,1268,1083,1004,892,781,686,660,637,632,695,767,828,899,952,974,998,972,976,950,943,972,972,997,1030,1043,979,954,896,826,797,762,801,886,985,1063,1150,1236,1311,1381,1384,1374,1347,1298,1260,1231,1248,1248,1257,1291,1306,1307,1322,1316,1363,1408,1447,1463,1479,1490,1512,1544,1568,1629,1698,1754,1823,1869,1950,2029,2096,2144,2298,2403,2460,2498,2502,2404,2261,2064,1848,1639,1466,455,452,451,453,451,451,451,453,451,453,451,451,451,451,451,451,449,451,453,453,452,451,452,453,453,451,451,450,451,451,451,453,453,449,451,451,449,451,453,449,451,451,449,454,449,453,451,453,457,453,451,451,449,451,451,452,451,449,449,452,449,451,451,449,453,453,451,451,451,457,451,451,449,451,449,451,451,453,451,451,451,-1173,2285,2299,2322,2335,2319,2275,2246,2230,2171,2138,2131,2088,2010,1924,1859,1788,1711,1696,1664,1625,1490,1316,1136,963,779,608,408,259,127,68,32,8,38,80,109,126,156,251,325,312,325,325,518,655,748,852,923,952,1036,1106,1155,1168,1201,1196,1146,1088,1056,1045,1042,1055,1102,1162,1248,1301,1351,1442,1523,1588,1619,1686,1767,1895,2045,2249,2378,2681,2865,3007,3115,3156,3203,3142,3033,2954,2877,2686,2558,2406,2314,2234,2183,2239,2350,2534,2697,2847,2884,2908,2893,2841,2820,2851,2892,2987,3092,3192,3280,3318,3283,3254,3176,3043,2903,2757,2610,2458,2316,2253,2195,2163,2156,2139,2098,2059,2064,2049,2044,2041,2078,2124,2172,2233,2319,2388,2439,2483,2526,2567,2604,2678,2764,2864,2943,3003,3020,3040,3033,3026,3020,3002,2986,2946,2912,2857,2749,2584,2372,2142,1911,1699,1475,1282,1159,1053,954,863,778,765,753,597,526,575,659,805,929,1069,1160,1212,1271,1300,1326,1340,1293,1249,1200,1155,1056,974,907,859,811,723,599,439,261,98,-42,-144,-184,-204,-217,-232,-244,-280,-331,-383,-453,-532,-578,-613,-648,-693,-713,-715,-703,-701,-754,-774,-826,-878,-859,-833,-854,-890,-937,-1000,-1033,-1046,-1037,-981,-892,-858,-832,-812,-815,-839,-840,-847,-902,-962,-1021,-1068,-1067,-1067,-1070,-1081,-1085,-1045,-1025,-998,-990,-974,-981,-976,-920,-890,-868,-830,-788,-711,-685,-709,-816,-972,-1028,-1022,-995,-904,-862,-836,-768,-671,-601,-524,-470,-456,-465,-437,-413,-373,-284,-219,-200,-205,-237,-271,-317,-279,-203,-196,-194,-208,-236,-224,-200,-154,-149,-168,-210,-182,-118,-66,-10,49,68,82,67,55,61,54,14,-1,20,45,124,246,363,428,468,442,403,392,344,222,104,18,-40,-32,-22,58,177,283,406,494,600,712,799,889,935,899,868,837,819,824,821,824,814,782,735,628,506,414,335,263,195,131,67,7,-67,-132,-143,-109,-39,55,167,249,325,386,391,429,441,469],"v":[null,-4526,null,null,null,1993,2143,2245,2249,null,2089,2111,2021,null,null,375,null,null,null,1510,null,null,null,null,1432,1432,1383,1296,1279,1268,1264,1210,1143,1123,1055,960,868,814,819,795,796,806,815,850,858,810,724,694,542,475,402,323,225,173,152,117,111,125,122,123,170,213,242,220,112,61,-11,-60,-113,-169,-206,-210,-206,-225,-248,-354,-355,-360,-378,-351,-308,-196,-67,1,122,220,287,332,379,467,580,718,764,803,822,850,890,913,923,899,863,829,746,661,586,569,572,610,618,689,821,878,968,1036,1107,1121,1143,1251,1299,1328,1351,1428,1514,1565,1607,1612,1679,1745,1783,1794,1735,1706,1592,1508,1378,1161,1019,791,697,460,313,175,104,-15,-15,43,66,137,199,304,323,437,496,592,647,710,816,866,905,900,913,923,940,937,894,885,934,989,1029,1075,1117,1152,1143,1126,1138,1123,1058,995,963,879,808,736,715,698,713,714,684,690,686,632,659,633,640,691,740,804,842,901,949,980,980,926,952,954,966,1019,1025,989,980,952,962,968,1069,1089,1219,1349,1380,1452,1491,1474,1440,1393,1300,1226,1120,960,862,728,675,null,610,639,726,756,842,894,928,938,null,null,883,919,949,965,987,997,1027,1023,973,960,878,824,781,776,819,901,898,null,1140,1211,1326,1376,1391,1356,1350,1308,1235,1235,1227,null,1267,1258,1301,1328,1330,1332,1352,1407,1435,1470,1487,1495,1512,1549,1607,1631,1680,1753,1828,1921,2021,2042,2116,2178,2314,2420,2465,null,2465,2367,2259,2036,1804,1619,1509,433,453,435,428,434,453,503,516,493,460,451,449,449,463,443,446,460,455,456,456,452,456,452,450,459,453,462,454,455,443,452,461,459,458,449,446,460,462,457,460,452,447,452,452,451,451,452,461,461,457,453,444,463,429,448,471,450,449,448,451,456,451,446,462,439,449,455,460,464,462,451,464,458,458,460,462,459,451,467,475,465,null,2226,2311,2308,2345,2382,null,2222,2229,2159,2129,2104,2093,2006,1904,1840,1799,1724,1680,1647,1595,1464,1290,1099,937,768,579,399,250,121,56,34,17,43,98,106,134,193,259,307,329,352,437,538,678,695,null,null,1006,1083,1165,1222,1180,1181,1179,1103,1071,1055,1035,1042,1058,1094,1189,1210,1281,null,1616,1604,1579,1639,1709,1780,1902,2110,2249,2457,2747,2880,3032,3128,3144,3157,3141,3018,2942,2789,2667,2576,2388,2334,null,null,null,2421,2567,2702,2868,2887,2903,2888,2842,2827,2845,2926,3030,3106,3183,3266,3289,3287,3236,3159,3019,2900,2796,null,2406,2311,2258,2201,2151,2156,2130,2078,2060,2067,2056,2072,2065,2070,2064,2170,2261,null,2478,2483,2465,2545,2581,2614,2670,2815,2898,2950,2998,3050,3029,3017,3025,3019,3003,2989,2945,2898,2843,2726,2592,2297,2095,1887,1678,1369,1234,1108,1048,944,838,766,707,663,603,562,584,661,776,928,1063,1170,1232,1271,1308,1346,1319,1227,1148,1175,1104,1001,972,898,866,787,685,632,377,228,70,-51,-138,-178,-201,-217,-227,-250,-298,-321,-413,-459,-514,-587,-616,-638,-693,-710,-722,-706,-706,-731,-791,-810,-832,-843,-845,-864,-892,-960,-970,null,null,-986,-952,-897,-846,-841,-817,-834,-836,-839,-870,-888,-954,-1025,-1037,-1063,-1060,-1069,-1069,-1040,-1189,null,null,-1016,-991,-992,-950,-923,-899,-856,-818,-773,-703,-706,-722,-851,-986,-1013,-994,-992,null,null,null,-675,-655,-563,-534,-469,-415,null,-441,-427,-358,-283,-208,-204,-218,null,null,-332,null,null,-229,-228,-244,-251,-238,-89,null,59,-133,null,null,-29,-34,4,59,56,67,74,70,65,59,19,6,28,61,102,234,369,420,452,413,421,357,299,180,106,-35,-46,-22,-6,84,199,296,395,524,634,735,811,891,923,890,881,831,814,804,815,801,803,801,674,622,478,418,311,270,215,98,53,2,-57,null,null,-97,15,-23,null,null,null,null,null,null,null,-7188],"cut":[1218.2000000476837,1543.0999999046326]};
const C={deep:'#0A1120',sodium:'#F2A23A',cyan:'#6FD6FF',violet:'#A493FF',mut:'#8494B2',ink:'#EAF0F8'};
const RM=matchMedia('(prefers-reduced-motion: reduce)').matches;
const G=window.gsap, ST=window.ScrollTrigger, SPLIT=window.SplitText;
const $=id=>document.getElementById(id);
const clamp=(x,a,b)=>Math.max(a,Math.min(b,x)), lerp=(a,b,t)=>a+(b-a)*t;
const ease=t=>t<.5?4*t*t*t:1-Math.pow(-2*t+2,3)/2;
const seg=(p,a,b)=>clamp((p-a)/(b-a),0,1);
const PKT=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Karachi',hour:'numeric',minute:'2-digit'});
const PKTs=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Karachi',hour:'numeric',minute:'2-digit',second:'2-digit'});
const hex=h=>[parseInt(h.slice(1,3),16)/255,parseInt(h.slice(3,5),16)/255,parseInt(h.slice(5,7),16)/255];

// ---------------------------------------------------------------- live data
let pulse=null, rows=null, ymid=50, yspan=0.3, meshDirty=true;
async function loadPulse(){
  try{const r=await fetch(PREFIX+'/api/pulse',{cache:'no-store'}); if(!r.ok) throw 0; pulse=await r.json(); buildRows(); showLive()}
  catch(e){ $('ls').textContent='live data unavailable'; if(!rows) fakeRows() }
}
function showLive(){
  const p=pulse, live=$('live'), now=Date.now()/1000;
  if(!p||p.f_now==null||!p.last_seen||now-p.last_seen>120){$('lv').textContent='offline';$('ls').textContent=p&&p.last_seen?'last reading '+PKT.format(p.last_seen*1000):'station offline';return}
  const bk=(p.backup||[]).find(b=>b[3]&&b[1]>=p.last_seen-40);
  live.classList.toggle('backup',!!bk);
  const txt=bk?'on backup':p.f_now.toFixed(3)+' Hz';
  if($('lv').textContent!==txt){$('lv').textContent=txt; if(G&&!RM) G.fromTo('#lv',{yPercent:40,opacity:0},{yPercent:0,opacity:1,duration:.5,ease:'power3.out'})}
  $('ls').textContent=bk?'mains off since '+PKT.format(bk[0]*1000):(p.trend>0.004?'rising':p.trend<-0.004?'falling':'holding')+', updated '+Math.max(1,Math.round(now-p.last_seen))+' s ago';
}
function buildRows(){
  const p=pulse; if(!p||!p.f||!p.f.length) return fakeRows();
  const out=[], bk=p.backup||[];
  for(let i=0;i<p.f.length;i+=60){const r=[];
    for(let k=0;k<60;k++){const j=i+k, v=j<p.f.length&&p.f[j]!=null?50+p.f[j]/1e4:null, t=p.t0+j*p.step; r.push({v,b:bk.some(s=>t>=s[0]&&t<=s[1])})}
    out.push(r)}
  const vals=p.f.filter(x=>x!=null).map(x=>50+x/1e4);
  if(vals.length){const lo=Math.min(...vals),hi=Math.max(...vals); ymid=(lo+hi)/2; yspan=Math.max(0.12,(hi-lo)/2)}
  rows=out.slice(-36); meshDirty=true;
}
function fakeRows(){
  rows=[]; for(let r=0;r<36;r++){const row=[];for(let k=0;k<60;k++)row.push({v:50+0.12*Math.sin((r*60+k)/37)+0.05*Math.sin((r*60+k)/9),b:false});rows.push(row)}
  ymid=50; yspan=0.2; meshDirty=true; $('axis').textContent='Live data is unavailable right now, so this landscape is a placeholder shape.';
}

// ---------------------------------------------------------------- camera (shared by WebGL and the 2D overlay)
const glc=$('terrain'), fx=$('fx'), cx=fx.getContext('2d');
let W=0,H=0,DPR=1;
function camAt(p){
  const down=ease(seg(p,0.12,0.6)), mob=W<760;
  const yaw=RM?0:Math.sin(performance.now()/9000)*0.22;
  return {p,down,mob,pitch:lerp(0.62,0,down),yw:lerp(yaw*0.6-0.32,0,down),dist:lerp(3.9,3.1,down),
    f:mob?W*0.95:Math.min(W*0.62,H*1.05),ox:W*(mob?0.5:lerp(0.68,0.5,down)),oy:H*(mob?lerp(0.3,0.36,down):lerp(0.5,0.47,down)),
    focus:ease(seg(p,0.36,0.56)),flat:ease(seg(p,0.58,0.68))};
}
const xOf=k=>lerp(-1.55,1.55,k/59);
const zOf=(r,R)=>lerp(1.7,-0.9,R>1?r/(R-1):1);
function projector(c){const cy=Math.cos(c.yw),sy=Math.sin(c.yw),cp=Math.cos(c.pitch),sp=Math.sin(c.pitch);
  return (x,y,z)=>{const X=x*cy-z*sy, Z0=x*sy+z*cy, Y=y*cp+Z0*sp, Z=Z0*cp-y*sp, d=Z+c.dist; return [c.ox+X*c.f/d, c.oy-Y*c.f/d, d]}}

// ---------------------------------------------------------------- WebGL terrain
let gl=null, prog=null, buf={}, nFill=0, parts=null;
const VS=`attribute vec3 aPos;attribute vec3 aInfo;uniform vec4 uCam;uniform vec4 uView;uniform vec2 uRes;
varying vec3 vInfo;varying float vDepth;
void main(){float cy=cos(uCam.x),sy=sin(uCam.x),cp=cos(uCam.y),sp=sin(uCam.y);
 float X=aPos.x*cy-aPos.z*sy, Z0=aPos.x*sy+aPos.z*cy, Y=aPos.y*cp+Z0*sp, Z=Z0*cp-aPos.y*sp, d=Z+uCam.z;
 vec2 s=vec2(uView.x+X*uCam.w/d, uView.y-Y*uCam.w/d);
 gl_Position=vec4(s.x/uRes.x*2.0-1.0, 1.0-s.y/uRes.y*2.0, clamp((d-0.5)/8.0,0.0,1.0)*2.0-1.0, 1.0);
 vInfo=aInfo; vDepth=d; gl_PointSize=1.0;}`;
const FS=`precision mediump float;varying vec3 vInfo;varying float vDepth;uniform vec4 uMode;uniform vec3 uA;uniform vec3 uB;uniform vec3 uV;
void main(){float age=vInfo.x, top=vInfo.y, bk=vInfo.z;
 float fade=mix(1.0,0.07,uMode.y)*(1.0-uMode.z);
 if(uMode.x<0.5){
   vec3 deep=vec3(0.039,0.067,0.125); vec3 c=mix(deep,vec3(0.10,0.15,0.26),pow(top,3.0)*(0.35+0.65*age));
   gl_FragColor=vec4(c,0.94*fade);
 } else {
   vec3 c=mix(uA,uB,pow(age,1.6)); c=mix(c,uV,bk);
   float fog=clamp(1.15-(vDepth-2.2)*0.28,0.25,1.0);
   gl_FragColor=vec4(c*fog,fade);
 }}`;
const PVS=`attribute vec3 aPos;uniform vec4 uCam;uniform vec4 uView;uniform vec2 uRes;uniform float uT;varying float vA;
void main(){vec3 p=aPos; p.y=mod(p.y+uT*0.018*(0.5+fract(p.x*7.13)),1.6)-0.6;
 float cy=cos(uCam.x),sy=sin(uCam.x),cp=cos(uCam.y),sp=sin(uCam.y);
 float X=p.x*cy-p.z*sy, Z0=p.x*sy+p.z*cy, Y=p.y*cp+Z0*sp, Z=Z0*cp-p.y*sp, d=Z+uCam.z;
 vec2 s=vec2(uView.x+X*uCam.w/d, uView.y-Y*uCam.w/d);
 gl_Position=vec4(s.x/uRes.x*2.0-1.0, 1.0-s.y/uRes.y*2.0, 0.0, 1.0);
 gl_PointSize=max(1.0,3.2/d*uRes.y/700.0); vA=smoothstep(0.98,-0.5,p.y)*smoothstep(-0.6,-0.3,p.y);}`;
const PFS=`precision mediump float;varying float vA;uniform float uAlpha;
void main(){vec2 q=gl_PointCoord-0.5; float r=1.0-smoothstep(0.1,0.5,length(q)); gl_FragColor=vec4(0.95,0.64,0.23,r*vA*uAlpha);}`;
function mk(type,src){const s=gl.createShader(type); gl.shaderSource(s,src); gl.compileShader(s); if(!gl.getShaderParameter(s,gl.COMPILE_STATUS)) throw gl.getShaderInfoLog(s); return s}
function program(vs,fs){const p=gl.createProgram(); gl.attachShader(p,mk(gl.VERTEX_SHADER,vs)); gl.attachShader(p,mk(gl.FRAGMENT_SHADER,fs)); gl.linkProgram(p);
  if(!gl.getProgramParameter(p,gl.LINK_STATUS)) throw gl.getProgramInfoLog(p); return p}
function initGL(){
  try{
    gl=glc.getContext('webgl',{antialias:true,premultipliedAlpha:false,powerPreference:'high-performance'});
    if(!gl) return false;
    prog=program(VS,FS); parts={prog:program(PVS,PFS)};
    buf.pos=gl.createBuffer(); buf.info=gl.createBuffer(); buf.idx=gl.createBuffer(); buf.lpos=gl.createBuffer(); buf.linfo=gl.createBuffer();
    const N=RM?0:(innerWidth<760?220:520), pp=new Float32Array(N*3);
    for(let i=0;i<N;i++){pp[i*3]=Math.random()*3.6-1.8; pp[i*3+1]=Math.random()*1.6-0.6; pp[i*3+2]=Math.random()*2.9-1.0}
    parts.buf=gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER,parts.buf); gl.bufferData(gl.ARRAY_BUFFER,pp,gl.STATIC_DRAW); parts.n=N;
    glc.addEventListener('webglcontextlost',e=>{e.preventDefault(); gl=null});
    return true;
  }catch(e){ console.warn('WebGL off:',e); gl=null; return false }
}
function buildMesh(){
  const R=rows.length, A=0.55/yspan, P=[],I=[],X=[], LP=[],LI=[];
  let base=0;
  for(let r=0;r<R;r++){
    const age=r/(R-1||1), z=zOf(r,R); let last=null;
    const hs=rows[r].map(c=>{if(c.v!=null) last=(c.v-ymid)*A; return last});
    const first=hs.find(h=>h!=null)??0;
    for(let k=0;k<60;k++){const h=hs[k]??first; P.push(xOf(k),h,z, xOf(k),-0.62,z); I.push(age,1,0, age,0,0)}
    for(let k=0;k<59;k++){const a=base+k*2; X.push(a,a+1,a+2, a+1,a+3,a+2)}
    base+=120;
    if(r<R-1){ for(let k=0;k<59;k++){const c0=rows[r][k],c1=rows[r][k+1]; if(c0.v==null||c1.v==null) continue;
        const bk=c0.b&&c1.b?1:0; LP.push(xOf(k),(c0.v-ymid)*A,z, xOf(k+1),(c1.v-ymid)*A,z); LI.push(age,1,bk, age,1,bk)} }
  }
  gl.bindBuffer(gl.ARRAY_BUFFER,buf.pos); gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(P),gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER,buf.info); gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(I),gl.STATIC_DRAW);
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,buf.idx); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER,new Uint16Array(X),gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER,buf.lpos); gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(LP),gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER,buf.linfo); gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(LI),gl.STATIC_DRAW);
  nFill=X.length; buf.nLines=LP.length/3; meshDirty=false;
}
function attr(p,name,b,size){const l=gl.getAttribLocation(p,name); if(l<0) return; gl.bindBuffer(gl.ARRAY_BUFFER,b); gl.enableVertexAttribArray(l); gl.vertexAttribPointer(l,size,gl.FLOAT,false,0,0)}
function uniCam(p,c){gl.uniform4f(gl.getUniformLocation(p,'uCam'),c.yw,c.pitch,c.dist,c.f*DPR); gl.uniform4f(gl.getUniformLocation(p,'uView'),c.ox*DPR,c.oy*DPR,0,0); gl.uniform2f(gl.getUniformLocation(p,'uRes'),W*DPR,H*DPR)}
function drawGL(c){
  gl.viewport(0,0,glc.width,glc.height); gl.clearColor(0,0,0,0); gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
  if(!rows) return; if(meshDirty) buildMesh();
  gl.useProgram(prog); uniCam(prog,c);
  gl.uniform3fv(gl.getUniformLocation(prog,'uA'),hex('#2E4068')); gl.uniform3fv(gl.getUniformLocation(prog,'uB'),hex('#A3B6DC')); gl.uniform3fv(gl.getUniformLocation(prog,'uV'),hex(C.violet));
  gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);
  if(c.flat<1){
    gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LEQUAL); gl.enable(gl.POLYGON_OFFSET_FILL); gl.polygonOffset(1,1);
    gl.uniform4f(gl.getUniformLocation(prog,'uMode'),0,c.focus,c.flat,0);
    attr(prog,'aPos',buf.pos,3); attr(prog,'aInfo',buf.info,3); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,buf.idx);
    gl.drawElements(gl.TRIANGLES,nFill,gl.UNSIGNED_SHORT,0);
    gl.disable(gl.POLYGON_OFFSET_FILL);
    gl.uniform4f(gl.getUniformLocation(prog,'uMode'),1,c.focus,c.flat,0);
    attr(prog,'aPos',buf.lpos,3); attr(prog,'aInfo',buf.linfo,3);
    gl.drawArrays(gl.LINES,0,buf.nLines);
    gl.disable(gl.DEPTH_TEST);
  }
  if(parts.n){
    gl.useProgram(parts.prog); uniCam(parts.prog,c);
    gl.uniform1f(gl.getUniformLocation(parts.prog,'uT'),performance.now()/1000);
    gl.uniform1f(gl.getUniformLocation(parts.prog,'uAlpha'),0.55*(1-c.down)+0.08);
    gl.blendFunc(gl.SRC_ALPHA,gl.ONE); attr(parts.prog,'aPos',parts.buf,3); gl.drawArrays(gl.POINTS,0,parts.n);
  }
}

// ---------------------------------------------------------------- 2D overlay: newest ridge, its morph into the proof, the recording's hum
const PN=PROOF.st.length, PST=PROOF.st.map(x=>50+x/1e4), PV=PROOF.v.map(x=>x==null?null:50+x/1e4);
function fitAt(shift){let n=0,sa=0,sb=0,saa=0,sbb=0,sab=0;
  for(let i=0;i<PN;i++){const j=i-shift; if(j<0||j>=PN||PV[j]==null) continue; const a=PST[i],b=PV[j]; n++;sa+=a;sb+=b;saa+=a*a;sbb+=b*b;sab+=a*b}
  if(n<20) return 0; const cov=sab/n-sa/n*sb/n; return cov/Math.sqrt((saa/n-(sa/n)**2)*(sbb/n-(sb/n)**2))}
const FITS={}; const fit=s=>FITS[s]??(FITS[s]=fitAt(s));
function draw2D(c,full){
  const ctx=cx; ctx.setTransform(DPR,0,0,DPR,0,0); ctx.clearRect(0,0,W,H); if(!rows) return;
  const proj=projector(c), R=rows.length, A=0.55/yspan, p=c.p;
  const L=c.mob?58:Math.max(90,W*0.08), Rr=W-(c.mob?16:60), Top=H*(c.mob?0.24:0.2), Bot=H*(c.mob?0.5:0.56);
  const plo=49.86,phi=50.36, py=v=>lerp(Bot,Top,(v-plo)/(phi-plo)), px=i=>lerp(L,Rr,i/(PN-1));
  ctx.lineJoin='round'; ctx.lineCap='round';
  for(let r=full?0:R-1;r<R;r++){
    const row=rows[r], newest=r===R-1, z=zOf(r,R), age=r/(R-1||1);
    const alpha=newest?1:lerp(1,0.07,c.focus)*(1-c.flat); if(alpha<=0.01) continue;
    const pts=row.map((q,k)=>q.v==null?null:proj(xOf(k),(q.v-ymid)*A,z));
    if(full&&!newest){ctx.globalAlpha=alpha; ctx.beginPath(); let s=false; pts.forEach(q=>{if(!q)return; s?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1]); s=true});
      const b1=proj(xOf(59),-0.62,z),b0=proj(xOf(0),-0.62,z); ctx.lineTo(b1[0],b1[1]); ctx.lineTo(b0[0],b0[1]); ctx.closePath(); ctx.fillStyle=C.deep; ctx.globalAlpha=alpha*0.9; ctx.fill();
      ctx.globalAlpha=alpha; ctx.beginPath(); let pen=false; pts.forEach(q=>{if(!q){pen=false;return} pen?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1]); pen=true});
      ctx.strokeStyle=`rgba(124,141,176,${0.25+0.75*Math.pow(age,1.6)})`; ctx.lineWidth=1.2; ctx.stroke(); continue}
    ctx.globalAlpha=1;
    if(c.flat>0){ctx.beginPath();
      for(let i=0;i<PN;i++){const k=Math.round(i/(PN-1)*59), q=pts[k]||proj(xOf(k),0,z); const X=lerp(q[0],px(i),c.flat), Y=lerp(q[1],py(PST[i]),c.flat); i?ctx.lineTo(X,Y):ctx.moveTo(X,Y)}
      ctx.strokeStyle=C.sodium; ctx.lineWidth=2.6; ctx.shadowColor='rgba(242,162,58,.55)'; ctx.shadowBlur=14; ctx.stroke(); ctx.shadowBlur=0; continue}
    ctx.beginPath(); let s=false; pts.forEach(q=>{if(!q)return; s?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1]); s=true});
    const b1=proj(xOf(59),-0.62,z),b0=proj(xOf(0),-0.62,z); ctx.lineTo(b1[0],b1[1]); ctx.lineTo(b0[0],b0[1]); ctx.closePath();
    let ytop=1e9,ybot=Math.max(b0[1],b1[1]); pts.forEach(q=>{if(q&&q[1]<ytop) ytop=q[1]});
    const g=ctx.createLinearGradient(0,ytop,0,ybot); g.addColorStop(0,'rgba(242,162,58,.14)'); g.addColorStop(0.22,'rgba(14,23,41,.94)'); g.addColorStop(1,'rgba(10,17,32,.97)'); ctx.fillStyle=g; ctx.fill();
    ctx.beginPath(); let pen=false; pts.forEach(q=>{if(!q){pen=false;return} pen?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1]); pen=true});
    ctx.strokeStyle=C.sodium; ctx.lineWidth=2.6; ctx.shadowColor='rgba(242,162,58,.6)'; ctx.shadowBlur=16; ctx.stroke(); ctx.shadowBlur=0;
    ctx.beginPath(); pen=false; row.forEach((q,k)=>{const P=pts[k]; if(!P||!q.b){pen=false;return} pen?ctx.lineTo(P[0],P[1]):ctx.moveTo(P[0],P[1]); pen=true});
    ctx.strokeStyle=C.violet; ctx.lineWidth=2.6; ctx.stroke();
    let k=59; while(k>0&&!pts[k]) k--; const q=pts[k];
    if(q){const t=performance.now()/1000, rr=RM?5:5+2*Math.sin(t*3.3); ctx.fillStyle=C.sodium; ctx.beginPath(); ctx.arc(q[0],q[1],rr,0,7); ctx.fill();
      if(!RM){ctx.strokeStyle=`rgba(242,162,58,${0.5-(t%1.6)/3.2})`; ctx.lineWidth=1.5; ctx.beginPath(); ctx.arc(q[0],q[1],6+(t%1.6)*14,0,7); ctx.stroke()}}
  }
  const hum=seg(p,0.62,0.7);
  if(hum>0){
    const slide=ease(seg(p,0.8,0.95)), shift=Math.round(lerp(55,0,slide));
    ctx.globalAlpha=hum; ctx.fillStyle=C.cyan;
    for(let i=0;i<PN;i++){const j=i-shift; if(j<0||j>=PN||PV[j]==null||PV[j]<plo||PV[j]>phi) continue; ctx.beginPath(); ctx.arc(px(i),py(PV[j]),1.7,0,7); ctx.fill()}
    ctx.globalAlpha=hum*0.9; ctx.font='13px "IBM Plex Sans",system-ui'; ctx.textAlign='right';
    [49.9,50.0,50.1,50.2,50.3].forEach(v=>{const y=py(v); ctx.fillStyle=C.mut; ctx.fillText(v.toFixed(1)+' Hz',L-8,y+4); ctx.fillStyle='rgba(132,148,178,.18)'; ctx.fillRect(L,y,Rr-L,1)});
    ctx.fillStyle=C.mut;
    for(let m=0;m<=50;m+=(c.mob?20:10)){const i=Math.round(m*60/PROOF.dt); if(i>=PN)break; ctx.textAlign=m===0?'left':'center'; ctx.fillText(PKT.format((PROOF.t0+i*PROOF.dt)*1000),px(i),Bot+22)}
    const ft=fit(shift); ctx.textAlign='right'; ctx.font='600 15px "IBM Plex Sans",system-ui'; ctx.fillStyle=ft>0.8?C.ink:C.mut;
    ctx.fillText(shift?`Recording ${shift*PROOF.dt} s out of place · fit ${ft.toFixed(2)}`:`Locked on · fit ${ft.toFixed(2)}`,Rr,Top-16);
    ctx.globalAlpha=1;
  }
}

// ---------------------------------------------------------------- scroll state and frame loop
const state={p:0};
let lastStep=-1;
function size(){DPR=Math.min(2,devicePixelRatio||1); W=glc.clientWidth; H=glc.clientHeight; for(const c of [glc,fx]){c.width=W*DPR; c.height=H*DPR}}
function frame(){
  const c=camAt(state.p), p=state.p;
  if(gl) drawGL(c); draw2D(c,!gl);
  if(!G) $('hero').style.opacity=1-seg(p,0.05,0.12);
  $('axis').style.opacity=1-seg(p,0.3,0.4); $('live').style.opacity=1-seg(p,0.5,0.58);
  const st=p<0.14?0:p<0.36?1:p<0.58?2:p<0.78?3:4;
  if(st!==lastStep){lastStep=st; showStep(st)}
  $('legend').classList.toggle('on',p>0.58&&p<0.99);
  requestAnimationFrame(frame);
}
function showStep(st){
  document.querySelectorAll('.step').forEach(e=>{const on=+e.dataset.s===st; e.classList.toggle('on',on);
    if(on&&G&&!RM){const ls=e._lines||e.querySelectorAll('h2,p,.n'); G.fromTo(ls,{yPercent:60,opacity:0,filter:'blur(6px)'},{yPercent:0,opacity:1,filter:'blur(0px)',duration:.7,stagger:.06,ease:'power3.out',overwrite:true})}});
}

// ---------------------------------------------------------------- proof chart
const pc=$('proof'), pctx=pc.getContext('2d'); let PW=0,PH=0;
function sizeProof(){PW=pc.clientWidth; PH=pc.clientHeight; pc.width=PW*DPR; pc.height=PH*DPR}
const PM={l:62,r:18,t:12,b:34}, plo=49.86, phi=50.36;
const gx=i=>PM.l+(PW-PM.l-PM.r)*i/(PN-1), gy=v=>PM.t+(PH-PM.t-PM.b)*(1-(v-plo)/(phi-plo));
let hoverI=null; const rev={v:RM?1:0};
function drawProof(){
  const c=pctx, rv=rev.v; c.setTransform(DPR,0,0,DPR,0,0); c.clearRect(0,0,PW,PH);
  c.font='12px "IBM Plex Sans",system-ui'; c.textAlign='right';
  [49.9,50.0,50.1,50.2,50.3].forEach(v=>{const y=gy(v); c.fillStyle=C.mut; c.fillText(v.toFixed(1)+' Hz',PM.l-10,y+4); c.fillStyle='rgba(132,148,178,.14)'; c.fillRect(PM.l,y,PW-PM.l-PM.r,1)});
  c.fillStyle=C.mut;
  for(let m=0;m<=50;m+=(PW<600?20:10)){const i=Math.round(m*60/PROOF.dt); if(i>=PN)break; c.textAlign=m===0?'left':'center'; c.fillText(PKT.format((PROOF.t0+i*PROOF.dt)*1000),gx(i),PH-10)}
  const a=PROOF.cut[0]/PROOF.dt, b=PROOF.cut[1]/PROOF.dt; c.fillStyle='rgba(164,147,255,.16)'; c.fillRect(gx(a),PM.t,(gx(b)-gx(a))*clamp(rv*1.4,0,1),PH-PM.t-PM.b);
  const n=Math.floor(PN*rv); c.save(); c.beginPath(); c.rect(0,0,gx(Math.max(0,n-1))+1,PH); c.clip();
  c.beginPath(); for(let i=0;i<PN;i++){i?c.lineTo(gx(i),gy(PST[i])):c.moveTo(gx(i),gy(PST[i]))}
  c.strokeStyle=C.sodium; c.lineWidth=2.4; c.lineJoin='round'; c.stroke();
  c.fillStyle=C.cyan; for(let i=0;i<PN;i++){if(PV[i]==null||PV[i]<plo||PV[i]>phi)continue; c.beginPath(); c.arc(gx(i),gy(PV[i]),1.6,0,7); c.fill()}
  c.restore();
  if(n>1&&rv<1){c.fillStyle=C.sodium; c.beginPath(); c.arc(gx(n-1),gy(PST[n-1]),4,0,7); c.fill()}
  if(hoverI!=null&&rv>=1){const x=gx(hoverI); c.fillStyle='rgba(234,240,248,.35)'; c.fillRect(x,PM.t,1,PH-PM.t-PM.b);
    c.fillStyle=C.sodium; c.beginPath(); c.arc(x,gy(PST[hoverI]),4.5,0,7); c.fill();
    if(PV[hoverI]!=null){c.strokeStyle=C.deep;c.lineWidth=2;c.fillStyle=C.cyan; c.beginPath(); c.arc(x,gy(PV[hoverI]),4.5,0,7); c.fill(); c.stroke()}}
}
pc.addEventListener('pointermove',e=>{const r=pc.getBoundingClientRect(), x=e.clientX-r.left, i=Math.round((x-PM.l)/(PW-PM.l-PM.r)*(PN-1)), tip=$('ptip');
  if(i<0||i>=PN){hoverI=null;tip.style.display='none';drawProof();return}
  hoverI=i; drawProof();
  const t=(PROOF.t0+i*PROOF.dt)*1000, inCut=i*PROOF.dt>=PROOF.cut[0]&&i*PROOF.dt<=PROOF.cut[1];
  tip.innerHTML=`<b>${PKTs.format(t)}</b><br><span style="color:${C.sodium}">Station ${PST[i].toFixed(3)} Hz</span><br><span style="color:${C.cyan}">Video ${PV[i]==null?'lost the hum here':PV[i].toFixed(3)+' Hz'}</span>${inCut?'<br><span style="color:#A493FF">mains off, inverter</span>':''}`;
  tip.style.display='block'; const w=tip.offsetWidth; let L=x+34; if(L+w>pc.clientWidth) L=x-w-4; tip.style.left=L+'px'; tip.style.top='20px'});
pc.addEventListener('pointerleave',()=>{hoverI=null;$('ptip').style.display='none';drawProof()});

// ---------------------------------------------------------------- motion: Lenis + ScrollTrigger + SplitText
function motion(){
  if(!G||!ST){
    addEventListener('scroll',()=>{const s=$('seq'),r=s.getBoundingClientRect(); state.p=clamp(-r.top/(s.offsetHeight-innerHeight),0,1)},{passive:true});
    new IntersectionObserver((es,ob)=>{if(es[0].isIntersecting){ob.disconnect(); rev.v=1; drawProof()}},{threshold:.35}).observe(pc);
    return;
  }
  G.registerPlugin(ST); if(SPLIT) G.registerPlugin(SPLIT);
  if(window.Lenis&&!RM){
    const lenis=new Lenis({lerp:0.09,smoothWheel:true});
    lenis.on('scroll',ST.update); G.ticker.add(t=>lenis.raf(t*1000)); G.ticker.lagSmoothing(0);
    document.querySelectorAll('a[href^="#"]').forEach(a=>a.addEventListener('click',e=>{const t=document.querySelector(a.getAttribute('href')); if(t){e.preventDefault(); lenis.scrollTo(t,{duration:1.6})}}));
  }
  G.to(state,{p:1,ease:'none',scrollTrigger:{trigger:'#seq',start:'top top',end:'bottom bottom',scrub:RM?true:0.9}});
  G.to('#hero',{yPercent:-18,opacity:0,ease:'none',scrollTrigger:{trigger:'#seq',start:'top top',end:'12% top',scrub:true}});
  ST.create({trigger:'#seq',start:'10% top',onToggle:s=>{$('hero').style.pointerEvents=s.isActive?'none':'auto'}});
  if(RM){rev.v=1; drawProof(); return}
  if(SPLIT){
    const sp=new SplitText('#hero h1',{type:'words,chars',wordsClass:'w'});
    G.timeline({defaults:{ease:'expo.out'}})
      .from(sp.chars,{yPercent:110,rotateX:-80,opacity:0,duration:1.1,stagger:{each:0.018,from:'start'}},0.15)
      .from('#hero p',{y:24,opacity:0,duration:.9},0.55)
      .from('#hero .btn',{y:18,opacity:0,duration:.7,stagger:.08},0.7)
      .from('#live',{x:30,opacity:0,duration:.9},0.4)
      .from('#axis',{opacity:0,duration:1},0.8)
      .from('.stage canvas',{opacity:0,duration:2.2,ease:'power2.out'},0)
      .from('.nav',{y:-30,opacity:0,duration:.8},0.2);
    document.querySelectorAll('.step').forEach(s=>{const parts=new SplitText(s.querySelector('h2'),{type:'lines',linesClass:'ln'}); s._lines=[s.querySelector('.n'),...parts.lines,s.querySelector('p')]});
  }
  G.utils.toArray('.block h2').forEach(h=>{const s=SPLIT?new SplitText(h,{type:'lines',linesClass:'ln'}).lines:[h];
    G.from(s,{yPercent:100,opacity:0,duration:1,stagger:.1,ease:'expo.out',scrollTrigger:{trigger:h,start:'top 82%'}})});
  G.utils.toArray('.figs li').forEach((li,i)=>{const b=li.querySelector('b'), m=b.textContent.match(/^([<]?)([\d.]+)(.*)$/);
    G.from(li,{y:40,opacity:0,duration:.9,delay:i*.08,ease:'power3.out',scrollTrigger:{trigger:'.figs',start:'top 85%'}});
    if(m&&!m[3].startsWith(':')){const v=parseFloat(m[2]), dp=(m[2].split('.')[1]||'').length, o={x:0}; b.textContent=m[1]+(0).toFixed(dp)+m[3];
      G.to(o,{x:v,duration:1.6,delay:.2+i*.08,ease:'power2.out',scrollTrigger:{trigger:'.figs',start:'top 85%'},onUpdate:()=>{b.textContent=m[1]+o.x.toFixed(dp)+m[3]}})}});
  G.to(rev,{v:1,duration:2.4,ease:'power2.inOut',onUpdate:drawProof,scrollTrigger:{trigger:pc,start:'top 75%'}});
  G.from('.chartwrap',{clipPath:'inset(0 0 100% 0 round 18px)',duration:1.2,ease:'expo.inOut',scrollTrigger:{trigger:'.chartwrap',start:'top 85%'}});
  G.utils.toArray('.fine li').forEach((li,i)=>G.from(li,{x:40,opacity:0,duration:.8,delay:i*.1,ease:'power3.out',scrollTrigger:{trigger:'.fine',start:'top 85%'}}));
}

addEventListener('resize',()=>{size();sizeProof();drawProof(); if(ST) ST.refresh()});
size(); sizeProof(); initGL(); motion(); drawProof();
loadPulse(); setInterval(loadPulse,20000); setInterval(showLive,5000);
requestAnimationFrame(frame);
})();
'''


_pulse = {"at": 0, "data": None}


def public_pulse(station="isb-01"):
    """Last 6 hours at 10-second steps for the public home page, cached for 20 s. Frequency only, no accounts."""
    now = time.time()
    with lock:
        if _pulse["data"] and now - _pulse["at"] < 20:
            return _pulse["data"]
    t1 = now; t0 = t1 - 6 * 3600; step = 10
    r = range_query(station, t0, t1, int((t1 - t0) / step))
    f = [None] * int((t1 - t0) // step)
    for t, m in zip(r["t"], r["mean"]):
        i = int((t - t0) // step)
        if m is not None and 0 <= i < len(f):
            f[i] = round((m - 50) * 1e4)
    recent = [m for t, m in zip(r["t"], r["mean"]) if m is not None and t > now - 90]
    trend = (recent[-1] - recent[0]) if len(recent) > 3 else 0
    data = {"station": station, "t0": t0, "step": step, "f": f, "f_now": recent[-1] if recent else None,
            "trend": round(trend, 4), "last_seen": last_seen.get(station), "backup": r.get("backup", [])}
    with lock:
        _pulse.update(at=now, data=data)
    return data


TEAM_HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GridPrint · team</title>
<meta name="description" content="GridPrint is built by two roommates in Islamabad: Ibrahim Sadat and Abdullah Habib.">
<script src="__PREFIX__/static/wave.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource-variable/bricolage-grotesque@5.3.0/wdth.css" integrity="sha384-WQqnp9hDfoH3W8tz8N27r/2KCPxaxoS9L8KDqmXZdjhQOsnqVEV0GNrH+R6M0y4M" crossorigin="anonymous">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-sans@5.3.0/400.css" integrity="sha384-by8hCwJDxTOUHMGBI4elNkybAoX7Gs45s1pbgL9fdUTDy+tXTA+tZmiU+0LkwckA" crossorigin="anonymous">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-sans@5.3.0/600.css" integrity="sha384-1dcQop1Fsaw4/4DmZbwqeTSgod1K+Zkz3QWtbAUGxmw7ECfIGKOHUIRKcGtpdNHx" crossorigin="anonymous">
<style>
:root{--deep:#0A1120;--night:#0E1729;--panel:#15213A;--line:#24324F;--ink:#EAF0F8;--mut:#8494B2;--sodium:#F2A23A;--cyan:#6FD6FF;
 --disp:'Bricolage Grotesque Variable',system-ui,sans-serif;--body:'IBM Plex Sans',system-ui,'Segoe UI',sans-serif}
*{box-sizing:border-box}html{background:var(--deep);color:var(--ink)}body{margin:0;font:17px/1.6 var(--body);min-height:100vh;overflow-x:hidden}
a{color:inherit}:focus-visible{outline:2px solid var(--sodium);outline-offset:3px;border-radius:6px}
#bg{position:fixed;inset:0;width:100%;height:100%;z-index:0;display:block}
.nav{position:fixed;inset:0 0 auto;z-index:20;display:flex;justify-content:space-between;align-items:center;padding:18px clamp(18px,4vw,48px);background:linear-gradient(var(--deep) 10%,transparent)}
.mark{font:700 22px/1 var(--disp);font-stretch:80%;text-decoration:none;display:flex;align-items:center;gap:10px}.mark svg{width:30px;height:18px}
.nav nav{display:flex;gap:6px}
.btn{display:inline-flex;align-items:center;gap:8px;font:600 15px/1 var(--body);padding:12px 18px;border-radius:999px;border:1px solid var(--line);background:rgba(21,33,58,.7);color:var(--ink);text-decoration:none;cursor:pointer;backdrop-filter:blur(6px);transition:border-color .2s,background .2s}
.btn:hover{border-color:var(--sodium)}.btn{white-space:nowrap}@media (max-width:480px){.nav .btn:not(.hot){display:none}}
.btn.hot{background:var(--sodium);border-color:var(--sodium);color:#1A1205}.btn.hot:hover{background:#FFB650}
main{position:relative;z-index:1;max-width:1180px;margin:0 auto;padding:clamp(120px,18vh,180px) clamp(18px,5vw,56px) 80px}
h1{margin:0;font:760 clamp(44px,7vw,100px)/.93 var(--disp);font-stretch:78%;letter-spacing:-.025em;max-width:12em;text-wrap:balance}
.lede{max-width:36em;color:#C4CEE0;font-size:clamp(17px,1.5vw,19px);margin:22px 0 56px}
.people{display:grid;grid-template-columns:1fr 1fr;gap:clamp(20px,3vw,40px);perspective:1400px}
.card{position:relative;height:clamp(520px,60vh,600px);transform-style:preserve-3d;cursor:pointer;outline:none}
.flip{position:absolute;inset:0;transform-style:preserve-3d;transition:transform .9s cubic-bezier(.2,.8,.2,1)}
.card.open .flip{transform:rotateY(180deg)}
.face{position:absolute;inset:0;backface-visibility:hidden;-webkit-backface-visibility:hidden;border-radius:26px;border:1px solid var(--line);overflow:hidden;
 background:linear-gradient(160deg,rgba(28,43,74,.92),rgba(14,23,41,.95));box-shadow:0 40px 80px -40px rgba(0,0,0,.8),inset 0 1px 0 rgba(255,255,255,.05);padding:30px;display:flex;flex-direction:column}
.back{transform:rotateY(180deg);justify-content:center;align-items:flex-start}
.glare{position:absolute;inset:0;pointer-events:none;background:radial-gradient(500px circle at var(--gx,50%) var(--gy,0%),rgba(242,162,58,.16),transparent 45%);mix-blend-mode:screen}
.portrait{position:relative;width:clamp(150px,15vw,190px);aspect-ratio:1;margin:6px 0 24px;transform:translateZ(60px)}
.portrait img,.portrait svg.av{width:100%;height:100%;border-radius:50%;object-fit:cover;display:block;border:3px solid var(--deep);position:relative;z-index:1}
.portrait .ring{position:absolute;inset:-14px;z-index:0}
.portrait .ring circle{fill:none;stroke:var(--sodium);stroke-width:1.6;stroke-dasharray:4 9;transform-origin:50% 50%;animation:spin 22s linear infinite}
.portrait .ring path{fill:none;stroke:var(--sodium);stroke-width:2.2;stroke-linecap:round;filter:drop-shadow(0 0 6px rgba(242,162,58,.8))}
@keyframes spin{to{transform:rotate(360deg)}}
.name{font:740 clamp(32px,3.4vw,46px)/1 var(--disp);font-stretch:82%;letter-spacing:-.02em;margin:0;transform:translateZ(40px)}
.role{color:var(--sodium);font-weight:600;margin:8px 0 16px;transform:translateZ(30px)}
.bio{color:#C4CEE0;margin:0;font-size:16px;transform:translateZ(20px);max-width:34em}
.hint{margin-top:auto;padding-top:18px;color:var(--mut);font-size:14px;display:flex;align-items:center;gap:8px;transform:translateZ(30px)}
.hint i{width:22px;height:22px;border-radius:50%;border:1px solid var(--line);display:grid;place-items:center;font-style:normal;font-size:12px;color:var(--sodium)}
.back h3{font:720 34px/1.05 var(--disp);font-stretch:84%;margin:0 0 10px}
.back p{color:#C4CEE0;margin:0 0 26px}
.mail{display:inline-flex;align-items:center;gap:12px;padding:16px 22px;border-radius:16px;background:var(--sodium);color:#1A1205;font-weight:600;text-decoration:none;font-size:17px;box-shadow:0 14px 34px -12px rgba(242,162,58,.7)}
.mail:hover{background:#FFB650}
.addr{margin-top:14px;color:var(--mut);font-size:14px;user-select:all}
.backbtn{margin-top:28px;background:none;border:0;color:var(--mut);font:inherit;font-size:14px;cursor:pointer;padding:0}
.backbtn:hover{color:var(--ink)}
.together{margin:70px 0 0;padding-top:30px;border-top:1px solid var(--line);display:flex;flex-wrap:wrap;gap:16px 40px;align-items:center;justify-content:space-between}
.together p{margin:0;color:#C4CEE0;max-width:40em}
footer{position:relative;z-index:1;padding:40px clamp(18px,6vw,88px) 50px;color:var(--mut);font-size:14px;display:flex;flex-wrap:wrap;gap:12px 30px;justify-content:space-between;border-top:1px solid var(--line)}
@media (max-width:820px){.people{grid-template-columns:1fr}.card{height:auto;min-height:560px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<canvas id="bg" aria-hidden="true"></canvas>
<header class="nav">
 <a class="mark" href="__PREFIX__/" data-go aria-label="GridPrint home"><svg viewBox="0 0 30 18" aria-hidden="true"><path d="M1 9 Q4.5 1 8 9 T15 9 T22 9 T29 9" fill="none" stroke="#F2A23A" stroke-width="2.4" stroke-linecap="round"/></svg>GridPrint</a>
 <nav><a class="btn" href="__PREFIX__/" data-go>Home</a><a class="btn hot" href="__PREFIX__/login" data-go>Open the explorer</a></nav>
</header>
<main>
 <h1>Two roommates and a wall socket.</h1>
 <p class="lede">GridPrint is built in a hostel room at NUST, Islamabad. One of us does the maths and the code, the other the engineering. Pick a card to get in touch.</p>
 <section class="people">
  <article class="card" tabindex="0" role="button" aria-expanded="false" aria-label="Ibrahim Sadat: show contact">
   <div class="flip">
    <div class="face front"><div class="glare"></div>
     <div class="portrait"><svg class="ring" viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="48"/><path d="M8 50 Q15 40 22 50 T36 50"/></svg>
      <img src="__PREFIX__/static/ibrahim.webp" alt="Ibrahim Sadat" width="520" height="520"></div>
     <h2 class="name">Ibrahim Sadat</h2>
     <div class="role">Maths major, NUST</div>
     <p class="bio">From Lahore, studying mathematics at NUST in Islamabad. Ibrahim started GridPrint with one question: can the electricity grid itself act as a clock that nobody can fake? He wrote the station firmware, the server, the hum matching and this site. Before this, he led his team "Connected, No Internet" to first place at NUST's Piston Cup 2026.</p>
     <div class="hint"><i>✉</i>Click the card to email Ibrahim</div>
    </div>
    <div class="face back"><div class="glare"></div>
     <h3>Write to Ibrahim</h3><p>Questions about GridPrint, the data, or working together.</p>
     <a class="mail" href="mailto:ibrahimsadat716@gmail.com?subject=GridPrint">✉ Email Ibrahim</a>
     <div class="addr">ibrahimsadat716@gmail.com</div>
     <button class="backbtn" type="button">← Back to the profile</button>
    </div>
   </div>
  </article>
  <article class="card" tabindex="0" role="button" aria-expanded="false" aria-label="Abdullah Habib: show contact">
   <div class="flip">
    <div class="face front"><div class="glare"></div>
     <div class="portrait"><svg class="ring" viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="48"/><path d="M64 50 Q71 40 78 50 T92 50"/></svg>
      <svg class="av" viewBox="0 0 100 100" role="img" aria-label="Abdullah Habib (default profile picture)"><rect width="100" height="100" fill="#22314F"/><circle cx="50" cy="40" r="17" fill="#8494B2"/><path d="M18 92 C20 68 34 60 50 60 C66 60 80 68 82 92 Z" fill="#8494B2"/></svg></div>
     <h2 class="name">Abdullah Habib</h2>
     <div class="role">Mechanical engineering</div>
     <p class="bio">Ibrahim's roommate and the engineer on the team. Abdullah brings the mechanical engineering side to GridPrint.</p>
     <div class="hint"><i>✉</i>Click the card to email Abdullah</div>
    </div>
    <div class="face back"><div class="glare"></div>
     <h3>Write to Abdullah</h3><p>Questions about the engineering side of GridPrint.</p>
     <a class="mail" href="mailto:abdullahhabibsaleemi786@gmail.com?subject=GridPrint">✉ Email Abdullah</a>
     <div class="addr">abdullahhabibsaleemi786@gmail.com</div>
     <button class="backbtn" type="button">← Back to the profile</button>
    </div>
   </div>
  </article>
 </section>
 <div class="together"><p>Need an account for the explorer? Accounts are created by hand. Email either of us with your name and why you'd like access.</p><a class="btn" href="__PREFIX__/login" data-go>Back to sign in</a></div>
</main>
<footer><span>GridPrint, by Ibrahim Sadat and Abdullah Habib, for Hack Club Crossroads.</span><span><a href="__PREFIX__/" data-go>Home</a></span></footer>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.15.1/dist/gsap.min.js" integrity="sha384-3vCAhD+a3BOliP3QqjQINn6ZWiZxoqu0WFxX5ZQZgqekDZuSb1mJBrrLMmu94H8f" crossorigin="anonymous" defer></script>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.15.1/dist/SplitText.min.js" integrity="sha384-e98MX/fxZrUXA/6T+aCptryLfHgR2FpEmWxDbbPloHT0Qg79dEXKdLJ/iwD2lzxs" crossorigin="anonymous" defer></script>
<script src="__PREFIX__/static/team.js" defer></script>
</body></html>
'''
TEAM_JS = r'''// GridPrint team page: a field of grid waves in perspective behind two 3D profile cards that tilt toward the
// pointer and flip over to show how to get in touch.
(()=>{
'use strict';
const G=window.gsap, RM=matchMedia('(prefers-reduced-motion: reduce)').matches, anim=!!G&&!RM;
// ---------------------------------------------------------------- background: wave strings receding into the distance
const cv=document.getElementById('bg'), c=cv.getContext('2d'); let W=0,H=0,DPR=1, mx=0.5, my=0.5;
function size(){DPR=Math.min(2,devicePixelRatio||1); W=innerWidth; H=innerHeight; cv.width=W*DPR; cv.height=H*DPR}
addEventListener('resize',size); size();
addEventListener('pointermove',e=>{mx=e.clientX/W; my=e.clientY/H},{passive:true});
let sx=0.5, sy=0.5;
function frame(now){
  const t=now/1000; sx+=(mx-sx)*0.04; sy+=(my-sy)*0.04;
  c.setTransform(DPR,0,0,DPR,0,0); c.clearRect(0,0,W,H);
  const g=c.createRadialGradient(W*0.7,H*0.25,0,W*0.7,H*0.25,Math.max(W,H)); g.addColorStop(0,'#13213D'); g.addColorStop(1,'#0A1120'); c.fillStyle=g; c.fillRect(0,0,W,H);
  const N=26, hor=H*(0.42+(sy-0.5)*0.06), vx=W*(0.5+(sx-0.5)*0.12);
  for(let i=N;i>=1;i--){
    const z=i/N, p=1/(0.35+z*2.2);                  // perspective: far strings are small and close to the horizon
    const yb=hor+(H*0.9)*p*0.55, amp=H*0.05*p, k=0.9+z*2;
    c.beginPath();
    for(let x=-20;x<=W+20;x+=8){const u=(x-vx)/W*Math.PI*2*k; const yy=yb+amp*(Math.sin(u+(RM?0:t*0.8)+i*0.7)+0.3*Math.sin(2.3*u-(RM?0:t*1.1)+i)); x<0?c.moveTo(x,yy):c.lineTo(x,yy)}
    const front=i===1; c.strokeStyle=front?'rgba(242,162,58,.85)':`rgba(124,141,176,${0.06+0.28*(1-z)})`; c.lineWidth=front?2.2:1;
    c.stroke(); if(front){c.strokeStyle='rgba(242,162,58,.2)'; c.lineWidth=9; c.stroke()}
  }
  if(!RM) requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// ---------------------------------------------------------------- cards: tilt, glare, flip
document.querySelectorAll('.card').forEach(card=>{
  const flip=card.querySelector('.flip');
  const rx=anim?G.quickTo(card,'rotationX',{duration:.6,ease:'power3.out'}):null, ry=anim?G.quickTo(card,'rotationY',{duration:.6,ease:'power3.out'}):null;
  card.addEventListener('pointermove',e=>{ if(!anim) return; const r=card.getBoundingClientRect(), px=(e.clientX-r.left)/r.width, py=(e.clientY-r.top)/r.height;
    rx((0.5-py)*14); ry((px-0.5)*16);
    card.querySelectorAll('.glare').forEach(gl=>{gl.style.setProperty('--gx',px*100+'%'); gl.style.setProperty('--gy',py*100+'%')})});
  card.addEventListener('pointerleave',()=>{if(anim){rx(0); ry(0)}});
  const toggle=open=>{card.classList.toggle('open',open); card.setAttribute('aria-expanded',String(open));
    if(anim) G.fromTo(card,{z:0},{z:60,duration:.45,yoyo:true,repeat:1,ease:'power2.out'})};
  card.addEventListener('click',e=>{ if(e.target.closest('a.mail')) return; if(e.target.closest('.backbtn')){toggle(false); return} toggle(!card.classList.contains('open')) });
  card.addEventListener('keydown',e=>{ if(e.target!==card) return; if(e.key==='Enter'||e.key===' '){e.preventDefault(); toggle(!card.classList.contains('open'))} });
});

// ---------------------------------------------------------------- arrival
if(anim){
  const parts=window.SplitText?new SplitText('h1',{type:'words,chars'}).chars:['h1'];
  G.timeline({defaults:{ease:'expo.out'}})
   .from(parts,{yPercent:110,rotateX:-80,opacity:0,duration:1.1,stagger:.02},0.35)
   .from('.lede',{y:24,opacity:0,duration:.9},0.7)
   .from('.card',{y:120,rotationX:-35,rotationY:(i)=>i?18:-18,opacity:0,duration:1.6,stagger:.15,transformOrigin:'50% 100%'},0.75)
   .from('.portrait',{scale:.6,opacity:0,duration:1.2,stagger:.15,ease:'back.out(1.6)'},1.05)
   .from('.together',{y:30,opacity:0,duration:.9},1.3);
}
})();
'''
WAVE_JS = r'''// GridPrint page transitions: leaving a page, a sine wave rises and floods the screen while the camera pans
// along it; it keeps oscillating while the next page loads, then drains away to reveal it.
// Loaded synchronously in <head> so the arriving page is covered before its first paint.
(()=>{
'use strict';
if(window.__gpWave) return; window.__gpWave=true;
const RM=matchMedia('(prefers-reduced-motion: reduce)').matches;
const KEY='gp_wave', DEEP='#0A1120', AMBER='#F2A23A';
let cv=null, ctx=null, W=0, H=0, DPR=1, raf=0;
const st={level:1.2, amp:0, phase:0, speed:6, k:1, line:1, mode:'idle', t0:0};
function mount(){
  if(cv) return; cv=document.createElement('canvas');
  cv.setAttribute('aria-hidden','true');
  cv.style.cssText='position:fixed;inset:0;width:100vw;height:100vh;z-index:2147483000;pointer-events:none;display:block';
  (document.body||document.documentElement).appendChild(cv); ctx=cv.getContext('2d'); size();
  addEventListener('resize',size);
}
function size(){ if(!cv) return; DPR=Math.min(2,devicePixelRatio||1); W=innerWidth; H=innerHeight; cv.width=W*DPR; cv.height=H*DPR }
function y(x,base,amp,ph,k){ // a grid-frequency-like wave: fundamental plus two quieter harmonics
  const u=x/W*Math.PI*2*k;
  return base+amp*(Math.sin(u+ph)+0.32*Math.sin(2.1*u-ph*1.3)+0.16*Math.sin(3.7*u+ph*0.7));
}
function draw(){
  const c=ctx; c.setTransform(DPR,0,0,DPR,0,0); c.clearRect(0,0,W,H);
  const base=H*st.level, amp=H*st.amp;
  // trailing ghosts: the camera has just passed them
  for(let g=3;g>=1;g--){
    c.beginPath(); for(let x=-10;x<=W+10;x+=6){const yy=y(x,base-g*amp*0.45,amp*(1-g*0.18),st.phase-g*0.6,st.k); x<0?c.moveTo(x,yy):c.lineTo(x,yy)}
    c.strokeStyle=`rgba(242,162,58,${0.10/g})`; c.lineWidth=1.5; c.stroke();
  }
  // the flood below the crest
  c.beginPath(); c.moveTo(-10,H+10);
  for(let x=-10;x<=W+10;x+=5) c.lineTo(x,y(x,base,amp,st.phase,st.k));
  c.lineTo(W+10,H+10); c.closePath();
  const g=c.createLinearGradient(0,Math.max(0,base-amp),0,H); g.addColorStop(0,'#13213D'); g.addColorStop(0.25,DEEP); g.addColorStop(1,DEEP);
  c.fillStyle=g; c.fill();
  // the crest itself, glowing
  c.beginPath(); for(let x=-10;x<=W+10;x+=4){const yy=y(x,base,amp,st.phase,st.k); x<0?c.moveTo(x,yy):c.lineTo(x,yy)}
  c.strokeStyle='rgba(242,162,58,.18)'; c.lineWidth=14; c.stroke(); c.strokeStyle='rgba(242,162,58,.35)'; c.lineWidth=6; c.stroke(); c.strokeStyle=AMBER; c.lineWidth=2.4; c.stroke();
  // once the screen is covered, an oscilloscope trace keeps humming in the middle until the next page is ready
  if(st.mode==='hold'||st.mode==='in'){
    const a=Math.max(0,1-(st.level<0.05?0:st.level*3));
    if(a>0){c.globalAlpha=a; c.beginPath(); for(let x=0;x<=W;x+=4){const yy=y(x,H*0.5,H*0.045,st.phase*1.6,1.4); x?c.lineTo(x,yy):c.moveTo(x,yy)}
      c.strokeStyle='rgba(242,162,58,.25)'; c.lineWidth=10; c.stroke(); c.strokeStyle=AMBER; c.lineWidth=2; c.stroke();
      c.font='600 13px "IBM Plex Sans",system-ui'; c.fillStyle='rgba(234,240,248,.55)'; c.textAlign='center'; c.fillText('tuning in…',W/2,H*0.5+H*0.045+38); c.globalAlpha=1}
  }
}
function loop(now){
  const dt=Math.min(0.05,(now-(loop.last||now))/1000); loop.last=now;
  st.phase+=dt*st.speed; tick(now); draw();
  if(st.mode!=='idle') raf=requestAnimationFrame(loop); else {cv&&cv.remove(); cv=null}
}
const ease=t=>t<.5?4*t*t*t:1-Math.pow(-2*t+2,3)/2;
function tick(now){
  const t=(now-st.t0)/1000;
  if(st.mode==='out'){ // rise: the crest climbs from below the screen past the top, wavelength tightening as the camera pushes in
    const p=Math.min(1,t/0.85), e=ease(p);
    st.level=1.15-1.45*e; st.amp=0.02+0.10*Math.sin(Math.PI*Math.min(1,p*1.1)); st.k=0.8+1.4*e; st.speed=5+9*e;
    if(p>=1){st.mode='hold'; st.t0=now}
  } else if(st.mode==='hold'){ st.level=-0.3; st.amp=0.02; st.speed=7 }
  else if(st.mode==='in'){ // drain: the crest falls back through the page and out the bottom
    const p=Math.min(1,t/0.95), e=ease(p);
    st.level=-0.3+1.5*e; st.amp=0.02+0.09*Math.sin(Math.PI*p); st.k=2.2-1.4*e; st.speed=12-7*e;
    if(p>=1){st.mode='idle'}
  }
}
function start(mode){ mount(); st.mode=mode; st.t0=performance.now(); cancelAnimationFrame(raf); loop.last=0; raf=requestAnimationFrame(loop) }

// ---- arriving: cover first, drain once the page has painted
let arriving=false;
try{const v=+sessionStorage.getItem(KEY)||0; sessionStorage.removeItem(KEY); arriving=!RM&&Date.now()-v<8000}catch(e){}
if(arriving){
  st.mode='hold'; st.level=-0.3; mount(); draw();
  const go=()=>setTimeout(()=>{start('hold'); setTimeout(()=>start('in'),180)},40);
  if(document.readyState==='complete') go(); else addEventListener('load',go,{once:true});
  setTimeout(()=>{if(st.mode==='hold') start('in')},2500);   // never trap the page behind the wave
}

// ---- leaving: links and forms marked data-go
function leave(fn){
  if(RM){fn();return}
  try{sessionStorage.setItem(KEY,String(Date.now()))}catch(e){}
  start('out'); setTimeout(fn,620);   // start loading the next page while the wave is still rising
}
document.addEventListener('click',e=>{
  const a=e.target.closest&&e.target.closest('a[data-go]'); if(!a||e.defaultPrevented) return;
  if(e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey||a.target==='_blank') return;
  const u=new URL(a.href,location.href); if(u.origin!==location.origin) return;
  if(u.pathname===location.pathname&&u.hash) return;
  e.preventDefault(); leave(()=>{location.href=a.href});
},true);
document.addEventListener('submit',e=>{
  const f=e.target; if(!f.matches||!f.matches('form[data-go]')||e.defaultPrevented) return;
  if(f.checkValidity&&!f.checkValidity()) return;
  e.preventDefault(); leave(()=>HTMLFormElement.prototype.submit.call(f));
},true);
addEventListener('pageshow',e=>{if(e.persisted){st.mode='idle'; if(cv){cv.remove(); cv=null}}});
})();
'''
IBRAHIM_WEBP = base64.b64decode('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')

def login_page(err):
    msgs = {"bad": "Wrong username or password.", "locked": "Too many attempts. Try again in 15 minutes."}
    m = msgs.get(err, "")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>GridPrint · sign in</title><script src="{PREFIX}/static/wave.js"></script><link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource-variable/bricolage-grotesque@5.3.0/wdth.css" integrity="sha384-WQqnp9hDfoH3W8tz8N27r/2KCPxaxoS9L8KDqmXZdjhQOsnqVEV0GNrH+R6M0y4M" crossorigin="anonymous">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-sans@5.3.0/400.css" integrity="sha384-by8hCwJDxTOUHMGBI4elNkybAoX7Gs45s1pbgL9fdUTDy+tXTA+tZmiU+0LkwckA" crossorigin="anonymous">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-sans@5.3.0/600.css" integrity="sha384-1dcQop1Fsaw4/4DmZbwqeTSgod1K+Zkz3QWtbAUGxmw7ECfIGKOHUIRKcGtpdNHx" crossorigin="anonymous"><style>{BASE_CSS}
body{{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}}
form{{width:100%;max-width:360px;background:var(--panel);border:1px solid var(--line);border-top:3px solid var(--acc);border-radius:12px;padding:26px}}
h1{{margin:10px 0 18px;font:720 34px/1 var(--disp);font-stretch:82%;letter-spacing:-.02em}}label{{display:block;margin:12px 0 5px;color:var(--mut);font-size:12px}}
input[type=text],input[type=password]{{width:100%;padding:10px 12px}}.row{{display:flex;align-items:center;gap:8px;margin:14px 0 18px;color:var(--mut);font-size:13px}}
.row label{{margin:0}}button{{width:100%;padding:10px}}
.err{{background:#2a1416;color:#ff9a9e;border:1px solid #5a2328;border-radius:8px;padding:8px 10px;margin-bottom:6px;font-size:13px}}
.new{{margin:16px 0 0;padding-top:14px;border-top:1px solid var(--line);font-size:13px;color:var(--mut)}}.new a{{font-weight:600}}
</style></head><body><form method="post" action="{PREFIX}/login" autocomplete="on" data-go>
<a class="k" href="{PREFIX}/" data-go>GridPrint</a><h1>Sign in</h1>{f'<div class="err">{m}</div>' if m else ''}
<label for="u">Username</label><input id="u" type="text" name="username" autocomplete="username" required maxlength="64" autofocus>
<label for="p">Password</label><input id="p" type="password" name="password" autocomplete="current-password" required maxlength="256">
<div class="row"><input id="r" type="checkbox" name="remember" value="1"><label for="r">Remember me on this device (admins, 30 days)</label></div>
<button class="pri" type="submit">Sign in</button>
<p class="new">New here? Create an account: <a href="{PREFIX}/team" data-go>contact the GridPrint team</a>. Accounts are set up by hand.</p>
<p class="sub" style="margin:10px 0 0"><a href="{PREFIX}/" data-go>What is GridPrint?</a></p>
</form></body></html>"""


EXPLORER_HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>GridPrint · explorer</title>
<script src="__PREFIX__/static/wave.js"></script>
<link rel="stylesheet" href="__UPLOT_CDN__uPlot.min.css" integrity="__UPLOT_CSS_SRI__" crossorigin="anonymous">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource-variable/bricolage-grotesque@5.3.0/wdth.css" integrity="sha384-WQqnp9hDfoH3W8tz8N27r/2KCPxaxoS9L8KDqmXZdjhQOsnqVEV0GNrH+R6M0y4M" crossorigin="anonymous">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-sans@5.3.0/400.css" integrity="sha384-by8hCwJDxTOUHMGBI4elNkybAoX7Gs45s1pbgL9fdUTDy+tXTA+tZmiU+0LkwckA" crossorigin="anonymous">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-sans@5.3.0/600.css" integrity="sha384-1dcQop1Fsaw4/4DmZbwqeTSgod1K+Zkz3QWtbAUGxmw7ECfIGKOHUIRKcGtpdNHx" crossorigin="anonymous">
<style>__BASE_CSS__
/* ---------- page */
html{scroll-behavior:auto}
body{min-height:100vh;background:
  radial-gradient(1200px 600px at 70% -10%,rgba(242,162,58,.07),transparent 60%),
  linear-gradient(rgba(132,148,178,.035) 1px,transparent 1px) 0 0/100% 32px,
  linear-gradient(90deg,rgba(132,148,178,.035) 1px,transparent 1px) 0 0/32px 100%,var(--bg)}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.panel{background:linear-gradient(180deg,rgba(23,35,61,.92),rgba(18,29,51,.92));border:1px solid var(--line);border-radius:18px;padding:18px 20px;position:relative;min-width:0;
  box-shadow:0 1px 0 rgba(255,255,255,.03) inset,0 20px 40px -24px rgba(0,0,0,.6)}
.lbl{color:var(--mut);font-size:13px;letter-spacing:0;text-transform:none;font-weight:600}
.big{font:680 34px/1.05 var(--disp);font-stretch:86%;font-variant-numeric:tabular-nums}
.mid{font:620 20px/1.25 var(--disp);font-stretch:92%;font-variant-numeric:tabular-nums}
button{transition:transform .12s,border-color .2s,background .2s}button:active{transform:scale(.96)}
.ghost{background:transparent}

/* ---------- app bar */
.appbar{position:sticky;top:0;z-index:40;display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:12px clamp(14px,3vw,32px);
  background:rgba(10,17,32,.72);backdrop-filter:blur(14px) saturate(1.2);border-bottom:1px solid rgba(36,50,79,.7)}
.brand{display:flex;align-items:center;gap:9px;font:700 21px/1 var(--disp);font-stretch:80%;color:var(--ink);text-decoration:none;view-transition-name:brand}
.brand svg{width:28px;height:16px}
.crumb{color:var(--mut);font-size:14px;padding-left:12px;border-left:1px solid var(--line)}
.stn{display:flex;align-items:center;gap:8px;color:var(--mut);font-size:13px}
.grow{flex:1}
.pill{display:inline-flex;align-items:center;gap:8px;padding:6px 12px 6px 10px;border-radius:999px;border:1px solid var(--line);font-size:13px;font-weight:600;color:var(--ink);background:rgba(18,29,51,.8)}
.pill i{width:8px;height:8px;border-radius:50%;background:var(--mut);box-shadow:0 0 0 0 currentColor}
.who{display:flex;gap:10px;align-items:center}.badge{font-size:12px;padding:2px 9px;border-radius:20px;border:1px solid var(--line);color:var(--mut)}
.badge.admin{color:var(--acc);border-color:var(--acc)}

main{padding:18px clamp(14px,3vw,32px) 60px;max-width:1400px;margin:0 auto}

/* ---------- instrument deck */
.deck{display:grid;grid-template-columns:1.35fr 1fr 1fr;gap:14px;margin-bottom:14px}
.meter{display:grid;grid-template-columns:minmax(0,1fr) minmax(220px,300px);grid-template-rows:auto 1fr auto;gap:2px 18px;min-height:220px;overflow:visible}
.meter .lbl{grid-column:1/3}
.odo{grid-column:1;grid-row:2;display:flex;align-items:flex-end;font:720 clamp(48px,5.4vw,78px)/1 var(--disp);font-stretch:84%;letter-spacing:-.02em;height:1em;overflow:hidden;align-self:center;white-space:nowrap;font-variant-numeric:tabular-nums}
.odo .col{display:inline-block;height:1em;overflow:hidden;position:relative}
.odo .strip{display:flex;flex-direction:column;will-change:transform}
.odo .strip span{height:1em;display:block;text-align:center}
.odo .u{font-size:.36em;color:var(--mut);margin-left:.25em;align-self:flex-end;padding-bottom:.18em;font-stretch:100%;letter-spacing:0}
.gauge{grid-column:2;grid-row:1/4;width:100%;justify-self:end;align-self:center;overflow:visible;margin-top:26px}
.gauge .tick{stroke:#3A4C74;stroke-width:1.4}.gauge .tick.major{stroke:#7B8DB3;stroke-width:2}
.gauge text{fill:var(--mut);font:12px 'IBM Plex Sans',system-ui;text-anchor:middle}
.dev{grid-column:1;grid-row:3;color:var(--mut);font-size:14px}
.dev b{color:var(--ink);font-weight:600}
.kv{display:flex;justify-content:space-between;gap:12px;padding:9px 0 0;margin-top:9px;border-top:1px solid var(--line);font-size:14px}
.kv span{color:var(--mut)}.kv b{font-weight:600;text-align:right}
.status .big{margin:6px 0 4px}.why{margin:12px 0 0;color:#C4CEE0;font-size:14px;line-height:1.55}.why b{display:block;color:var(--mut);font-size:12px;font-weight:600;margin-bottom:2px}#cCause{min-height:1.2em}
.range .mid{margin:6px 0 2px}

/* ---------- controls */
.controls{position:sticky;top:62px;z-index:30;display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 -6px 12px;padding:8px 6px;
  background:linear-gradient(rgba(10,17,32,.86),rgba(10,17,32,.7));backdrop-filter:blur(10px);border-radius:14px}
.seg,.grp{display:flex;gap:2px;align-items:center;background:rgba(18,29,51,.9);border:1px solid var(--line);border-radius:12px;padding:4px;position:relative}
.seg button,.grp button{white-space:nowrap}.seg button,.grp button,.grp input{border:0;background:transparent;padding:7px 11px;border-radius:9px;position:relative;z-index:1}
.grp input{background:#0F1A30;border:1px solid #22314F}
.seg button.on{background:transparent;color:#1A1205;font-weight:600}
.seg.fx button.on{background:transparent}
.seg:not(.fx) button.on{background:var(--acc)}
.thumb{position:absolute;top:4px;left:4px;height:calc(100% - 8px);border-radius:9px;background:var(--acc);z-index:0;box-shadow:0 6px 20px -6px rgba(242,162,58,.6);display:none}
.seg.fx .thumb{display:block}
.grp button:hover,.seg button:not(.on):hover{background:rgba(132,148,178,.12)}
#ai{background:var(--acc);color:#1A1205;font-weight:600}

/* ---------- chart stage */
.stagewrap{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:14px;align-items:start;margin-bottom:14px}
.chartcol{min-width:0}
#chart{background:linear-gradient(180deg,#14203A,#101A2F);border:1px solid var(--line);border-radius:18px;padding:12px 8px 4px;position:relative;min-height:420px;overflow:hidden}
#chart::after{content:"";position:absolute;inset:0;pointer-events:none;border-radius:18px;box-shadow:inset 0 0 80px rgba(10,17,32,.55)}
#msg{position:absolute;inset:0;display:none;align-items:center;justify-content:center;color:var(--mut);font-size:15px;pointer-events:none;z-index:2}
#tip{position:absolute;display:none;pointer-events:none;z-index:10;background:rgba(10,17,32,.92);backdrop-filter:blur(6px);border:1px solid #33456D;border-radius:10px;padding:7px 11px;font-size:12px;line-height:1.5;font-variant-numeric:tabular-nums;white-space:nowrap;box-shadow:0 10px 24px rgba(0,0,0,.5)}
#tip b{color:var(--acc);font-size:16px}#tip .tt{color:var(--mut);font-size:12px}
.u-select{background:rgba(242,162,58,.16)!important;border-left:1px solid var(--acc);border-right:1px solid var(--acc)}
#slider{position:relative;height:34px;margin:10px 6px 2px;user-select:none;touch-action:none}
#slTrack{position:absolute;left:0;right:0;top:13px;height:8px;border-radius:6px;background:#131F38;border:1px solid var(--line)}
#slWin{position:absolute;top:5px;height:24px;border-radius:8px;background:rgba(242,162,58,.2);border:1px solid var(--acc);cursor:grab;min-width:14px;box-shadow:0 0 18px -4px rgba(242,162,58,.5)}
#slWin:active{cursor:grabbing}
.slH{position:absolute;top:-1px;width:12px;height:24px;border-radius:6px;background:var(--acc);cursor:ew-resize}
.slH::after{content:"";position:absolute;left:5px;top:6px;width:2px;height:10px;background:#1A1205;border-radius:1px}
#slL{left:-7px}#slR{right:-7px}
.slLbl{display:flex;justify-content:space-between;color:var(--mut);font-size:12px;margin:0 6px;font-variant-numeric:tabular-nums}

/* ---------- side panel: point details */
#details{width:320px;padding:16px 18px}
#details .row{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-bottom:1px solid var(--line);font-size:14px;font-variant-numeric:tabular-nums}
#details .row span:first-child{color:var(--mut)}#details .row span:last-child{text-align:right}
#details ul{margin:12px 0 0;padding-left:18px;color:var(--mut);font-size:13px;line-height:1.6}
.phead{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.x{padding:3px 10px;font-size:18px;line-height:1;background:transparent}

/* ---------- lower */
.lower{display:grid;grid-template-columns:1.4fr 1fr;gap:14px}
.lower h2{margin:0 0 10px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:14px}td,th{padding:8px 8px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--mut);font-weight:600;font-size:13px}
tr.ev{cursor:pointer;transition:background .2s}tr.ev:hover td{background:rgba(242,162,58,.06)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px;vertical-align:middle;background:var(--bad)}

/* ---------- orb (probable cause) */
.orb{position:absolute;top:14px;right:14px;width:40px;height:40px;padding:0;border-radius:50%;border:1px solid var(--line);background:#121D33;cursor:pointer;display:grid;place-items:center;transition:transform .25s;z-index:3}
.orb svg{width:28px;height:28px}.orb .ring{fill:none;stroke:currentColor;stroke-width:1.6;opacity:.35}
.orb .wave{fill:none;stroke:currentColor;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:80;stroke-dashoffset:0}
.orb.calm{color:#3fbf7f}.orb.active{color:#e6a23c}.orb.stressed{color:#e5484d}.orb.unknown{color:#8d8b84}.orb.backup{color:#a493ff}
.orb{animation:breathe 3.2s ease-in-out infinite}.orb.backup{animation-duration:5s}.orb.stressed{animation-duration:1.4s}.orb.active{animation-duration:2.2s}
@keyframes breathe{0%,100%{box-shadow:0 0 0 0 currentColor}50%{box-shadow:0 0 16px -2px currentColor}}
.orb:hover,.orb.open{transform:scale(1.12)}.orb:hover .wave,.orb.open .wave{animation:trace 1.1s ease-in-out infinite}
@keyframes trace{0%{stroke-dashoffset:80}60%{stroke-dashoffset:0}100%{stroke-dashoffset:-80}}
.orbpop{display:none;position:absolute;top:62px;right:8px;z-index:50;width:min(380px,86vw);background:rgba(18,29,51,.96);backdrop-filter:blur(10px);border:1px solid #33456D;border-radius:14px;padding:14px 16px;box-shadow:0 20px 40px rgba(0,0,0,.5);font-size:13px;line-height:1.5}
.orbpop.show{display:block;animation:pop .22s cubic-bezier(.2,.9,.3,1.2)}@keyframes pop{from{opacity:0;transform:translateY(-6px) scale(.97)}to{opacity:1;transform:none}}
.orbpop h4{margin:0 0 6px;font-size:12px;color:var(--mut);font-weight:600}.orbpop .cause{color:var(--ink);font-size:15px;line-height:1.5}.orbpop .mood{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;vertical-align:1px;background:currentColor}
.orbpop .tag{font-size:12px;color:var(--mut);display:block;margin-top:2px}
.tjump{color:var(--acc);text-decoration:none;border-bottom:1px dotted var(--acc);cursor:pointer}.tjump:hover{background:rgba(242,162,58,.15)}

/* ---------- drawer: AI report */
#scrim{position:fixed;inset:0;z-index:60;background:rgba(5,9,18,.55);backdrop-filter:blur(3px);display:none}
#reportCard{position:fixed;top:0;right:0;bottom:0;z-index:61;width:min(720px,94vw);display:none;border-radius:22px 0 0 22px;padding:22px 26px;overflow:auto;
  background:linear-gradient(180deg,#16233F,#111C33);border:1px solid var(--line);box-shadow:-30px 0 60px rgba(0,0,0,.45)}
#report{line-height:1.65;color:var(--ink);font-size:15px}
#report h2{font:700 22px/1.2 var(--disp);font-stretch:88%;margin:22px 0 8px;color:var(--acc)}#report h3{font-size:15px;margin:16px 0 4px}
#report ul{margin:4px 0;padding-left:20px}#report p{margin:8px 0}
.tw{overflow-x:auto;margin:8px 0}table.rt{border-collapse:collapse;width:100%;font-size:13px}table.rt th,table.rt td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}

dialog{background:linear-gradient(180deg,#16233F,#111C33);color:var(--ink);border:1px solid var(--line);border-radius:18px;max-width:820px;width:94%;padding:22px}
dialog::backdrop{background:rgba(5,9,18,.6);backdrop-filter:blur(3px)}
.adminonly{display:none!important}body.admin .adminonly{display:revert!important}
#wipe{position:fixed;inset:0;z-index:100;display:none;background:#0A1120}#wipe i{position:absolute;left:0;right:0;top:50%;height:2px;background:#F2A23A;box-shadow:0 0 24px #F2A23A;transform-origin:left}

@media (max-width:1100px){.deck{grid-template-columns:1fr 1fr}.meter{grid-column:1/3}.lower{grid-template-columns:1fr}}
@media (max-width:860px){.stagewrap{grid-template-columns:1fr}#details{width:auto}.controls{position:static}.crumb{display:none}}
@media (max-width:600px){.seg{overflow-x:auto;max-width:100%;scrollbar-width:none}.controls>*{max-width:100%}.grp{flex-wrap:wrap}.grp input[type=datetime-local]{flex:1 1 100%;min-width:0}.deck{grid-template-columns:1fr}.meter{grid-column:auto;grid-template-columns:1fr}.gauge{grid-column:1;grid-row:auto;justify-self:center;max-width:260px}.who #me{display:none}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<header class="appbar">
 <a class="brand" href="__PREFIX__/" data-go aria-label="GridPrint home"><svg viewBox="0 0 30 18" aria-hidden="true"><path d="M1 9 Q4.5 1 8 9 T15 9 T22 9 T29 9" fill="none" stroke="#F2A23A" stroke-width="2.4" stroke-linecap="round"/></svg>GridPrint</a>
 <span class="crumb">Explorer</span>
 <label class="stn">Station <select id="station"></select></label>
 <span class="grow"></span>
 <span class="pill" id="pill" aria-hidden="true"><i></i><span id="pillTxt">connecting</span></span>
 <div class="who"><span id="me" class="sub"></span><span id="role" class="badge"></span>
  <button id="adminBtn" class="adminonly ghost">Admin</button>
  <form method="post" action="__PREFIX__/logout" style="margin:0" data-go><button class="ghost" type="submit">Sign out</button></form></div>
</header>
<main>
<section class="deck">
 <article class="panel meter" aria-label="Frequency now">
  <div class="lbl">Frequency now</div>
  <div class="odo" id="odo" aria-hidden="true"></div><div id="cNow" class="sr">–</div>
  <svg class="gauge" id="gauge" viewBox="0 0 300 175" aria-hidden="true">
   <g id="gBands"></g><g id="gTicks"></g>
   <text x="34" y="172">49.5</text><text x="150" y="18">50.0</text><text x="266" y="172">50.5</text>
   <g id="needle"><line x1="150" y1="150" x2="150" y2="38" stroke="#F2A23A" stroke-width="3" stroke-linecap="round"/><circle cx="150" cy="150" r="8" fill="#F2A23A"/><circle cx="150" cy="150" r="3" fill="#0A1120"/></g>
  </svg>
  <div class="dev" id="dev">&nbsp;</div>
  <button id="orb" class="orb unknown" aria-label="Probable cause of recent changes" title="Probable cause of recent changes">
   <svg viewBox="0 0 40 40" aria-hidden="true"><circle class="ring" cx="20" cy="20" r="17"/><path class="wave" d="M5 20 H11 L14 12 L18 28 L22 9 L26 25 L29 20 H35"/></svg>
  </button>
  <div id="orbPop" class="orbpop" role="tooltip"><div class="sub">Reading the grid…</div></div>
 </article>
 <article class="panel status"><div class="lbl">Status</div><div class="big" id="cStatus">–</div><div class="sub" id="cCause"></div>
  <div class="kv"><span>Last reading</span><b id="cAgo">–</b></div>
  <p class="why" id="why"></p></article>
 <article class="panel range"><div class="lbl">Showing</div><div class="mid" id="cRange">–</div>
  <div class="kv"><span>Mean ± std dev</span><b id="cMean">–</b></div>
  <div class="kv"><span>Outage time · glitches</span><b id="cOut">–</b></div></article>
</section>
<nav class="controls" aria-label="Time range">
 <div class="seg" id="presets"><span class="thumb" id="thumb"></span>
  <button data-p="live" class="on">Live 15 min</button><button data-p="3600">1 h</button><button data-p="21600">6 h</button>
  <button data-p="86400">24 h</button><button data-p="today">Today</button><button data-p="yesterday">Yesterday</button><button data-p="604800">7 days</button>
 </div>
 <div class="grp"><button id="back" title="Step back" aria-label="Step back">◀</button><input type="date" id="day" title="Pick a day"><button id="fwd" title="Step forward" aria-label="Step forward">▶</button></div>
 <div class="grp"><input type="datetime-local" id="from" aria-label="From"><span class="sub">→</span><input type="datetime-local" id="to" aria-label="To"><button id="go">Show</button></div>
 <div class="grp"><button id="zIn" title="Zoom in" aria-label="Zoom in">＋</button><button id="zOut" title="Zoom out" aria-label="Zoom out">－</button><button id="reset">Reset zoom</button><button id="detBtn" title="Show point details">Details</button><button id="dl" class="adminonly">Download CSV</button><button id="ai" class="adminonly">✦ AI report</button></div>
</nav>
<section class="stagewrap">
 <div class="chartcol">
  <div id="chart" data-lenis-prevent><div id="msg"></div><div id="tip"></div></div>
  <div id="slider" title="Drag the window to move along the timeline; drag its edges to zoom"><div id="slTrack"></div><div id="slWin"><div class="slH" id="slL"></div><div class="slH" id="slR"></div></div></div>
  <div class="slLbl"><span id="slA"></span><span id="slB"></span></div>
 </div>
 <aside class="panel" id="details"><div class="phead"><div class="lbl">Point details</div><button class="x" id="detClose" title="Close" aria-label="Close point details">×</button></div>
  <div id="detBody"><div class="sub">Hover over the graph to see the full reading here.</div></div>
  <ul><li>Drag across the graph to zoom in, or use ＋ / － and the slider. Scroll zooms gently; Shift+scroll moves along the timeline. Double-click resets.</li>
  <li>Red shading: outage. Purple: mains off, socket on an inverter (not grid data). Yellow ticks: glitch seconds.</li>
  <li>Click a row in the table below to jump to it.</li></ul></aside>
</section>
<section class="lower">
 <article class="panel"><h2 class="lbl">Outages and backup power in this range</h2>
  <table><thead><tr><th>Start</th><th>End</th><th>Duration</th><th>Likely cause</th></tr></thead><tbody id="events"><tr><td colspan="4" class="sub">–</td></tr></tbody></table></article>
 <article class="panel"><h2 class="lbl">Range statistics</h2><table><tbody id="stats"></tbody></table>
  <div class="sub adminonly" style="margin-top:10px">Raw daily files (UTC days): <span id="days"></span></div></article>
</section>
</main>
<div id="scrim"></div>
<aside id="reportCard" data-lenis-prevent aria-label="AI report"><div class="phead"><div class="lbl">AI report · Gemini</div><div style="display:flex;gap:8px"><button id="copyRep">Copy</button><button class="x" id="repClose" aria-label="Close report">×</button></div></div><div id="report"></div></aside>
<dialog id="adminDlg" data-lenis-prevent><div class="phead"><div class="lbl">Admin</div><button id="closeAdmin">Close</button></div>
 <div class="lbl" style="margin:10px 0 6px">Gemini (AI reports)</div>
 <div class="bar" style="display:flex;gap:8px;flex-wrap:wrap"><input id="gKey" type="password" placeholder="Paste Gemini API key" style="flex:1;min-width:220px" autocomplete="off">
  <input id="gModel" type="text" placeholder="model" style="width:200px"><button id="saveKey" class="pri">Save</button></div>
 <div id="keyState" class="sub"></div>
 <div class="lbl" style="margin:18px 0 6px">Signed-in devices</div>
 <table><thead><tr><th>User</th><th>Since</th><th>Until</th><th>Device</th><th></th></tr></thead><tbody id="sessRows"></tbody></table>
 <div style="margin-top:12px"><button id="revokeAll">Sign out every device</button></div>
</dialog>
<script src="__UPLOT_CDN__uPlot.iife.min.js" integrity="__UPLOT_JS_SRI__" crossorigin="anonymous"></script>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.15.1/dist/gsap.min.js" integrity="sha384-3vCAhD+a3BOliP3QqjQINn6ZWiZxoqu0WFxX5ZQZgqekDZuSb1mJBrrLMmu94H8f" crossorigin="anonymous"></script>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.15.1/dist/Flip.min.js" integrity="sha384-xR88kf2b8M3o5d6ggk7esxOrWIOpcbo46xvxry4lyanM4R2V7ZMwCgrEwNOfsQt0" crossorigin="anonymous"></script>
<script src="https://cdn.jsdelivr.net/npm/lenis@1.3.29/dist/lenis.min.js" integrity="sha384-OHGPFWKcc+jP8Q7LpOJtmcyBSz6L1L/nH2wKWNQ456cvisoFPacxtBsDQPmwEjLu" crossorigin="anonymous"></script>
<script src="__PREFIX__/static/app.js"></script>
<script src="__PREFIX__/static/app-fx.js"></script>
</body></html>
'''
APP_FX = r'''// GridPrint explorer: motion and instruments layered on top of app.js. app.js keeps doing the data work and
// writes plain text into the page; this file watches those elements and turns each change into motion.
(()=>{
'use strict';
const G=window.gsap, $=id=>document.getElementById(id);
const RM=matchMedia('(prefers-reduced-motion: reduce)').matches;
const anim=!!G&&!RM;
const C={sodium:'#F2A23A',violet:'#A493FF',ok:'#3DD68C',bad:'#F2555A',warn:'#FF8F6B',mut:'#8494B2'};
const watch=(el,fn,opts)=>{if(!el) return; new MutationObserver(fn).observe(el,opts||{childList:true,characterData:true,subtree:true}); fn()};

// ---------------------------------------------------------------- odometer
const odo=$('odo'); let odoText='';
function buildOdo(txt){
  odo.innerHTML='';
  const m=txt.match(/^([\d.]+)\s*(Hz)?$/);
  if(!m){const s=document.createElement('span'); s.textContent=txt==='–'?'—':txt; s.style.fontSize=txt.length>4?'.6em':''; odo.appendChild(s); return}
  for(const ch of m[1]){
    if(/\d/.test(ch)){const col=document.createElement('span'); col.className='col'; const strip=document.createElement('span'); strip.className='strip';
      for(let d=0;d<10;d++){const s=document.createElement('span'); s.textContent=d; strip.appendChild(s)} col.appendChild(strip); col.dataset.d='0'; odo.appendChild(col)}
    else{const s=document.createElement('span'); s.textContent=ch; odo.appendChild(s)}
  }
  if(m[2]){const u=document.createElement('span'); u.className='u'; u.textContent='Hz'; odo.appendChild(u)}
}
function setOdo(txt){
  const shape=t=>t.replace(/\d/g,'0');
  if(shape(txt)!==shape(odoText)||!odo.firstChild) buildOdo(txt);
  odoText=txt;
  const digits=(txt.match(/[\d.]+/)||[''])[0].replace('.','');
  odo.querySelectorAll('.col').forEach((col,i)=>{const d=+digits[i]; if(col.dataset.d===String(d)&&col._set) return;
    const strip=col.firstChild, y=-d*10+'%';
    if(G){ if(anim&&col._set) G.to(strip,{yPercent:-d*10,duration:.55+i*.05,ease:'power3.out',overwrite:true}); else G.set(strip,{yPercent:-d*10}) }
    else strip.style.transform=`translateY(${y})`;
    col.dataset.d=d; col._set=true});
}

// ---------------------------------------------------------------- gauge (49.5 to 50.5 Hz over 220 degrees)
const NS='http://www.w3.org/2000/svg', CX=150, CY=150, RAD=112, SWEEP=110;
const ang=f=>Math.max(-1,Math.min(1,(f-50)/0.5))*SWEEP;
function polar(a,r){const t=(a-90)*Math.PI/180; return [CX+r*Math.cos(t),CY+r*Math.sin(t)]}
function arc(a0,a1,r){const [x0,y0]=polar(a0,r),[x1,y1]=polar(a1,r); return `M${x0} ${y0} A${r} ${r} 0 ${a1-a0>180?1:0} 1 ${x1} ${y1}`}
function el(tag,attrs,parent){const e=document.createElementNS(NS,tag); for(const k in attrs) e.setAttribute(k,attrs[k]); parent.appendChild(e); return e}
(function buildGauge(){
  const b=$('gBands'), t=$('gTicks');
  el('path',{d:arc(-SWEEP,SWEEP,RAD),stroke:'#1B2945','stroke-width':16,fill:'none','stroke-linecap':'round'},b);
  el('path',{d:arc(ang(49.8),ang(50.2),RAD),stroke:'rgba(242,162,58,.22)','stroke-width':16,fill:'none'},b);
  el('path',{d:arc(ang(49.95),ang(50.05),RAD),stroke:'rgba(242,162,58,.5)','stroke-width':16,fill:'none'},b);
  window.__trail=el('path',{d:arc(0,0.01,RAD-22),stroke:C.sodium,'stroke-width':3,fill:'none','stroke-linecap':'round',opacity:.55},b);
  for(let f=49.5;f<=50.5001;f+=0.05){const a=ang(f), major=Math.abs((f*10)-Math.round(f*10))<1e-6, [x0,y0]=polar(a,RAD+11), [x1,y1]=polar(a,RAD+(major?22:17));
    el('line',{x1:x0,y1:y0,x2:x1,y2:y1,class:'tick'+(major?' major':'')},t)}
})();
const needle={a:-SWEEP}; let needleColor=C.sodium;
function setGauge(f,color){
  const a=f==null?-SWEEP:ang(f);
  const apply=()=>{$('needle').setAttribute('transform',`rotate(${needle.a} ${CX} ${CY})`);
    window.__trail.setAttribute('d',needle.a>=0?arc(0,Math.max(needle.a,0.01),RAD-22):arc(needle.a,0,RAD-22))};
  if(anim) G.to(needle,{a,duration:1.4,ease:'elastic.out(1,0.55)',onUpdate:apply}); else {needle.a=a; apply()}
  if(color!==needleColor){needleColor=color; $('needle').querySelectorAll('line,circle:first-of-type').forEach(n=>{n.setAttribute(n.tagName==='line'?'stroke':'fill',color)}); window.__trail.setAttribute('stroke',color)}
  const dev=$('dev');
  if(f==null){dev.innerHTML='&nbsp;';return}
  const mhz=Math.round((f-50)*1000);
  dev.innerHTML=mhz===0?'<b>exactly on 50 Hz</b>':`<b>${Math.abs(mhz)} mHz</b> ${mhz>0?'above':'below'} 50 Hz · ${mhz>0?'more power than demand':'more demand than power'}`;
}

// ---------------------------------------------------------------- live readout -> odometer, gauge, status pill
function syncNow(){
  const txt=$('cNow').textContent.trim(), st=$('cStatus').textContent.trim();
  setOdo(txt);
  const f=parseFloat(txt), live=!isNaN(f)&&/Hz/.test(txt);
  const color=st==='ON BACKUP'?C.violet:st==='OUTAGE'?C.bad:st==='GLITCH'?C.warn:live?C.sodium:C.mut;
  setGauge(live&&st!=='OUTAGE'?f:null,color);
  const pill=$('pill'), dot=pill.querySelector('i'), label=st&&st!=='–'?st.charAt(0)+st.slice(1).toLowerCase():'connecting';
  if($('pillTxt').textContent!==label){$('pillTxt').textContent=label; if(anim) G.fromTo(pill,{scale:.92},{scale:1,duration:.5,ease:'back.out(3)'})}
  dot.style.background=color; dot.style.color=color;
  if(anim&&!dot._pulse){dot._pulse=G.to(dot,{boxShadow:'0 0 0 7px rgba(0,0,0,0)',repeat:-1,duration:1.6,ease:'power2.out',startAt:{boxShadow:'0 0 0 0 currentColor'}})}
}
watch($('cNow'),syncNow); watch($('cStatus'),syncNow);

// ---------------------------------------------------------------- the probable cause, shown in the status panel too
watch($('orbPop'),()=>{const c=$('orbPop').querySelector('.cause'), w=$('why'); if(!c||!w) return; const t=c.textContent.trim(); if(w.dataset.t===t) return; w.dataset.t=t;
  w.innerHTML='<b>Probable cause</b>'; w.appendChild(document.createTextNode(t)); if(anim) G.fromTo(w,{opacity:0,y:8},{opacity:1,y:0,duration:.6,ease:'power3.out'})});

// ---------------------------------------------------------------- preset thumb that slides between buttons (GSAP Flip)
const seg=$('presets'), thumb=$('thumb');
function moveThumb(first){
  const on=seg.querySelector('button.on'); if(!on){thumb.style.opacity=0;return}
  thumb.style.opacity=1; const box={x:on.offsetLeft-4,w:on.offsetWidth};
  if(anim&&!first) G.to(thumb,{x:box.x,width:box.w,duration:.55,ease:'expo.out'}); else {thumb.style.width=box.w+'px'; thumb.style.transform=`translateX(${box.x}px)`; if(G) G.set(thumb,{x:box.x,width:box.w})}
}
if(seg){seg.classList.add('fx'); moveThumb(true); new MutationObserver(()=>moveThumb(false)).observe(seg,{attributes:true,subtree:true,attributeFilter:['class']}); addEventListener('resize',()=>moveThumb(true))}

// ---------------------------------------------------------------- chart: every new range eases in
let lastRange='';
watch($('cRange'),()=>{const r=$('cRange').textContent; if(r===lastRange) return; lastRange=r;
  if(!anim) return; const c=$('chart').querySelectorAll('canvas');
  G.fromTo(c,{opacity:.25,filter:'blur(4px) saturate(.4)'},{opacity:1,filter:'blur(0px) saturate(1)',duration:.7,ease:'power2.out',overwrite:true});
  G.fromTo('#cRange',{yPercent:30,opacity:0},{yPercent:0,opacity:1,duration:.5,ease:'power3.out'})});
watch($('chart'),m=>{if(!anim) return; const u=$('chart').querySelector('.uplot'); if(u&&!u._in){u._in=true; G.from(u,{clipPath:'inset(0 100% 0 0)',duration:1.1,ease:'expo.inOut',clearProps:'clipPath'})}},{childList:true});

// ---------------------------------------------------------------- tables: rows arrive in a quick cascade
function cascade(tb){let n=0; watch(tb,()=>{const rows=[...tb.children]; if(!anim||rows.length===n||rows.length>60){n=rows.length;return} n=rows.length;
  G.fromTo(rows,{opacity:0,y:10},{opacity:1,y:0,duration:.45,stagger:.025,ease:'power2.out',overwrite:true})},{childList:true})}
cascade($('events')); cascade($('stats'));
watch($('cMean'),()=>{if(anim) G.fromTo(['#cMean','#cOut'],{opacity:0,x:-8},{opacity:1,x:0,duration:.5,stagger:.06})},{childList:true,characterData:true,subtree:true});

// ---------------------------------------------------------------- side panel and report drawer
const det=$('details');
let detWas=det.style.display!=='none';
new MutationObserver(()=>{const open=det.style.display!=='none'; if(open===detWas) return; detWas=open;
  dispatchEvent(new Event('resize'));
  if(open&&anim) G.fromTo(det,{opacity:0,x:30},{opacity:1,x:0,duration:.6,ease:'expo.out'});
}).observe(det,{attributes:true,attributeFilter:['style']});
const card=$('reportCard'), scrim=$('scrim');
let closing=false, repOpen=false;
new MutationObserver(()=>{if(closing) return; const open=card.style.display==='block'; if(open===repOpen) return; repOpen=open; scrim.style.display=open?'block':'none';
  if(open&&anim){G.fromTo(card,{xPercent:100},{xPercent:0,duration:.75,ease:'expo.out'}); G.fromTo(scrim,{opacity:0},{opacity:1,duration:.4})}
}).observe(card,{attributes:true,attributeFilter:['style']});
watch($('report'),()=>{if(!anim) return; const kids=[...$('report').children]; if(kids.length>1) G.fromTo(kids,{opacity:0,y:14},{opacity:1,y:0,duration:.6,stagger:.04,ease:'power3.out',overwrite:true})},{childList:true});
function closeReport(){if(card.style.display!=='block') return;
  const done=()=>{closing=true; card.style.display='none'; scrim.style.display='none'; repOpen=false; closing=false};
  if(anim){G.to(card,{xPercent:100,duration:.5,ease:'expo.in',onComplete:done}); G.to(scrim,{opacity:0,duration:.4})} else done()}
$('repClose').addEventListener('click',closeReport); scrim.addEventListener('click',closeReport);
addEventListener('keydown',e=>{if(e.key==='Escape') closeReport()});

// ---------------------------------------------------------------- admin dialog
const dlg=$('adminDlg');
new MutationObserver(()=>{if(dlg.open&&anim) G.fromTo(dlg,{opacity:0,y:20,scale:.97},{opacity:1,y:0,scale:1,duration:.5,ease:'expo.out'})}).observe(dlg,{attributes:true,attributeFilter:['open']});

// ---------------------------------------------------------------- smooth scroll (the chart, report and dialog keep their own wheel)
if(window.Lenis&&anim){const lenis=new Lenis({lerp:0.1}); G.ticker.add(t=>lenis.raf(t*1000)); G.ticker.lagSmoothing(0)}

// ---------------------------------------------------------------- arrival and departure
if(anim){
  G.timeline({defaults:{ease:'expo.out'}})
    .from('.appbar',{y:-40,opacity:0,duration:.8})
    .from('.deck .panel',{y:40,opacity:0,duration:1,stagger:.09},.1)
    .from('.gauge',{rotate:-25,opacity:0,transformOrigin:'50% 86%',duration:1.4},.3)
    .from('.controls > *',{y:16,opacity:0,duration:.7,stagger:.05},.35)
    .from('#chart',{y:30,opacity:0,duration:1},.45)
    .from(['#slider','.slLbl'],{opacity:0,duration:.6},.8)
    .from('.lower .panel',{y:40,opacity:0,duration:.9,stagger:.1},.6);
}
})();
'''
APP_PAGE = (EXPLORER_HTML.replace("__BASE_CSS__", BASE_CSS).replace("__PREFIX__", PREFIX).replace("__UPLOT_CDN__", UPLOT_CDN)
            .replace("__UPLOT_CSS_SRI__", UPLOT_CSS_SRI).replace("__UPLOT_JS_SRI__", UPLOT_JS_SRI))


APP_JS = r"""
'use strict';
(async function(){
if(!window.uPlot){ await new Promise(res=>{const s=document.createElement('script');s.src='static/uplot.js';s.onload=res;s.onerror=res;document.head.appendChild(s);});
  const l=document.createElement('link');l.rel='stylesheet';l.href='static/uplot.css';document.head.appendChild(l); }
const PKT=5*3600, $=id=>document.getElementById(id), base=location.pathname.replace(/\/explorer\/?$/,'').replace(/\/$/,'');
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
    const onBk=(d.backup||[]).some(b=>t>=b[0]-d.bucket&&t<=b[1]);
    tip.innerHTML=`<div class="tt">${fmt12(t)}</div>`+(m!=null?`<b${onBk?' style="color:#a493ff"':''}>${m.toFixed(3)} Hz</b>`:`<b style="color:var(--bad)">no signal</b>`)+(onBk?'<div class="tt">inverter, not the grid</div>':'');
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
    cursor:{drag:{x:true,y:false,setScale:true},points:{size:9,fill:'#F2A23A',stroke:'#fff',width:1.5}},
    scales:{x:{time:true,min:from,max:to},y:{range:(u,mn,mx)=>{if(mn==null)return[49.5,50.5];const p=Math.max(0.01,(mx-mn)*0.12);return[mn-p,mx+p]}}},
    axes:[{stroke:'#8d8b84',grid:{stroke:'#1E2A44'},ticks:{stroke:'#2A3A5C'}},
          {stroke:'#8d8b84',grid:{stroke:'#1E2A44'},ticks:{stroke:'#2A3A5C'},size:64,values:(u,v)=>v.map(x=>x.toFixed(3))}],
    series:[{label:'Time (PKT)',value:(u,v)=>v==null?'–':fmtD(v)+' '+fmtT(v)},
            {label:'Frequency',stroke:'#F2A23A',width:1.6,value:fmtHz},
            {label:'Min',stroke:'rgba(242,162,58,.3)',width:0,value:fmtHz,points:{show:false}},
            {label:'Max',stroke:'rgba(242,162,58,.3)',width:0,value:fmtHz,points:{show:false}}],
    bands:[{series:[3,2],fill:'rgba(242,162,58,.16)'}],
    hooks:{
      drawClear:[u=>{const d=data;if(!d||!d.t.length)return;
        const ctx=u.ctx,{top,height}=u.bbox,x0=u.valToPos(d.t[0],'x',true),bw=Math.max(1.5,u.valToPos(d.t[0]+d.bucket,'x',true)-x0),tick=8*devicePixelRatio;
        ctx.save();
        for(const b of (d.backup||[])){const xa=u.valToPos(b[0],'x',true),xb=u.valToPos(b[1],'x',true);
          ctx.fillStyle='rgba(164,147,255,.13)';ctx.fillRect(xa,top,Math.max(2,xb-xa),height);
          ctx.fillStyle='rgba(164,147,255,.8)';ctx.fillRect(xa,top,Math.max(1,devicePixelRatio),height);
          ctx.font=`${11*devicePixelRatio}px system-ui`;const lb=b[3]?'on backup power':'backup power';
          if(xb-xa>ctx.measureText(lb).width+12*devicePixelRatio) ctx.fillText(lb,xa+6*devicePixelRatio,top+14*devicePixelRatio)}
        for(let i=0;i<d.t.length;i++){
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
    ['Outage time',dur(s.outage_s)],['On backup power',dur(s.backup_s||0)],['Glitch seconds',s.glitches],['Detail',d.bucket===1?'every second':'1 point per '+dur(d.bucket)]];
  $('stats').innerHTML=rows.map(r=>`<tr><td class="sub">${esc(r[0])}</td><td>${esc(r[1])}</td></tr>`).join('');
  const ev=d.events.map(e=>({a:+e[0],b:+e[1],dot:'',cause:diag(e[2])[0],tip:diag(e[2])[1]}))
    .concat((d.backup||[]).map(e=>({a:+e[0],b:+e[1],dot:' style="background:#a493ff"',cause:'Mains off, on inverter/UPS'+(e[3]?' (now)':''),
      tip:`The reading went flat at ${(+e[2]).toFixed(3)} Hz: that is the inverter's own clock, not the grid. Likely a local power cut or load-shedding.`})))
    .sort((x,y)=>y.a-x.a);
  $('events').innerHTML=ev.length?ev.slice(0,80).map(e=>`<tr class="ev" data-a="${e.a}" data-b="${e.b}"><td><span class="dot"${e.dot}></span>${fmtDT(e.a)}</td><td>${fmtT(e.b)}</td><td>${dur(e.b-e.a+1)}</td><td title="${esc(e.tip)}">${esc(e.cause)}</td></tr>`).join('')
    :'<tr><td colspan="4" class="sub">No outages or backup power in this range</td></tr>';
  document.querySelectorAll('tr.ev').forEach(tr=>tr.onclick=()=>{const a=+tr.dataset.a,b=+tr.dataset.b,p=Math.max(60,(b-a)*0.6);plot.setScale('x',{min:a-p,max:b+p})});
}

// ---- the live insight orb
let insight=null, insightAt=0, orbHide=0, bkWas=false;
async function loadInsight(){ if(!station) return;
  const r=await getJSON(`${base}/api/insight?station=${encodeURIComponent(station)}`).catch(()=>null); if(!r) return;
  insight=r; insightAt=Date.now(); $('orb').className='orb '+(r.mood||'unknown')+($('orbPop').classList.contains('show')?' open':''); renderInsight() }
function ago(s){return s<90?'just now':s<3600?Math.round(s/60)+' min ago':Math.round(s/3600)+' h ago'}
function renderInsight(){ const r=insight, el=$('orbPop'); if(!r){el.innerHTML='<div class="sub">Reading the grid…</div>';return}
  const mood={calm:'Calm',active:'Active',stressed:'Under stress',backup:'On backup power',unknown:'No data'}[r.mood]||'';
  const col={calm:'#3fbf7f',active:'#e6a23c',stressed:'#e5484d',backup:'#a493ff'}[r.mood]||'#8d8b84';
  const linkT=x=>esc(x||'').replace(/\b([01]?\d):([0-5]\d) (AM|PM)\b/g,(m,h,mi,ap)=>{const H=(+h%12)+(ap==='PM'?12:0);
    return `<a href="#" class="tjump" data-md="" data-hm="${String(H).padStart(2,'0')}:${mi}">${m}</a>`});
  el.innerHTML=`<h4><span class="mood" style="color:${col}"></span>${esc(mood)} · probable cause</h4><div class="cause">${linkT(r.cause)}</div>`
    +(r.basis||r.confidence?`<span class="tag" style="margin-top:8px">${esc([r.basis,r.confidence].filter(Boolean).join(' · '))}</span>`:'') }
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
  const r=await getJSON(`${base}/api/range?station=${encodeURIComponent(station)}&from=${t-300}&to=${t+5}&points=400`).catch(()=>null); if(!r) return;
  const ls=r.last_seen, st=$('cStatus');
  $('cAgo').textContent=ls?(t-ls<120?Math.round(t-ls)+' s ago':t-ls<7200?Math.round((t-ls)/60)+' min ago':fmtDT(ls)):'–';
  const i=r.t.length-1;
  if(i<0||!ls||t-ls>60){$('cNow').textContent='–';st.textContent=ls?'OFFLINE':'–';st.style.color='var(--mut)';return}
  const out=r.out[i]>0.5,gl=r.gl[i]>0;
  $('cNow').textContent=r.mean[i]==null?'–':r.mean[i].toFixed(3)+' Hz';
  const bk=(r.backup||[]).filter(b=>b[3]&&b[1]>=ls-40).pop();
  if(bk&&!out){st.textContent='ON BACKUP';st.style.color='#a493ff';st.title='Mains is off: the socket is running on an inverter/UPS, so this reading is the inverter\'s clock, not the grid.';
    $('cCause').textContent='Mains off since '+fmt12s(bk[0])+' · inverter, not grid';$('cCause').title=st.title;
    if(!bkWas){bkWas=true;loadInsight()} return}
  if(bkWas){bkWas=false;loadInsight()}
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
