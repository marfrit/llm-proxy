#!/usr/bin/env python3
"""Warmes Material und keine toten Sendungen (Re-Check 2026-11-01).

ROT, wenn der Materialaufbau im ersten Request statt im Warmlauf passiert, der
Request ihn also zahlt, oder ein Anrufer, der waehrend der Vorbereitung auflegt,
trotzdem an die Engine geschickt wird.
"""
import http.server
import json
import os
import socket
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402
from _feyntalk_fixture import MODEL, make_material, start_engine  # noqa: E402

lp = lade()
fail = 0
checks = 0
CAPS = {"supports_preserve_thinking": True, "supports_object_arguments": False}


def check(cond, msg):
    global fail, checks
    checks += 1
    if not cond:
        print("ROT:", msg)
        fail += 1


def reset(host, port, setting="tokens"):
    lp.LOCAL_BACKENDS[:] = [("eng", host, port)]
    lp.BACKEND_FEYNTALK.clear()
    lp.BACKEND_FEYNTALK["eng"] = setting
    lp.MODEL_ALIASES = {}
    lp._FEYNTALK_WARNED.clear()
    with lp.FEYNTALK_CACHE_LOCK:
        lp.FEYNTALK_CACHE.clear()
        lp.FEYNTALK_MATERIALS.clear()
    getattr(lp, "_FEYNTALK_WARM", {}).clear()
    lp._build_sems({"eng": 4})
    lp._disc_cache.clear()


def proxy_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lp.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def post(port, path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


material = make_material()

# 1) Warmlauf: der Aufbau passiert im Hintergrund, nicht im ersten Request.
srv, E = start_engine(material, caps=CAPS, n_ctx=100000, renderer_delay=1.0)
reset(*srv.server_address[:2], "tokens")
del lp._testlog[:]
getattr(lp, "_feyntalk_warm_all", lambda: None)()
ende = time.monotonic() + 6.0
while time.monotonic() < ende and not lp.FEYNTALK_MATERIALS:
    time.sleep(0.05)
check(bool(lp.FEYNTALK_MATERIALS), "Material wurde nicht warmgebaut")
check(E.renderer_hits == 1, f"Renderer-Endpunkt {E.renderer_hits}x statt 1x")
check(any("material warmed in" in z for z in lp._testlog),
      f"keine Warmlauf-Zeile: {lp._testlog[-3:]}")
px = proxy_server()
pport = px.server_address[1]
hits = E.renderer_hits
t0 = time.monotonic()
post(pport, "/v1/chat/completions",
     {"model": MODEL, "stream": True, "messages": [{"role": "user", "content": "hi"}]})
dt = time.monotonic() - t0
check(E.renderer_hits == hits, f"erster Request holte das Material erneut ({E.renderer_hits})")
check(dt < 0.5, f"erster Request zahlte den Bau: {dt:.2f}s")
px.shutdown()
srv.shutdown()

# 2) Keine tote Sendung: der Anrufer legt waehrend der Vorbereitung auf.
srv, E = start_engine(material, caps=CAPS, n_ctx=100000, renderer_delay=1.0)
reset(*srv.server_address[:2], "tokens")
px = proxy_server()
pport = px.server_address[1]
del lp._testlog[:]
raw = json.dumps({"model": MODEL, "stream": True,
                  "messages": [{"role": "user", "content": "hi"}]}).encode()
s = socket.create_connection(("127.0.0.1", pport), timeout=10)
s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
          b"Content-Type: application/json\r\nContent-Length: "
          + str(len(raw)).encode() + b"\r\n\r\n" + raw)
time.sleep(0.1)
s.close()
time.sleep(2.0)
check("POST /v1/responses" not in E.paths and "POST /v1/chat/completions" not in E.paths,
      f"toter Anrufer wurde an die Engine geschickt: {E.paths}")
check(lp._backend_inflight.get("eng", 0) == 0,
      f"Slot nach totem Anrufer nicht frei: {lp._backend_inflight.get('eng')}")
check(any("CLIENT-CANCELLED before send" in z for z in lp._testlog),
      f"keine CLIENT-CANCELLED-Zeile: {lp._testlog[-3:]}")
px.shutdown()
srv.shutdown()


# 3) Ein Faden-Start-Fehler setzt das Event und raeumt den Eintrag weg.
class _BoomThread:
    def __init__(self, *a, **k):
        raise RuntimeError("kein Faden")


_orig_thread = lp.threading.Thread
lp.threading.Thread = _BoomThread
getattr(lp, "_FEYNTALK_WARM", {}).clear()
t0 = time.monotonic()
res = lp._feyntalk_warm_and_wait("eng", "127.0.0.1", 1, "hash")
dt = time.monotonic() - t0
lp.threading.Thread = _orig_thread
check(res is None and dt < 2.0, f"Faden-Start-Fehler liess warten: {dt:.2f}s")
check("hash" not in getattr(lp, "_FEYNTALK_WARM", {}), "Eintrag blieb nach Faden-Start-Fehler")

# 4) Ein geplatzter Warmlauf loggt einmal; der Request baut synchron weiter.
srv, E = start_engine(material, caps=CAPS, n_ctx=100000, renderer_delay=0.0)
reset(*srv.server_address[:2], "tokens")
_orig_build = lp._feyntalk_warm_build


def _fail_warm(name, host, port, served_hash, entry):
    lp._feyntalk_log_once(name + "|warm|" + str(served_hash),
                          f"feyntalk: {name} material warm-up failed (TestError: boom)")
    entry["evt"].set()


lp._feyntalk_warm_build = _fail_warm
getattr(lp, "_FEYNTALK_WARM", {}).clear()
del lp._testlog[:]
lp._feyntalk_warm_and_wait("eng", *srv.server_address[:2], material["hash"])
check(sum(1 for z in lp._testlog if "warm-up failed" in z) == 1,
      f"geplatzter Warmlauf nicht genau einmal geloggt: {lp._testlog[-3:]}")
lp._feyntalk_warm_build = _orig_build
cap = lp._feyntalk_capability(("eng",) + srv.server_address[:2])
check(cap.get("mode") == "tokens", f"Request baute nicht synchron nach: {cap.get('mode')}")
check(E.renderer_hits == 1, f"synchroner Bau fehlte: {E.renderer_hits}")
srv.shutdown()

# 5) Zwei Wartende auf einem Bau: ein Renderer-Abruf, beide bekommen das Material.
srv, E = start_engine(material, caps=CAPS, n_ctx=100000, renderer_delay=1.0)
reset(*srv.server_address[:2], "tokens")
ergebnis = []


def _wait():
    ergebnis.append(lp._feyntalk_warm_and_wait("eng", *srv.server_address[:2], material["hash"]))


ts = [threading.Thread(target=_wait) for _ in range(2)]
for t in ts:
    t.start()
for t in ts:
    t.join(6.0)
check(E.renderer_hits == 1, f"zwei Wartende holten das Material {E.renderer_hits}x")
check(len(ergebnis) == 2 and all(x is not None for x in ergebnis),
      f"nicht beide Wartenden bekamen das Material: {ergebnis}")
srv.shutdown()

# 6) Ein klassisches Backend ohne Alias bekommt keinen tokens-Probe.
srv, E = start_engine(material, caps=CAPS, n_ctx=100000)
reset(*srv.server_address[:2], "off")
lp.MODEL_ALIASES = {}
lp._feyntalk_warm_all()
time.sleep(0.5)
check(E.prop_hits == 0 and E.renderer_hits == 0,
      f"klassisches Backend wurde geprobt: props={E.prop_hits} renderer={E.renderer_hits}")
srv.shutdown()

# 7) Der Startpfad (reload_backends) loest genau EINEN Warmlauf aus; main ruft
#    ihn nicht zusaetzlich auf.
_src = open(lp.__file__).read().split('if __name__ == "__main__":', 1)[1]
check("_feyntalk_warm_all()" not in _src, "main ruft den Warmlauf zusaetzlich auf")
check("reload_backends(None, None)" in _src, "main laedt die Backends nicht")
_runs = []
_orig_run = lp._feyntalk_warm_run


def _count_run():
    _runs.append(lp._feyntalk_setting("eng"))


lp._feyntalk_warm_run = _count_run
_orig_load = lp._load_local_backends
lp._load_local_backends = lambda: list(lp.LOCAL_BACKENDS)
lp._build_sems({"eng": 4})
lp.reload_backends(None, None)
ende = time.monotonic() + 3.0
while time.monotonic() < ende and lp._FEYNTALK_WARM_RUN["active"]:
    time.sleep(0.02)
lp._feyntalk_warm_run = _orig_run
lp._load_local_backends = _orig_load
check(len(_runs) == 1, f"Startpfad loeste {len(_runs)} Warmlaeufe aus")

# 9) SIGHUP-Sturm: hoechstens zwei Laeufe, der letzte Stand wird warm.
_runs2 = []
_orig_run2 = lp._feyntalk_warm_run


def _slow_run():
    _runs2.append(lp._feyntalk_setting("eng"))
    time.sleep(0.2)


lp._feyntalk_warm_run = _slow_run
lp._FEYNTALK_WARM_RUN["active"] = False
lp._FEYNTALK_WARM_RUN["pending"] = False
for i in range(20):
    lp.BACKEND_FEYNTALK["eng"] = "tokens" if i % 2 == 0 else "text"
    lp._feyntalk_warm_all()
lp.BACKEND_FEYNTALK["eng"] = "text"
ende = time.monotonic() + 3.0
while time.monotonic() < ende and lp._FEYNTALK_WARM_RUN["active"]:
    time.sleep(0.02)
lp._feyntalk_warm_run = _orig_run2
check(len(_runs2) <= 2, f"SIGHUP-Sturm loeste {len(_runs2)} Laeufe aus")
check(bool(_runs2) and _runs2[-1] == "text", f"letzter Lauf sah nicht den letzten Stand: {_runs2}")


# 8) _feyntalk_warm_all blockiert nicht: Alias-Aufloesung und Probes laufen im Faden.
srv, E = start_engine(material, caps=CAPS, n_ctx=100000)
reset(*srv.server_address[:2], "tokens")
lp.MODEL_ALIASES = {"x-ft": {"model": MODEL, "feyntalk": "tokens"}}
_orig_discover = lp.discover


def _slow_discover(backend):
    time.sleep(2.0)
    return _orig_discover(backend)


lp.discover = _slow_discover
try:
    t0 = time.monotonic()
    lp._feyntalk_warm_all()
    dt = time.monotonic() - t0
finally:
    lp.discover = _orig_discover
check(dt < 0.5, f"_feyntalk_warm_all blockierte {dt:.2f}s")
ende = time.monotonic() + 6.0
while time.monotonic() < ende and not lp.FEYNTALK_MATERIALS:
    time.sleep(0.05)
check(bool(lp.FEYNTALK_MATERIALS), "Warmlauf nach Rueckkehr nicht abgeschlossen")
srv.shutdown()

# 9) Ein Faden-Start-Fehler des Laufs: kein Ausnahme-Durchschlag zum Aufrufer (SIGHUP-Pfad),
#    die Lauf-Flagge bleibt nicht haengen, der naechste Aufruf waermt wieder.
srv, E = start_engine(material, caps=CAPS, n_ctx=100000, renderer_delay=0.0)
reset(*srv.server_address[:2], "tokens")
with lp._FEYNTALK_WARM_RUN_LOCK:
    lp._FEYNTALK_WARM_RUN["active"] = False
    lp._FEYNTALK_WARM_RUN["pending"] = False
lp.threading.Thread = _BoomThread
try:
    durchschlag = None
    try:
        lp._feyntalk_warm_all()
    except Exception as e:  # noqa: BLE001
        durchschlag = e
finally:
    lp.threading.Thread = _orig_thread
check(durchschlag is None, f"Faden-Start-Fehler schlug zum Aufrufer durch: {durchschlag!r}")
check(lp._FEYNTALK_WARM_RUN["active"] is False, "Lauf-Flagge blieb nach Faden-Start-Fehler gesetzt")
lp.FEYNTALK_MATERIALS.clear()
lp._feyntalk_warm_all()
ende = time.monotonic() + 6.0
while time.monotonic() < ende and not lp.FEYNTALK_MATERIALS:
    time.sleep(0.05)
check(bool(lp.FEYNTALK_MATERIALS), "nach dem Faden-Start-Fehler waermt der naechste Aufruf nicht")
srv.shutdown()

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
