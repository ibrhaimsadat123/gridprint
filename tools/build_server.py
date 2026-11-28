"""Rebuild server/gridprint_server.py from the editable page sources in server/web/.

The server is deliberately ONE file (easy to paste into PythonAnywhere's WSGI editor), so the pages live
inside it as string constants. Edit server/web/*, then run:   python3 tools/build_server.py
"""
import base64, os, ast
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRV, WEB = os.path.join(ROOT, "server", "gridprint_server.py"), os.path.join(ROOT, "server", "web")
s = open(SRV, encoding="utf-8").read()
rd = lambda n: open(os.path.join(WEB, n), encoding="utf-8").read()

def put(name, content):
    global s
    assert "'''" not in content, name + " must not contain triple quotes"
    a = s.index(name + " = r'''") + len(name + " = r'''"); b = s.index("'''", a); s = s[:a] + content + s[b:]

put("HOME_JS", rd("home.js").replace("__PROOF__", rd("proof.json")))   # proof.json: the A1 phone-vs-station match
put("HOME_HTML", rd("home.html"))
put("EXPLORER_HTML", rd("explorer.html"))
put("APP_FX", rd("app-fx.js"))
put("TEAM_HTML", rd("team.html"))
put("TEAM_JS", rd("team.js"))
put("WAVE_JS", rd("wave.js"))
k = "IBRAHIM_WEBP = base64.b64decode('"; a = s.index(k) + len(k); b = s.index("'", a)
s = s[:a] + base64.b64encode(open(os.path.join(WEB, "ibrahim.webp"), "rb").read()).decode() + s[b:]
ast.parse(s)
open(SRV, "w", encoding="utf-8").write(s)
print("rebuilt", SRV, len(s), "bytes")
