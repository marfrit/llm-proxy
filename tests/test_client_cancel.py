#!/usr/bin/env python3
"""Stream hardening, Runde 5: Abbruch-Latenz des Client-Wachhunds.

Der Wachhund darf einen halben Schluss (FIN, Client liest weiter) nicht als
Auflegen werten, aber einen vollen Schluss muss er sehen:

  a) Nicht-Strom: vor dem ersten Schreibvorgang ist ein FIN ein Auflegen
     (die alte Frist gilt wieder).
  b) Strom vor dem ersten Byte (kalte Vorfuellung): dito.
  c) Strom nach dem ersten Byte: FIN allein ist kein Auflegen; der Wachhund
     probt mit einem SSE-Kommentar und sieht einen spaeteren vollen Schluss.
  d) Ein Schreibfehler beim Senden der Kopfzeilen ist ClientGone
     (CLIENT-CANCELLED), kein nackter OSError (Backend-FAIL).

ROT auf 2313989: a) und b) werden nicht mehr gekappt, c) sieht den spaeteren
Schluss nicht, d) meldet OSError statt ClientGone.
"""
import io
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402

lp = lade()
fail = 0
checks = 0


def check(cond, msg):
    global fail, checks
    checks += 1
    if not cond:
        print("ROT:", msg)
        fail += 1


class H(lp.Handler):
    def __init__(self, wfile, conn):
        self.wfile, self.connection = wfile, conn

    def send_response(self, *a, **k):
        pass

    def send_header(self, *a, **k):
        pass

    def end_headers(self):
        pass


class FakeConn:
    def __init__(self, sock):
        self.sock = sock
        self.closed = False

    def close(self):
        self.closed = True
        self.sock.close()


def wachhund(streaming, wrote_gesetzt):
    ca, cb = socket.socketpair()
    ba, bb = socket.socketpair()
    h = H(ca.makefile("wb"), ca)
    h._client_lock = threading.Lock()
    h._client_wrote = threading.Event()
    if wrote_gesetzt:
        h._client_wrote.set()
    h._client_streaming = streaming
    h._client_at_frame_end = True   # eine Rahmengrenze ist erreicht
    gone = threading.Event()
    stop = threading.Event()
    conn = FakeConn(ba)
    h._client_watchdog(lambda: conn, gone, stop)
    return h, cb, conn, gone, stop, (ca, ba, bb)


def warte_bis(fn, grenze):
    ende = time.monotonic() + grenze
    while time.monotonic() < ende:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


def schliesse(socken):
    for s in socken:
        try:
            s.close()
        except OSError:
            pass


# a) Nicht-Strom: FIN kappt den Upstream innerhalb 1,5 s (alte Frist).
h, cb, conn, gone, stop, socken = wachhund(streaming=False, wrote_gesetzt=True)
time.sleep(0.2)
cb.close()
check(warte_bis(lambda: conn.closed, 1.5),
      "Nicht-Strom: FIN kappte den Upstream nicht innerhalb 1,5 s")
stop.set()
try:
    h.wfile.close()
except OSError:
    pass
schliesse(socken)

# b) Strom, noch kein Byte geschrieben (kalte Vorfuellung): dito.
h, cb, conn, gone, stop, socken = wachhund(streaming=True, wrote_gesetzt=False)
time.sleep(0.2)
cb.close()
check(warte_bis(lambda: conn.closed, 1.5),
      "Strom ohne erstes Byte: FIN kappte den Upstream nicht innerhalb 1,5 s")
stop.set()
try:
    h.wfile.close()
except OSError:
    pass
schliesse(socken)

# c) Strom nach dem ersten Byte: FIN allein kappt nicht; ein spaeterer voller
#    Schluss wird innerhalb von 3 s gesehen.
h, cb, conn, gone, stop, socken = wachhund(streaming=True, wrote_gesetzt=True)
time.sleep(0.2)
cb.shutdown(socket.SHUT_WR)
time.sleep(0.8)
check(not conn.closed and not gone.is_set(),
      "Strom nach FIN wurde faelschlich gekappt (halber Schluss)")
cb.close()
check(warte_bis(lambda: conn.closed, 3.0),
      "voller Schluss nach halbem wurde nicht innerhalb 3 s gesehen")
stop.set()
try:
    h.wfile.close()
except OSError:
    pass
schliesse(socken)

# d) Kopfzeilen-Schreibfehler ist ClientGone, kein nackter OSError.
ca, cb = socket.socketpair()
h = H(ca.makefile("wb"), ca)
h._headers_buffer = [b"HTTP/1.1 200 OK\r\n\r\n"]
cb.close()
try:
    h.flush_headers()
    check(False, "Kopfzeilen-Schreibfehler warf nicht")
except lp.ClientGone:
    check(True, "")
except BaseException as e:  # noqa: BLE001 - genau das ist die Frage
    check(False, f"Kopfzeilen-Schreibfehler ergab {type(e).__name__}: {e}")
try:
    h.wfile.close()
except OSError:
    pass
schliesse((ca, cb))

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
