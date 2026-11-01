#!/usr/bin/env python3
"""Zwei Zeitueberschreitungen, zwei Bedeutungen.

Ein Lese-Timeout des Backends ist ein Backend-Ausfall, kein haengender Anrufer.
Es wird als Backend-Timeout protokolliert, zaehlt als Fehlschlag, und solange noch
nichts beim Anrufer angekommen ist, geht die Kette weiter. Die kurze Failover-Frist
gilt fuer Verbindung und erstes Byte -- nicht fuer den ganzen Strom: ein langsamer,
aber stetiger Strom auf einem Nicht-letzten Backend wird nicht gekappt.

ROT gegen den Stand vor dem Fix: der Timeout landet als CLIENT-STALLED im Log und
gilt als Erfolg; der Strom wird nach 3 s abgeschnitten (bzw. bei kleiner Frist
gleich am ersten Byte).
"""
import io
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402

lp = lade()
fail = 0


def check(cond, msg):
    global fail
    if not cond:
        print("ROT:", msg)
        fail += 1


def rahmen(text):
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": text}}]}) + "\n\n").encode()


class Backend(BaseHTTPRequestHandler):
    """mode: 'stall' sendet Kopfzeilen und dann nichts mehr; 'slow' sendet ein
    erstes Byte, wartet laenger als die Failover-Frist und schliesst den Strom ab;
    'ok' antwortet sofort."""
    mode = "ok"
    pause = 0.0
    hits = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.hits is not None:
            self.hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()
        if self.mode == "stall":
            time.sleep(self.pause)
            try:
                self.wfile.write(rahmen("STALL") + b"data: [DONE]\n\n")
            except OSError:
                pass
        elif self.mode == "slow":
            self.wfile.write(rahmen("ERST"))
            self.wfile.flush()
            time.sleep(self.pause)
            self.wfile.write(rahmen("FOLGE") + b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self.wfile.write(rahmen("ok") + b"data: [DONE]\n\n")
        self.close_connection = True


def start(mode, pause=0.0):
    hits = []
    klasse = type("B_" + mode, (Backend,), {"mode": mode, "pause": pause, "hits": hits})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), klasse)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, hits


class H(lp.Handler):
    def __init__(self, wfile, conn):
        self.wfile, self.connection = wfile, conn
        self.headers = {"get": lambda k, d=None: d}

    def send_response(self, *a, **k):
        pass

    def send_header(self, *a, **k):
        pass

    def end_headers(self):
        pass


def lauf(backends, is_streaming=True):
    a, b = socket.socketpair()
    h = H(io.BytesIO(), a)
    try:
        h._proxy_local("POST", "/v1/chat/completions", b"", "", 0.0,
                       model=None, is_streaming=is_streaming)
        return h.wfile.getvalue()
    finally:
        a.close()
        b.close()


# 1) Backend sendet Kopfzeilen, dann Stocken > kurze Frist -> Ausfall, naechstes Backend
lp.CONNECT_TIMEOUT = 0.3
lp.READ_TIMEOUT = 10
stall, stall_hits = start("stall", pause=0.8)
ok, ok_hits = start("ok")
lp.LOCAL_BACKENDS[:] = [("stall", "127.0.0.1", stall.server_address[1]),
                        ("ok", "127.0.0.1", ok.server_address[1])]
raus = lauf(lp.LOCAL_BACKENDS)
check(stall_hits and ok_hits, f"Kette lief nicht weiter: stall={stall_hits} ok={ok_hits}")
check(b'"ok"' in raus and b"STALL" not in raus,
      f"Anrufer bekam nicht die Antwort des zweiten Backends: {raus[:120]!r}")
zeit = [z for z in lp._testlog if "stall" in z]
check(any("BACKEND-TIMEOUT" in z for z in zeit), f"kein Backend-Timeout im Log: {zeit}")
check(not any("CLIENT-STALLED" in z for z in zeit),
      f"Backend-Timeout wurde als CLIENT-STALLED gemeldet: {zeit}")
stall.shutdown()
ok.shutdown()

# 2) langsamer, stetiger Strom > 3 s auf einem Nicht-letzten Backend bleibt ganz
lp.CONNECT_TIMEOUT = 3
lp.READ_TIMEOUT = 10
langsam, lang_hits = start("slow", pause=3.5)
ok2, ok2_hits = start("ok")
lp.LOCAL_BACKENDS[:] = [("langsam", "127.0.0.1", langsam.server_address[1]),
                        ("ok2", "127.0.0.1", ok2.server_address[1])]
t0 = time.monotonic()
raus = lauf(lp.LOCAL_BACKENDS)
dauer = time.monotonic() - t0
check(b"ERST" in raus and b"FOLGE" in raus,
      f"langsamer Strom wurde gekappt (nach {dauer:.1f}s): {raus[:160]!r}")
check(not ok2_hits, f"langsamer Strom fiel faelschlich auf das zweite Backend: {ok2_hits}")
langsam.shutdown()
ok2.shutdown()

print("GRUEN" if not fail else f"ROT ({fail})")
sys.exit(1 if fail else 0)
