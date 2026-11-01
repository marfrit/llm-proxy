#!/usr/bin/env python3
"""Stream hardening, Runde 6: die SSE-Probe darf nie in einem Rahmen landen.

Die Relais-Wege schreiben rohe read1-Stuecke mit beliebigen TCP-Grenzen. Der
Wachhund darf sein Kommentar nur an einer Rahmengrenze anhaengen. Ein
gestreamter Auftrag, der als JSON beantwortet wird, darf gar keine Kommentare
bekommen.
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

    def log_message(self, *a):
        pass

    def send_response(self, code, message=None):
        self._headers_buffer = [b"HTTP/1.1 200 OK\r\n"]

    def send_header(self, k, v):
        self._headers_buffer.append((k + ": " + v + "\r\n").encode())

    def end_headers(self):
        self._headers_buffer.append(b"\r\n")
        self.flush_headers()


def lauf_stream(resp, is_streaming=True):
    ca, cb = socket.socketpair()
    h = H(ca.makefile("wb"), ca)
    h._client_lock = threading.Lock()
    h._client_wrote = threading.Event()
    h._client_at_frame_end = False
    h._client_streaming = is_streaming
    gone, stop = threading.Event(), threading.Event()

    class Conn:
        sock = None

        def close(self):
            pass

    h._client_watchdog(lambda: Conn(), gone, stop)
    ergebnis = {}

    def fahre():
        try:
            h._stream_response(resp, "eng", 0.0, 0.0, "", "POST",
                               "/v1/chat/completions", is_streaming=is_streaming)
            ergebnis["ok"] = True
        except BaseException as e:  # noqa: BLE001
            ergebnis["exc"] = e

    t = threading.Thread(target=fahre)
    t.start()
    cb.settimeout(6)
    received = b""
    while b"\r\n\r\n" not in received:
        received += cb.recv(4096)
    cb.shutdown(socket.SHUT_WR)
    # Zeitbudget statt Socket-Timeout: die Probe des Wachhunds kann den
    # Timeout sonst immer wieder zuruecksetzen.
    ende = time.monotonic() + 3.0
    while time.monotonic() < ende:
        try:
            stueck = cb.recv(4096)
        except (socket.timeout, OSError):
            break
        if not stueck:
            break
        received += stueck
    t.join(4)
    stop.set()
    try:
        h.wfile.close()
    except OSError:
        pass
    for s in (ca, cb):
        try:
            s.close()
        except OSError:
            pass
    return received, ergebnis


class SplitResp:
    status = 200

    def __init__(self):
        self.i = 0

    def getheader(self, name, default=""):
        return "text/event-stream" if name == "Content-Type" else default

    def read1(self, n):
        self.i += 1
        if self.i == 1:
            return b'data: AAA'
        if self.i == 2:
            time.sleep(0.8)
            return b'BBB\n\n'
        if self.i == 3:
            return b'data: CCC\n\n'
        if self.i == 4:
            return b'data:'
        return b""


class JsonResp:
    status = 200

    def __init__(self):
        self.i = 0

    def getheader(self, name, default=""):
        return "application/json" if name == "Content-Type" else default

    def read1(self, n):
        self.i += 1
        if self.i == 1:
            return b'{"error":"boom"}'
        if self.i == 2:
            time.sleep(0.8)
            return b'{"more":1}'
        return b""

# 1) Rahmen ueber read1-Grenzen zerrissen: jede Probe auf einer Grenze.
received, ergebnis = lauf_stream(SplitResp())
koerper = received.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in received else received
schlecht = []
pos = koerper.find(b":\n\n")
while pos >= 0:
    if pos != 0 and koerper[pos - 2:pos] != b"\n\n":
        schlecht.append(pos)
    pos = koerper.find(b":\n\n", pos + 1)
check(not schlecht, f"SSE-Kommentar mitten im Rahmen an {schlecht}: {koerper!r}")
check(ergebnis.get("ok") is True, "Handler kehrte nicht zurueck")
check(koerper.replace(b":\n\n", b"") ==
      b"data: AAABBB\n\n"
      b"data: CCC\n\n"
      b"data:",
      f"Rahmen verstuemmelt: {koerper!r}")

# 2) Gestreamter Auftrag, als JSON beantwortet: keine Kommentare im JSON.
received, ergebnis = lauf_stream(JsonResp(), is_streaming=True)
koerper = received.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in received else received
check(b":\n\n" not in koerper, f"Kommentar im JSON: {koerper!r}")

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
