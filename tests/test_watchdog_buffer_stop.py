#!/usr/bin/env python3
"""Wachhund bei vollem Puffer: der Lesefaden muss vor dem Beitritt geweckt werden.

Legt der Anrufer auf, waehrend der Lesefaden in `_UpstreamBuffer.put` auf Platz
wartet, weckt der Socket-shutdown ihn nicht -- nur `buf.stop()`. Ohne den
stop-Aufruf wartet der Wachhund den ganzen Beitritts-Timeout ab, bevor er die
Backend-Verbindung schliesst.

ROT auf bb7c71f: der Schluss kommt erst nach CONNECT_TIMEOUT (3 s), nicht
deutlich darunter.
"""
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


ca, cb = socket.socketpair()
ba, bb = socket.socketpair()
h = H(ca.makefile("wb"), ca)
h._client_lock = threading.Lock()
h._client_wrote = threading.Event()
h._client_wrote.set()
h._client_streaming = True
h._client_at_frame_end = True
h._backend_close_lock = threading.Lock()

buf = lp._UpstreamBuffer(4)
geparkt = threading.Event()


def leser():
    geparkt.set()
    buf.put(b"0123456789", lambda: None)   # passt nie: wartet auf Platz oder stop


leser_faden = threading.Thread(target=leser, daemon=True)
leser_faden.start()
geparkt.wait(2.0)
time.sleep(0.1)                     # sicher im put-Warten

h._backend_buf = buf
h._backend_reader = leser_faden
conn = FakeConn(ba)
gone, stop = threading.Event(), threading.Event()
h._client_watchdog(lambda: conn, gone, stop)
time.sleep(0.2)

t0 = time.monotonic()
cb.close()
ende = t0 + 3.5
while time.monotonic() < ende and not conn.closed:
    time.sleep(0.01)
dt = time.monotonic() - t0
check(conn.closed, "Backend-Verbindung blieb offen")
check(dt < 1.5, f"Schluss erst nach {dt:.2f}s (erwartet deutlich unter dem Timeout)")
check(gone.is_set(), "gone wurde nicht gesetzt")

stop.set()
try:
    h.wfile.close()
except OSError:
    pass
for s in (ca, cb, ba, bb):
    try:
        s.close()
    except OSError:
        pass

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
