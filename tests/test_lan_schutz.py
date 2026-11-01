#!/usr/bin/env python3
"""LAN-Port: nur der Browserpfad wird geschlossen, nicht der LAN-Zugriff selbst.

Die LAN-Adresse bleibt eine Vertrauenszone: Anfragen ohne Origin (curl, Agenten,
Server) gehen unveraendert durch. Ein Browser darf nur mit einem erlaubten Origin
sprechen; ein fremdes Origin bekommt 403, ein POST-Rumpf ohne application/json
415. OPTIONS antwortet kein pauschales "*" mehr. /admin/recheck ist zusaetzlich
gedrosselt, damit ein Aufruf nicht jedes Mal alle freien Wolkenmodelle prueft.

ROT gegen den Stand vor dem Fix: fremdes Origin kommt durch, text/plain wird
angenommen, OPTIONS liefert "*", und zwei Rechecks im Fenster sind beide 202.
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ["LLM_PROXY_ALLOWED_ORIGINS"] = "erlaubt.example"
os.environ["LLM_PROXY_RECHECK_MIN_INTERVAL"] = "300"

from _lade import lade  # noqa: E402

lp = lade()
lp.LOCAL_BACKENDS[:] = []        # keine Discovery auf echte Hosts
rechecks = []
lp._do_recheck = lambda reason: rechecks.append(reason)

from http.server import ThreadingHTTPServer  # noqa: E402

srv = ThreadingHTTPServer(("127.0.0.1", 0), lp.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
basis = "http://127.0.0.1:%d" % srv.server_address[1]
fail = 0


def check(cond, msg):
    global fail
    if not cond:
        print("ROT:", msg)
        fail += 1


def anfrage(methode, pfad, rumpf=None, kopf=None):
    req = urllib.request.Request(basis + pfad, data=rumpf, headers=kopf or {}, method=methode)
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


# 1) fremdes Origin -> 403, auch auf dem Verwaltungspfad
code, _, _ = anfrage("POST", "/v1/chat/completions",
                     json.dumps({"messages": []}).encode(),
                     {"Content-Type": "application/json", "Origin": "http://fremd.example"})
check(code == 403, f"fremdes Origin kam mit {code} durch, erwartet 403")
code, _, _ = anfrage("POST", "/admin/recheck", b"",
                     {"Origin": "http://fremd.example"})
check(code == 403, f"/admin/recheck mit fremdem Origin: {code}, erwartet 403")

# 2) ohne Origin geht derselbe Aufruf durch (kein 403)
code, _, _ = anfrage("POST", "/v1/chat/completions",
                     json.dumps({"messages": []}).encode(),
                     {"Content-Type": "application/json"})
check(code != 403, f"Anfrage ohne Origin wurde abgewiesen: {code}")

# 3) text/plain-Rumpf -> 415 (erzwingt den CORS-Preflight im Browser)
code, _, _ = anfrage("POST", "/v1/chat/completions", b"kein json",
                     {"Content-Type": "text/plain"})
check(code == 415, f"text/plain-Rumpf: {code}, erwartet 415")

# 4) OPTIONS: fremdes Origin bekommt keine CORS-Kopfzeilen, erlaubtes wird gespiegelt
code, h, _ = anfrage("OPTIONS", "/v1/chat/completions",
                     None, {"Origin": "http://fremd.example"})
check(h.get("Access-Control-Allow-Origin") != "*",
      "OPTIONS antwortet weiter mit Access-Control-Allow-Origin: *")
check("Access-Control-Allow-Origin" not in h,
      f"OPTIONS gab fremdem Origin CORS-Kopfzeilen: {h.get('Access-Control-Allow-Origin')}")
code, h, _ = anfrage("OPTIONS", "/v1/chat/completions",
                     None, {"Origin": "http://erlaubt.example"})
check(h.get("Access-Control-Allow-Origin") == "http://erlaubt.example",
      f"OPTIONS spiegelt erlaubtes Origin nicht: {h.get('Access-Control-Allow-Origin')!r}")

# 5) /admin/recheck zweimal im Fenster -> zweiter Aufruf 429
code1, _, _ = anfrage("POST", "/admin/recheck", b"")
code2, _, _ = anfrage("POST", "/admin/recheck", b"")
check(code1 == 202 and code2 == 429,
      f"Recheck-Fenster: {code1} und {code2}, erwartet 202 dann 429")
check(len(rechecks) == 1, f"Recheck lief {len(rechecks)} mal, erwartet genau einmal")

srv.shutdown()
print("GRUEN" if not fail else f"ROT ({fail})")
sys.exit(1 if fail else 0)
