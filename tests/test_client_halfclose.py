#!/usr/bin/env python3
"""Halber Schluss: der Anrufer schickt seine Anfrage und schliesst die SCHREIBSEITE.

Ein Agent, der einen Strom anfordert und danach nur noch liest, macht genau das:
er sendet den Rumpf, macht `shutdown(SHUT_WR)` und liest weiter. Der Client ist
damit nicht weg -- er liest weiter. Frueher hielt der Client-Wachhund das FIN
fuer ein Auflegen, setzte `gone_evt` und kappte die Backend-Verbindung mitten im
Strom: der Anrufer bekam einen abgeschnittenen Strom.

Geprueft wird der Text-Relaisweg mit einem echten Fake-Backend, 25 SSE-Rahmen
und einem rohen Socket-Anrufer:

  a) Halbschluss-Anrufer bekommt alle 25 Rahmen.
  b) Gegenprobe: ein Anrufer, der offen laesst, bekommt ebenfalls alle 25.

Aufruf:  python3 tests/test_client_halfclose.py [pfad-zu-llm-proxy.py]
"""

import http.server
import json
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


N_FRAMES = 25
RAHMEN = (b'data: {"id":"c","object":"chat.completion.chunk","model":"m",'
          b'"choices":[{"index":0,"delta":{"content":"x"},"finish_reason":null}]}\n\n')

FERTIG = []          # Backend-Laeufe, die alle 25 Rahmen geschrieben haben


class FakeBackend(http.server.BaseHTTPRequestHandler):
    """/v1/models nennt das Modell, POST /v1/chat/completions stroemt 25 Rahmen."""

    fertig = False    # letzter Lauf vollstaendig durchgeschrieben?

    def log_message(self, *a):
        pass

    def _sende(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?", 1)[0].rstrip("/") == "/v1/models":
            self._sende(json.dumps({"data": [{"id": "fake-local", "object": "model"}]}).encode())
        else:
            self._sende(b"{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        geschrieben = 0
        for _ in range(N_FRAMES):
            try:
                self.wfile.write(RAHMEN)
                self.wfile.flush()
            except OSError:
                return
            geschrieben += 1
            time.sleep(0.05)
        FakeBackend.fertig = True
        FERTIG.append(geschrieben)


backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeBackend)
backend.daemon_threads = True
threading.Thread(target=backend.serve_forever, daemon=True).start()
bport = backend.server_address[1]

lp.LOCAL_BACKENDS[:] = [("fake", "127.0.0.1", bport)]
lp.BACKEND_FEYNTALK.clear()          # Textweg, kein Tokenweg

proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lp.Handler)
proxy.daemon_threads = True
threading.Thread(target=proxy.serve_forever, daemon=True).start()
pport = proxy.server_address[1]


def client(halbschluss=True):
    """Roher Socket-Anrufer: Request senden, optional die Schreibseite schliessen, bis EOF lesen."""
    body = json.dumps({"model": "fake-local", "stream": True,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    s = socket.create_connection(("127.0.0.1", pport), timeout=10)
    s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\n"
              b"Host: 127.0.0.1\r\n"
              b"Content-Type: application/json\r\n"
              b"Content-Length: " + str(len(body)).encode() + b"\r\n"
              b"\r\n" + body)
    s.settimeout(10)
    daten = b""
    try:
        if halbschluss:
            # Erst wenn die Antwort sichtbar begonnen hat, die Schreibseite
            # schliessen; danach liest der Client weiter.
            while b"data: " not in daten:
                daten += s.recv(65536)
            s.shutdown(socket.SHUT_WR)
        while True:
            stueck = s.recv(65536)
            if not stueck:
                break
            daten += stueck
    except (socket.timeout, OSError):
        pass
    s.close()
    return daten


rahmen_halb = client(halbschluss=True).count(b"data: ")
check(rahmen_halb == N_FRAMES,
      f"Halbschluss-Anrufer bekam {rahmen_halb} von {N_FRAMES} Rahmen")

rahmen_offen = client(halbschluss=False).count(b"data: ")
check(rahmen_offen == N_FRAMES,
      f"Anrufer mit offener Schreibseite bekam {rahmen_offen} von {N_FRAMES} Rahmen")

# c) Voller Schluss: der naechste Schreibvorgang scheitert -> CLIENT-CANCELLED,
#    kein Backend-FAIL und kein routed-FAIL (sonst ein sinnloser Retry).
del lp._testlog[:]
koerper = json.dumps({"model": "fake-local", "stream": True,
                     "messages": [{"role": "user", "content": "hi"}]}).encode()
s = socket.create_connection(("127.0.0.1", pport), timeout=10)
s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
          b"Content-Type: application/json\r\nContent-Length: " + str(len(koerper)).encode()
          + b"\r\n\r\n" + koerper)
s.settimeout(10)
gesehen = b""
while gesehen.count(b"data: ") < 2:
    gesehen += s.recv(65536)
s.close()                                   # voller Schluss, nichts mehr lesen
zeit = time.monotonic()
while time.monotonic() - zeit < 3.0 and not any("CLIENT-CANCELLED" in z for z in lp._testlog):
    time.sleep(0.05)
check(any("CLIENT-CANCELLED" in z for z in lp._testlog),
      f"voller Schluss wurde nicht als CLIENT-CANCELLED gebucht: {lp._testlog[-3:]}")
check(not any("routed-FAIL" in z for z in lp._testlog),
      f"voller Schluss loeste routed-FAIL aus: {lp._testlog[-3:]}")

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
