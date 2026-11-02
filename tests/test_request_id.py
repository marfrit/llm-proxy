#!/usr/bin/env python3
"""X-Request-Id: Eingang, Upstream, Antwort und Logzeile eines Zuges.

Die ID wird einmal je eingehender Anfrage bestimmt: ein gueltiger Client-Wert
(1..128 Zeichen aus [A-Za-z0-9._:-]) bleibt, sonst erzeugt das Gateway eine im
selben Format (`req-` + 16 Hex). Sie reist an jeden Upstream-Versuch, steht in
jeder Antwort (Erfolg, eigener Fehler, SSE vor dem ersten Ereignis) und in der
Logzeile mit Modell, Backend, Groessen und Zeiten. Ein ungueltiger Client-Wert
darf nirgends auftauchen; ersetzt ein Backend die ID, gewinnt die Gateway-ID
und die Abweichung steht einmal im Protokoll.

ROT ohne die Client-Seite von arcint 1.4.2: kein X-Request-Id am Backend, keine
Antwortkopfzeile, kein `rid=` in der Logzeile, ungueltige Werte reisten mit.
"""
import http.client
import http.server
import json
import os
import re
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402

lp = lade()
fail = 0
checks = 0
MODEL = "rid-model"
RID_MUSTER = re.compile(r"req-[0-9a-f]{16}")


def warte_bis(fn, grenze=2.0):
    ende = time.monotonic() + grenze
    while time.monotonic() < ende:
        if fn():
            return True
        time.sleep(0.02)
    return fn()


def check(cond, msg):
    global fail, checks
    checks += 1
    if not cond:
        print("ROT:", msg)
        fail += 1


def _json(payload, status=200):
    return json.dumps(payload).encode()


class Backend(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []              # X-Request-Id jedes POST
    stream = False
    hard_close = False     # Verbindung ohne Antwort abreissen (Failover)
    replace_rid = None     # Antwortkopfzeile mit diesem Wert

    def log_message(self, *a):
        pass

    def _sende(self, body, ct="application/json", status=200, rid=None):
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        if rid is not None:
            self.send_header("X-Request-Id", rid)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        if self.path.split("?", 1)[0].rstrip("/") == "/v1/models":
            self._sende(_json({"data": [{"id": MODEL, "object": "model"}]}))
        else:
            self._sende(_json({"error": "not found"}, 404))

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        rid = self.headers.get("X-Request-Id")
        type(self).seen.append(rid)
        if self.hard_close:
            self.close_connection = True
            self.connection.close()
            return
        antwort_rid = self.replace_rid
        if self.stream:
            frames = [json.dumps({"id": "c", "object": "chat.completion.chunk",
                                  "model": MODEL,
                                  "choices": [{"index": 0,
                                               "delta": {"content": "x"},
                                               "finish_reason": None}]})]
            body = b"".join(b"data: " + f.encode() + b"\n\n" for f in frames)
            body += b"data: [DONE]\n\n"
            self._sende(body, ct="text/event-stream", rid=antwort_rid)
        else:
            body = _json({"id": "c", "object": "chat.completion", "model": MODEL,
                          "choices": [{"index": 0,
                                       "message": {"role": "assistant",
                                                   "content": "ok"},
                                       "finish_reason": "stop"}]})
            self._sende(body, rid=antwort_rid)


def start_backend(**einst):
    klasse = type("B", (Backend,), dict(einst, seen=[]))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), klasse)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1], klasse.seen


good_srv, good_port, good_seen = start_backend()
bad_srv, bad_port, bad_seen = start_backend(hard_close=True)

proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lp.Handler)
proxy.daemon_threads = True
threading.Thread(target=proxy.serve_forever, daemon=True).start()
pp = proxy.server_address[1]


def backends(*eintraege):
    lp.LOCAL_BACKENDS[:] = list(eintraege)
    lp.BACKEND_FEYNTALK.clear()
    lp._disc_cache.clear()
    lp._testlog.clear()
    good_seen.clear()
    bad_seen.clear()


def post(body, rid=None, stream=None):
    body = dict(body)
    if stream is not None:
        body["stream"] = stream
    raw = json.dumps(body).encode()
    conn = http.client.HTTPConnection("127.0.0.1", pp, timeout=10)
    headers = {"Content-Type": "application/json"}
    if rid is not None:
        headers["X-Request-Id"] = rid
    conn.request("POST", "/v1/chat/completions", body=raw, headers=headers)
    resp = conn.getresponse()
    daten = resp.read()
    kopf = dict(resp.getheaders())
    status = resp.status
    conn.close()
    return status, kopf, daten


def raw_post(rid_bytes, body, stream=False):
    """Roher Client: der Kopfzeilenwert geht byteweise raus, ohne Client-Pruefung."""
    j = dict(body)
    j["stream"] = stream
    raw = json.dumps(j).encode()
    s = socket.create_connection(("127.0.0.1", pp), timeout=10)
    s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
              b"Content-Type: application/json\r\nX-Request-Id: " + rid_bytes +
              b"\r\nContent-Length: " + str(len(raw)).encode() + b"\r\n\r\n" + raw)
    s.settimeout(10)
    buf = b""
    try:
        while b"\r\n\r\n" not in buf:
            stueck = s.recv(4096)
            if not stueck:
                break
            buf += stueck
    except OSError:
        pass
    s.close()
    kopf = {}
    for zeile in buf.split(b"\r\n"):
        if b":" in zeile:
            k, v = zeile.split(b":", 1)
            kopf[k.decode("latin-1").strip().lower()] = v.strip().decode("latin-1")
    return kopf


CHAT = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}

# 1. Gueltige Client-ID: an das Backend, zurueck, in der Logzeile.
backends(("good", "127.0.0.1", good_port))
wert = "client.Valid-Id_1:abc"
status, kopf, _ = post(CHAT, rid=wert)
check(status == 200, f"gueltige ID: Status {status}")
check(good_seen == [wert], f"Backend sah {good_seen!r}, erwartet {wert!r}")
check(kopf.get("X-Request-Id") == wert,
      f"Antwort trug X-Request-Id {kopf.get('X-Request-Id')!r}")
warte_bis(lambda: any(f"rid={wert}" in z for z in lp._testlog))
check(any(f"rid={wert}" in z for z in lp._testlog),
      f"Logzeile traegt rid= nicht; logs={lp._testlog!r}")

# 2. Ohne Client-ID: erzeugt im arcint-Format, ueberall dieselbe.
status, kopf, _ = post(CHAT)
neu = kopf.get("X-Request-Id")
check(RID_MUSTER.fullmatch(neu or ""),
      f"erzeugte ID {neu!r} passt nicht auf req-[0-9a-f]{{16}}")
check(good_seen[-1] == neu, f"Backend sah {good_seen[-1]!r}, Antwort {neu!r}")
warte_bis(lambda: any(f"rid={neu}" in z for z in lp._testlog))
check(any(f"rid={neu}" in z for z in lp._testlog),
      f"Logzeile traegt die erzeugte rid= nicht; neu={neu} logs={lp._testlog[-5:]}")

# 3. Ungueltige Werte: ersetzt, nirgends beim Backend oder im Log.
ungueltig = [b"a b", b"x" * 129, "\u00e4".encode()]
for roh in ungueltig:
    backends(("good", "127.0.0.1", good_port))
    kopf = raw_post(roh, CHAT)
    neu = kopf.get("x-request-id")
    check(RID_MUSTER.fullmatch(neu or ""),
          f"ungueltig {roh!r}: Antwort-ID {neu!r} nicht erzeugt")
    check(good_seen and good_seen[-1] == neu,
          f"ungueltig {roh!r}: Backend sah {good_seen[-1]!r}")
    text = roh.decode("latin-1")
    check(all(text not in z for z in lp._testlog),
          f"ungueltig {roh!r}: Wert steht im Protokoll")

# 4. Backend ersetzt die ID: Gateway-ID gewinnt, eine Debugzeile.
backends(("good", "127.0.0.1", good_port))
Backend.replace_rid = "backend.own-id"
wert = "gateway-wins.1"
status, kopf, _ = post(CHAT, rid=wert)
backend_wert = "backend.own-id"
check(kopf.get("X-Request-Id") == wert,
      f"Backend-ID gewann: Antwort {kopf.get('X-Request-Id')!r}")
warte_bis(lambda: any("backend replaced the request id" in z for z in lp._testlog))
treffer = [z for z in lp._testlog if "backend replaced the request id" in z]
check(len(treffer) == 1, f"Ersetzungszeile {len(treffer)} mal statt einmal")
check(treffer and f"rid={wert}" in treffer[0] and f"backend_rid={backend_wert}" in treffer[0],
      f"Ersetzungszeile nennt die IDs nicht: {treffer!r}")
eigene = re.findall(r"\brid=[A-Za-z0-9._:-]+", treffer[0]) if treffer else []
check(len(eigene) == 1, f"Ersetzungszeile traegt rid= {len(eigene)} mal: {treffer!r}")
Backend.replace_rid = None

# 5. Failover: dieselbe ID an beide Backends, Antwort traegt sie.
backends(("bad", "127.0.0.1", bad_port), ("good", "127.0.0.1", good_port))
wert = "failover-id.1"
status, kopf, _ = post(CHAT, rid=wert)
check(status == 200, f"Failover: Status {status}")
check(bad_seen == [wert] and good_seen == [wert],
      f"Failover sah bad={bad_seen!r} good={good_seen!r}, erwartet {wert!r} zweimal")
check(kopf.get("X-Request-Id") == wert,
      f"Failover-Antwort trug {kopf.get('X-Request-Id')!r}")

# 6. SSE: Kopfzeile ist da, bevor der erste Rahmen kommt.
backends(("good", "127.0.0.1", good_port))
Backend.stream = True
wert = "sse-id.2"
status, kopf, daten = post(CHAT, rid=wert, stream=True)
check(status == 200 and b"data: [DONE]" in daten, "SSE-Antwort unvollstaendig")
check(kopf.get("X-Request-Id") == wert, f"SSE-Kopfzeile {kopf.get('X-Request-Id')!r}")
check(good_seen == [wert], f"SSE-Backend sah {good_seen!r}")
Backend.stream = False

# 7. Eigener Fehler ohne Backend: Kopfzeile trotzdem da.
backends()
status, kopf, _ = post({"model": "nirgends-vorhanden",
                        "messages": [{"role": "user", "content": "hi"}]})
check(status in (404, 502), f"Fehlerantwort Status {status}")
neu = kopf.get("X-Request-Id")
check(RID_MUSTER.fullmatch(neu or ""), f"Fehlerantwort-ID {neu!r} nicht erzeugt")
warte_bis(lambda: any(f"rid={neu}" in z for z in lp._testlog))
check(any(f"rid={neu}" in z for z in lp._testlog),
      "Fehler-Logzeile traegt rid= nicht")

# 8. Grammatik selbst: Grenzen und ungueltige Zeichen.
check(lp._anfrage_rid("a") == "a", "1 Zeichen wurde nicht durchgelassen")
check(lp._anfrage_rid("A" * 128) == "A" * 128, "128 Zeichen wurden nicht durchgelassen")
for roh in ("", "A" * 129, "a b", "a\nb", "a\rb", "\u00e4", "a/b"):
    neu = lp._anfrage_rid(roh)
    check(RID_MUSTER.fullmatch(neu), f"{roh!r} wurde nicht ersetzt ({neu!r})")

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
