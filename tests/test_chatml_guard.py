#!/usr/bin/env python3
"""ChatML-Marker: im Antworttext enden sie den Zug, im Denken nicht (1.0.6).

Gemessen 2026-10-18: der Stop-String "<|im_start|>" beendete eine pi-Sitzung zweimal still mitten
im Gedanken, als das Modell ueber das Qwen-Chat-Template nachdachte. Fuer Stroeme prueft jetzt
ChatMLGuard nur den content.

ROT, wenn
  - ein Marker im content (auch ueber Deltas zerrissen) nicht an dieser Stelle mit finish "stop"
    und [DONE] endet, Text davor verloren geht oder danach noch etwas durchkommt;
  - ein Marker im reasoning oder in tool_calls-Argumenten den Zug beendet oder veraendert;
  - ein angefangenes "<|i", das kein Marker wird, verschwindet;
  - ein Strom ohne Marker nicht byte-gleich bleibt;
  - _stream_response nach dem Schnitt weiter vom Backend liest;
  - eine Strom-Anfrage noch stop-Strings ans Backend schickt, oder eine Nicht-Strom-Anfrage keine
    mehr (durchgehend: Proxy-Server + Fake-Backend).
Gegenprobe: der Strom mit Marker im content MUSS geschnitten werden, sonst bestuende ein
Waechter, der nie etwas tut, alle "bleibt unveraendert"-Faelle.

Aufruf:  python3 tests/test_chatml_guard.py [pfad-zu-llm-proxy.py]
"""
import http.server
import io
import json
import os
import socket
import sys
import threading
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402

lp = lade()
fail = 0


def check(cond, msg):
    global fail
    if not cond:
        print("ROT:", msg)
        fail += 1


def frame(delta, finish=None):
    return ("data: " + json.dumps({"id": "c", "object": "chat.completion.chunk", "model": "ds",
                                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
            + "\n\n").encode()


def run(raw, size):
    g = lp.ChatMLGuard()
    out = b""
    for i in range(0, len(raw), size):
        out += g.feed(raw[i:i + size])
    return out + g.flush(), g


def seq(out):
    s = []
    for ev in out.decode().split("\n\n"):
        for l in ev.split("\n"):
            if l == "data: [DONE]":
                s.append(("D", ""))
            elif l.startswith("data: {"):
                ch = json.loads(l[6:])["choices"][0]
                d = ch.get("delta") or {}
                for k in ("reasoning", "content"):
                    if d.get(k):
                        s.append((k[0], d[k]))
                for t in d.get("tool_calls") or []:
                    s.append(("t", t["function"]["arguments"]))
                if ch.get("finish_reason"):
                    s.append(("f", ch["finish_reason"]))
    return s


DONE = b"data: [DONE]\n\n"

# 1. Marker im content, zerrissen: Schnitt genau dort (Gegenprobe)
cut = (frame({"role": "assistant", "content": ""}) + frame({"content": "Fertig."})
       + frame({"content": "<|im"}) + frame({"content": "_end|><|im_start|>user\nweiter"})
       + frame({"content": " mehr Text"}) + frame({}, "stop") + DONE)
for size in (1, 7, 100000):
    out, g = run(cut, size)
    s = seq(out)
    text = "".join(t for k, t in s if k == "c")
    check(text == "Fertig." and s[-2:] == [("f", "stop"), ("D", "")] and g.cut,
          f"Marker im content [{size}B]: {s}")
    check(out.count(b"[DONE]") == 1, f"Marker im content [{size}B]: [DONE] {out.count(b'[DONE]')}x")

# 2. Marker im reasoning: unveraendert, byte-gleich
think = (frame({"role": "assistant", "content": ""})
         + frame({"reasoning": "Das Template nutzt <|im_start|>user und <|im_end|> am Ende."})
         + frame({"content": "Antwort."}) + frame({}, "stop") + DONE)
for size in (1, 7, 100000):
    out, g = run(think, size)
    check(out == think and not g.cut, f"Marker im reasoning [{size}B]: veraendert/geschnitten")

# 3. Marker in tool_calls-Argumenten: unveraendert
tc = (frame({"role": "assistant", "content": ""})
      + frame({"tool_calls": [{"index": 0, "id": "x", "type": "function",
                               "function": {"name": "bash", "arguments": "{\"command\": \"printf '<|im_start|>'\"}"}}]})
      + frame({}, "tool_calls") + DONE)
for size in (1, 7, 100000):
    out, g = run(tc, size)
    check(out == tc and not g.cut, f"Marker in tool_calls [{size}B]: veraendert")

# 4. angefangenes "<|i", das keiner wird
near = frame({"content": "a <|i"}) + frame({"content": "nfo|> b"}) + frame({}, "stop") + DONE
for size in (1, 7, 100000):
    out, g = run(near, size)
    s = seq(out)
    check("".join(t for k, t in s if k == "c") == "a <|info|> b" and ("f", "stop") in s and not g.cut,
          f"Beinahe-Marker [{size}B]: {s}")

# 4b. Robustheit: content kein String, delta kein dict - kein Absturz, unveraendert
odd = (b'data: {"choices":[{"index":0,"delta":{"content":["x"]},"finish_reason":null}]}\n\n'
       b'data: {"choices":[{"index":0,"delta":"x","finish_reason":null}]}\n\n' + frame({}, "stop") + DONE)
try:
    out, g = run(odd, 9)
    check(out == odd, "seltsame Frames veraendert")
except Exception as e:
    check(False, f"seltsame Frames: {type(e).__name__}: {e}")

# 4c. tool_calls im selben Frame wie ein Marker am content-Anfang gehen nicht verloren
both = frame({"content": "<|im_end|>", "tool_calls": [{"index": 0, "id": "x", "type": "function",
                                                     "function": {"name": "bash", "arguments": "{}"}}]}) + DONE
out, g = run(both, 100000)
check(g.cut and ("t", "{}") in seq(out), f"tool_calls mit Marker verloren: {seq(out)}")

# 4d. Zurueckgehaltenes "<" geht vor dem naechsten reasoning-Delta raus (Reihenfolge)
order = frame({"content": "a <"}) + frame({"reasoning": "r"}) + frame({"content": "b"}) + frame({}, "stop") + DONE
out, g = run(order, 100000)
check([k for k, _ in seq(out)][:3] == ["c", "c", "r"] or [t for k, t in seq(out) if k == "c"][:2] == ["a ", "<"],
      f"Reihenfolge mit Zurueckgehaltenem: {seq(out)}")

# 5. ohne Marker byte-gleich
plain = frame({"role": "assistant", "content": ""}) + frame({"content": "Hallo "}) + frame({"content": "Welt"}) \
    + frame({}, "stop") + DONE
out, _ = run(plain, 64)
check(out == plain, "ohne Marker nicht byte-gleich")

# 6. _stream_response liest nach dem Schnitt nicht weiter
class Antwort:
    """Liefert Rahmen bis kurz nach dem Marker und blockiert dann.

    Der Lesefaden darf dem Verarbeitungsfaden vorauslaufen; deshalb haelt der
    Fake nach dem Marker an, statt bis EOF durchzulaufen. Hielt der Waechter
    beim Schnitt nicht an, zoege der Leser nach der Freigabe weiter und
    resp.i ueberstiege die Marke."""

    def __init__(self, frames):
        self.frames, self.i, self.gelesen, self.status = list(frames), 0, 0, 200
        self.blockiert = threading.Event()

    def getheader(self, name, default=""):
        return "text/event-stream" if name == "Content-Type" else default

    def read1(self, n=-1):
        self.i += 1
        if self.gelesen >= marker_index + 1:   # Marker plus einen Rahmen
            self.blockiert.set()
            freigabe.wait(5.0)
        if self.gelesen >= len(self.frames):
            return b""
        self.gelesen += 1
        return self.frames[self.gelesen - 1]


class H(lp.Handler):
    def __init__(self, wfile, conn):
        self.wfile, self.connection = wfile, conn

    def send_response(self, *a, **k):
        pass

    def send_header(self, *a, **k):
        pass

    def end_headers(self):
        pass


frames = [frame({"content": "ok"}), frame({"content": "<|im_end|>"})] + [frame({"content": "x"})] * 50 + [DONE]
marker_index = frames.index(frame({"content": "<|im_end|>"})) + 1
freigabe = threading.Event()
resp = Antwort(frames)
a, b = socket.socketpair()
h = H(io.BytesIO(), a)
vorher = set(threading.enumerate())
fertig = {}


def fahre():
    h._stream_response(resp, "pruef", 0.0, 0.0, "", "POST", "/v1/chat/completions",
                       is_streaming=True, chatml_guard=True)
    fertig["zurueck"] = True


w = threading.Thread(target=fahre)
w.start()
# Der Fake blockiert nach dem Marker plus einem Rahmen. Der Waechter muss den
# Leser stoppen; sonst zoege der Leser nach der Freigabe weiter und resp.i
# ueberstiege marker_index + 2. Der Anrufer sieht nichts nach dem Marker.
check(resp.blockiert.wait(2.0), "Leser blockierte nicht nach dem Marker")
w.join(1.0)
check(not w.is_alive(), "Handler kehrte nach dem Schnitt nicht zurueck")
check(fertig.get("zurueck") is True, "Handler brach ab, statt den Schnitt zu beenden")
leser = [t for t in threading.enumerate() if t not in vorher and t is not w]
check(len(leser) == 1, f"genau ein Leserfaden erwartet, gefunden {leser}")
freigabe.set()
for t in leser:
    t.join(2.0)
check(all(not t.is_alive() for t in leser), "Leserfaden endete nach dem Schnitt nicht")
check(resp.i <= marker_index + 2,
      f"nach dem Schnitt weitergelesen: {resp.i} Rahmen statt <= {marker_index + 2}")
a.close(); b.close()
check(b'"x"' not in h.wfile.getvalue(), "Text nach dem Marker kam beim Anrufer an")
check(b'"stop"' in h.wfile.getvalue() and h.wfile.getvalue().count(b"[DONE]") == 1, "Integration: kein sauberes Ende")

# 7. durchgehend: Strom ohne stop-Strings, Nicht-Strom mit
seen = []


class Backend(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "fake-local", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        j = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append(j)
        if j.get("stream"):
            body = frame({"content": "ok"}) + frame({}, "stop") + DONE
            ct = "text/event-stream"
        else:
            body = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                            "finish_reason": "stop"}]}).encode()
            ct = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


be = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Backend)
threading.Thread(target=be.serve_forever, daemon=True).start()
lp.LOCAL_BACKENDS[:] = [("fake", "127.0.0.1", be.server_address[1])]
px = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lp.Handler)
threading.Thread(target=px.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{px.server_address[1]}/v1/chat/completions"
for stream in (True, False):
    req = urllib.request.Request(url, json.dumps({"model": "fake-local", "stream": stream,
                                                  "messages": [{"role": "user", "content": "hi"}]}).encode(),
                                 {"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        check(False, f"durchgehend stream={stream}: {e}")
check(len(seen) == 2, f"Backend sah {len(seen)} Anfragen")
if len(seen) == 2:
    st_stream, st_plain = seen[0].get("stop") or [], seen[1].get("stop") or []
    check(not any(m in st_stream for m in lp._CHATML), f"Strom-Anfrage traegt noch stop {st_stream}")
    check(all(m in st_plain for m in lp._CHATML), f"Nicht-Strom-Anfrage ohne ChatML-stop {st_plain}")
px.shutdown(); be.shutdown()

print("GRUEN" if not fail else f"ROT ({fail})")
sys.exit(1 if fail else 0)
