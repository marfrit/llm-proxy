#!/usr/bin/env python3
"""Client-Abbruch mitten im Strom: der Lesefaden darf nicht am Verbindungsschluss sterben.

Der Client-Wachhund kappt den Backend-Socket. Der Lesefaden bekommt dabei ein
EOF und schliesst in `read1` den fp (`_close_conn`). Laeuft gleichzeitig ein
`conn.close()` aus einem anderen Faden -- im Fehlerbild war das
der Wachhund --, treffen sich beide in `_close_conn`, der zweite liest
`self.fp = None` und faellt an `'NoneType' object has no attribute 'close'`.
Die Zeile landet als `Lesefaden abgerissen: AttributeError: ...` im Protokoll,
obwohl der Abbruch selbst funktioniert.

Geprueft wird ein echter Token-Modus-Strom mit einem echten Backend:

  a) kein `Lesefaden abgerissen`/`AttributeError` im Gateway-Protokoll,
  b) die Backend-Verbindung wird genau einmal geschlossen,
  c) der Abbruch erreicht das Backend (sein Schreibvorgang scheitert).

ROT auf dem Stand vor dem Fix: der Wachhund schliesst die Verbindung selbst,
der Lesefaden faellt an `None.close`, und die Verbindung wird zweimal
geschlossen.

Der Wettlauffenster ist im echten `_close_conn` zwei Bytecode-Zeilen breit;
fuer die Pruefung wird es geweitet (zwischen `self.fp = None` und `fp.close()`),
damit der Fall nicht von der Terminierung abhaengt. Aufruf:
  python3 tests/test_client_abort_reader_race.py [pfad-zu-llm-proxy.py]
"""
import http.client
import http.server
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402
from _feyntalk_fixture import MODEL, make_material  # noqa: E402

lp = lade()
fail = 0
checks = 0
MATERIAL = make_material()
CAPS = {"supports_preserve_thinking": True, "supports_object_arguments": False}


def check(cond, msg):
    global fail, checks
    checks += 1
    if not cond:
        print("ROT:", msg)
        fail += 1


def _sse(obj):
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()


class Backend(http.server.BaseHTTPRequestHandler):
    cancelled = threading.Event()
    protocol_version = "HTTP/1.1"        # Verbindung bleibt am HTTPResponse haengen

    def log_message(self, *a):
        pass

    def _json(self, payload, status=200):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/props":
            self._json({"token_mode": "ready", "renderer_hash": MATERIAL["hash"],
                        "build": {"version": "1.4.0"}, "arcint": {"v": 1},
                        "chat_template_caps": CAPS})
        elif self.path == "/v1/responses/renderer":
            self._json(MATERIAL)
        elif self.path == "/v1/models":
            self._json({"data": [{"id": MODEL, "object": "model"}]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        # Eine riesige Laenge haelt die Verbindung persistent (will_close=False),
        # sonst gibt getresponse() den Socket an den Response ab und der
        # Wachhund haette beim Schluss nichts mehr zu treffen.
        self.send_header("Content-Length", str(10_000_000))
        self.end_headers()
        frames = [_sse({"type": "response.created",
                        "response": {"id": "r", "created_at": 1700000000,
                                     "status": "in_progress"}})]
        frames += [_sse({"type": "arcint:token", "v": 1, "token_index": i,
                         "token_id": 1, "field": "text"}) for i in range(20)]
        try:
            for frame in frames:
                self.wfile.write(frame)
                self.wfile.flush()
                time.sleep(0.4)      # Lesefaden wartet im read1, wenn der Client auflegt
        except OSError:
            Backend.cancelled.set()


backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Backend)
backend.daemon_threads = True
threading.Thread(target=backend.serve_forever, daemon=True).start()
bp = backend.server_address[1]
proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lp.Handler)
proxy.daemon_threads = True
threading.Thread(target=proxy.serve_forever, daemon=True).start()
pp = proxy.server_address[1]

lp.LOCAL_BACKENDS[:] = [("eng", "127.0.0.1", bp)]
lp.BACKEND_FEYNTALK.clear()
lp.BACKEND_FEYNTALK["eng"] = "tokens"
lp._FEYNTALK_WARNED.clear()
with lp.FEYNTALK_CACHE_LOCK:
    lp.FEYNTALK_CACHE.clear()
    lp.FEYNTALK_MATERIALS.clear()
lp._FEYNTALK_WARM.clear()
lp._build_sems({"eng": 4})
lp._disc_cache.clear()


class CountingConn(http.client.HTTPConnection):
    """Zaehlt Schluesse der Verbindung, die einen POST trug (Discovery ist GET)."""
    lock = threading.Lock()
    closes = []

    def request(self, method, url, body=None, headers={}, **kw):
        self._methode = method
        return super().request(method, url, body=body, headers=headers, **kw)

    def getresponse(self):
        antwort = super().getresponse()
        antwort._post_conn = getattr(self, "_methode", None) == "POST"
        return antwort

    def close(self):
        if getattr(self, "_methode", None) == "POST" and self.sock is not None:
            with CountingConn.lock:
                CountingConn.closes.append(self)     # nur echte Socket-Schluesse
        return super().close()


def abort_mid_stream(body):
    s = socket.create_connection(("127.0.0.1", pp), timeout=10)
    raw = json.dumps(body).encode()
    s.sendall((f"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
               f"Content-Type: application/json\r\nContent-Length: {len(raw)}"
               f"\r\n\r\n").encode() + raw)
    s.settimeout(5)
    got = b""
    try:
        while len(got) < 200:
            chunk = s.recv(4096)
            if not chunk:
                break
            got += chunk
    except OSError:
        pass
    s.close()       # voller Schluss: der Wachhund sieht ihn und kappt das Backend


def main():
    global fail, checks
    http_client_conn = http.client.HTTPConnection
    http_client_resp_close = http.client.HTTPResponse._close_conn

    close_conn_calls = []

    def widened_close(antwort):
        """Das echte _close_conn, mit geweitetem Wettlauf zwischen None und close."""
        fp = antwort.fp
        antwort.fp = None
        if getattr(antwort, "_post_conn", False):
            close_conn_calls.append(antwort)
        if fp is not None:
            time.sleep(0.05)
        fp.close()      # fp ist None, wenn ein zweiter Faden zuerst war

    chat = {"model": MODEL, "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}
    lp._testlog.clear()
    Backend.cancelled.clear()
    CountingConn.closes.clear()
    close_conn_calls.clear()
    http.client.HTTPConnection = CountingConn
    http.client.HTTPResponse._close_conn = widened_close
    try:
        abort_mid_stream(chat)
        ende = time.monotonic() + 3.0
        while time.monotonic() < ende and not Backend.cancelled.is_set():
            time.sleep(0.02)
        time.sleep(0.2)                     # Protokollzeile des Hauptfadens abwarten
    finally:
        http.client.HTTPConnection = http_client_conn
        http.client.HTTPResponse._close_conn = http_client_resp_close

    zeilen = [z for z in lp._testlog if "Lesefaden abgerissen" in z
              or "AttributeError" in z]
    check(not zeilen, f"Protokoll traegt den Lesefaden-Fehler: {zeilen}")
    check(len(close_conn_calls) == 1,
          f"Response {len(close_conn_calls)} mal geschlossen (erwartet 1)")
    check(len(CountingConn.closes) == 1,
          f"Backend-Socket {len(CountingConn.closes)} mal geschlossen (erwartet 1)")
    check(Backend.cancelled.is_set(), "Abbruch erreichte das Backend nicht")

    if fail:
        print(f"ROT ({fail} von {checks})")
        return 1
    print(f"GRUEN: {checks} Faelle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
