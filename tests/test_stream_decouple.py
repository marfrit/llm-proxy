#!/usr/bin/env python3
"""Stream hardening: der Gateway liest den Engine-Strom immer ab, auch wenn das Schreiben stockt.

Drei Fragen, je eine Pruefung:

a) Die Engine schreibt mit 5 s Schreibfrist; der Gateway-Anrufer holt 6 s
   mitten im Strom nichts ab. Vorher steht der Gateway-Leser selbst im
   blockierten Schreibvorgang, die Engine faellt in ihre Schreibfrist und
   bricht ab ("client gone"). Nachher puffert ein Leserfaden den rohen
   SSE-Strom, und der Zug laeuft durch.

b) Die Kosten je Ereignis wachsen nicht mit der Position im Strom (kein
   `"".join` des ganzen Praefix je Ereignis).

c) Legt der Anrufer mitten im Strom auf, wird die Upstream-Verbindung
   trotzdem innerhalb einer Sekunde gekappt.

ROT vor dem Fix: a) die Engine meldet die Schreibfrist, b) die letzten
Viertel kosten mehr als das Doppelte je Ereignis.
"""
import io
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402
from _feyntalk_fixture import (engine_stream, make_material,  # noqa: E402
                               span_event, token_event)
import bench_token_stream  # noqa: E402
from feyntalk_render.spans import normalize_tokens  # noqa: E402

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
        self.headers = {"get": lambda k, d=None: d,
                        "Content-Type": "application/json"}

    def send_response(self, *a, **k):
        pass

    def send_header(self, *a, **k):
        pass

    def end_headers(self):
        pass


class SlowWfile(io.BytesIO):
    """wfile, das nach der n-ten Leerung einmal 6 s blockiert (langsamer Anrufer)."""

    def __init__(self, sleep_on_flush, sleep_seconds):
        super().__init__()
        self._flushes = 0
        self._sleep_on = sleep_on_flush
        self._sleep_seconds = sleep_seconds
        self.slept = False

    def flush(self):
        self._flushes += 1
        if not self.slept and self._flushes >= self._sleep_on:
            self.slept = True
            time.sleep(self._sleep_seconds)


class EnginePipe:
    """Fake-Engine: ein Schreiber mit Frist schiebt SSE-Rahmen in eine 2-Platz-Roehre.

    Bleibt der Leser lange genug weg, laeuft `put` in die Frist -- genau das, was
    eine echte Engine mit 5 s Socket-Schreibfrist als "client gone" erlebt."""

    status = 200

    def __init__(self, chunks, write_timeout=5.0, slots=2):
        self.chunks = chunks
        self.write_timeout = write_timeout
        self.buf = []
        self.timed_out = False
        self.produced = 0
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._writer = threading.Thread(target=self._produce, daemon=True)
        self._writer.start()

    def _produce(self):
        try:
            for chunk in self.chunks:
                if not self._offer(chunk):
                    return
                self.produced += 1
            self._offer(None)
        finally:
            self.done.set()

    def _offer(self, chunk):
        deadline = time.monotonic() + self.write_timeout
        with self._cv:
            while len(self.buf) >= 2:
                rest = deadline - time.monotonic()
                if rest <= 0:
                    self.timed_out = True
                    return False
                self._cv.wait(timeout=rest)
            self.buf.append(chunk)
            self._cv.notify()
            return True

    def getheader(self, name, default=""):
        return "text/event-stream" if name == "Content-Type" else default

    def read1(self, n):
        with self._cv:
            while not self.buf:
                if self.done.is_set():
                    return b""
                self._cv.wait(timeout=0.25)
            item = self.buf.pop(0)
            self._cv.notify()
        return b"" if item is None else item


def token_request(material, stream=True):
    return {"material": material, "tools": None,
            "model": material["model"]["served_id"], "stream": stream}


def run_token(resp, request, wfile, sock=None, conn=None):
    h = H(wfile, conn)
    h._stream_chat_from_tokens(resp, "eng", 0.0, 0.0, " (test)", "POST",
                               "/v1/chat/completions", request, sock=sock)
    return h.wfile.getvalue()


def parse_chat(out):
    content = ""
    reasoning = ""
    finish = []
    for block in out.decode("utf-8").split("\n\n"):
        line = block.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        j = json.loads(payload)
        choice = j["choices"][0]
        delta = choice.get("delta") or {}
        content += delta.get("content") or ""
        reasoning += delta.get("reasoning_content") or ""
        if choice.get("finish_reason"):
            finish.append(choice["finish_reason"])
    return content, reasoning, finish


# a) langsamer Anrufer: der Leser puffert, die Engine faellt nicht in ihre Frist
material = make_material()
events, n = bench_token_stream.synthesize(material, 1500, 1000)
chunks = [bench_token_stream.sse(e) for e in events]
engine = EnginePipe(chunks, write_timeout=5.0, slots=2)
wfile = SlowWfile(sleep_on_flush=20, sleep_seconds=6.0)
out = run_token(engine, token_request(material), wfile)
expected = normalize_tokens(material, events)
content, reasoning, finish = parse_chat(out)
check(engine.timed_out is False, "Engine lief in ihre 5-s-Schreibfrist (client gone)")
check(engine.produced == len(chunks),
      f"Engine lieferte {engine.produced} von {len(chunks)} Rahmen")
check(content == expected.text,
      f"content weicht ab: {len(content)} != {len(expected.text)} Zeichen")
check(reasoning == expected.reasoning,
      f"reasoning weicht ab: {len(reasoning)} != {len(expected.reasoning)} Zeichen")

# b) die Kosten je Ereignis wachsen nicht mit der Position im Strom
#
# Mit der vollen Obergrenze (16 MiB) laeuft der Leserfaden dem Schreibpfad
# beliebig weit voraus: `read1`-zu-`read1` misst dann nur noch die Schleife
# des Lesers samt GIL-Konkurrenz, nicht die Verarbeitung. Damit die Messung
# das behauptet, was sie prueft, wird die Obergrenze hier klein gewaehlt --
# der Leser staut am Puffer zurueck und sein naechstes `read1` faellt mit dem
# Verarbeitungsschritt des Schreibpfads zusammen. (a) prueft die volle Grenze,
# (b) die Kosten je Ereignis; das Praefix-Join war in beiden Grenzen dieselbe
# superlineare Stelle.
urspruengliche_grenze = lp.STREAM_QUEUE_MAX_BYTES
lp.STREAM_QUEUE_MAX_BYTES = 8 * 1024
material = make_material()
events, _ = bench_token_stream.synthesize(material, 1200, 1200, ids=[1, 2, 7, 6])
chunks = [bench_token_stream.sse(e) for e in events] + [b""]
warm = EnginePipe(chunks, write_timeout=5.0, slots=2)
run_token(warm, token_request(material), io.BytesIO())
resp = bench_token_stream.FakeResp(list(chunks))
run_token(resp, token_request(material), io.BytesIO())
lp.STREAM_QUEUE_MAX_BYTES = urspruengliche_grenze
costs = [c for _, c in resp.costs][1:-1]
q = len(costs) // 4
first = sum(costs[:q]) / q
last = sum(costs[-q:]) / q
check(costs and last < 2 * first,
      f"Kosten je Ereignis steigen mit der Position: erst {first * 1e6:.1f}us, "
      f"zuletzt {last * 1e6:.1f}us")


# c) der Anrufer legt auf: ein fehlgeschlagener Schreibvorgang beendet den Zug
#    als ClientGone (kein Backend-Fehler, kein routed-FAIL). Ein halber Schluss
#    allein wird nicht mehr gekappt -- der Client darf weiterlesen.
class SocketResp:
    status = 200

    def __init__(self, sock):
        self.sock = sock

    def read1(self, n):
        return self.sock.recv(n)


ca, cb = socket.socketpair()
ba, bb = socket.socketpair()
h = H(ca.makefile("wb"), ca)
state = {}


pump_stop = threading.Event()


def _pumpe():
    for i in range(400):
        if pump_stop.is_set():
            return
        try:
            bb.sendall(bench_token_stream.sse(token_event(i, 1, "text")))
        except OSError:
            return
        time.sleep(0.02)


def _drive():
    try:
        h._stream_chat_from_tokens(SocketResp(ba), "eng", 0.0, 0.0, " (test)", "POST",
                                   "/v1/chat/completions", token_request(make_material()),
                                   sock=None)
    except BaseException as e:  # noqa: BLE001 - im Faden nur festhalten, nicht werfen
        state["exc"] = e
    finally:
        state["done"] = True


pumpe = threading.Thread(target=_pumpe, daemon=True)
pumpe.start()
driver = threading.Thread(target=_drive, daemon=True)
driver.start()
time.sleep(0.3)
cb.close()
driver.join(2.0)
check(not driver.is_alive(), "Stream lief nach dem Schliessen des Anrufers weiter")
check(isinstance(state.get("exc"), lp.ClientGone),
      f"kein ClientGone beim Schreiben an den geschlossenen Anrufer: {state.get('exc')!r}")
pump_stop.set()
pumpe.join(0.5)
try:
    h.wfile.close()
except OSError:
    pass
for sock in (ca, ba, bb):
    try:
        sock.close()
    except OSError:
        pass

# d) leere Stuecke erzeugen keine leeren Rahmen
#
# token1 (b"\xc3") ist ein unvollstaendiges UTF-8-Zeichen: der Detokeniser
# haelt es zurueck und liefert "" als Stueck. Ein leeres Stueck darf keinen
# Delta-Rahmen mit content=""/reasoning_content="" erzeugen -- und der
# sichtbare Text muss trotzdem dem normalisierten Strom entsprechen.
material_d = make_material()
events_d = [
    span_event("thinking", "start", 0, {"preopened": True}),
    token_event(0, 10, "reasoning"),
    token_event(1, 4, "reasoning"),
    token_event(2, 5, "reasoning"),
    token_event(3, 11, "reasoning"),
    span_event("thinking", "end", 3, {"closed": True}),
    token_event(4, 4, "text"),
    token_event(5, 5, "text"),
    token_event(6, 1, "text"),
    {"type": "response.completed",
     "response": {"id": "resp_fixture", "created_at": 1700000000,
                  "status": "completed",
                  "output": [{"type": "message", "role": "assistant",
                              "content": [{"type": "output_text",
                                           "text": "\u00fcHello"}]}],
                  "usage": {"input_tokens": 11, "output_tokens": 7,
                            "total_tokens": 18}}},
]
out_d = run_token(bench_token_stream.FakeResp(
    [bench_token_stream.sse(e) for e in events_d] + [b""]),
    token_request(material_d), io.BytesIO())


def delta_teile(out):
    teile = []
    for block in out.decode("utf-8").split("\n\n"):
        line = block.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        teile.append(json.loads(payload)["choices"][0].get("delta") or {})
    return teile


deltas = delta_teile(out_d)
leer = [d for d in deltas if d.get("content") == "" or d.get("reasoning_content") == ""]
check(not leer, f"leere Stuecke erzeugten {len(leer)} leere Delta-Rahmen")
expected_d = normalize_tokens(material_d, events_d)
content_d, reasoning_d, _ = parse_chat(out_d)
check(content_d == expected_d.text,
      f"content weicht ab: {content_d!r} != {expected_d.text!r}")
check(reasoning_d == expected_d.reasoning,
      f"reasoning weicht ab: {reasoning_d!r} != {expected_d.reasoning!r}")

# e) Lesefaden wirft mitten im Strom: das ist ein Stromabriss, kein Traceback
class BoomResp:
    status = 200

    def __init__(self, erster):
        self.erster = erster
        self.gelesen = False

    def read1(self, n):
        if not self.gelesen:
            self.gelesen = True
            return self.erster
        raise AttributeError("'NoneType' object has no attribute 'read1'")


try:
    run_token(BoomResp(bench_token_stream.sse(events_d[0])),
              token_request(material_d), io.BytesIO())
except BaseException as e:  # noqa: BLE001 - genau das ist die Frage
    check(isinstance(e, lp.StromAbgeschnitten),
          f"Lesefaden-Abriss meldete {type(e).__name__}: {e!r} (erwartet StromAbgeschnitten)")
else:
    check(False, "Lesefaden-Abriss warf nicht")

# f) Responses-Relais entkoppelt: der Anrufer stockt 6 s, die Engine nicht
material_resp = make_material()
raw_resp = engine_stream("answer")
parts_resp = [p + b"\n\n" for p in raw_resp.split(b"\n\n") if p]
engine_resp = EnginePipe(parts_resp, 5.0, 2)
wfile_resp = SlowWfile(sleep_on_flush=5, sleep_seconds=6.0)
h_resp = H(wfile_resp, None)
h_resp._stream_responses_from_tokens(engine_resp, "eng", 0, 0, "", "POST",
                                     "/v1/responses",
                                     {"material": material_resp, "tools": None})
check(engine_resp.timed_out is False,
      "Responses-Relais: Engine lief in ihre 5-s-Schreibfrist")
check(wfile_resp.slept, "Responses-Relais: der Stockfall wurde nie erreicht")
check(h_resp.wfile.getvalue() == raw_resp,
      "Responses-Relais: Ausgabe ist nicht byte-gleich zum rohen Strom")

# g) Text-Relais entkoppelt: derselbe Stock-Test auf dem Chat/completions-Weg
class ConnStub:
    """Nur die Frist-Aufrufe, die _stream_response auf der Verbindung macht."""

    def gettimeout(self):
        return None

    def settimeout(self, t):
        pass


modell = make_material()["model"]["served_id"]
frames_text = []
for _ in range(40):
    frames_text.append(("data: " + json.dumps({
        "id": "chatcmpl-decouple", "object": "chat.completion.chunk",
        "model": modell,
        "choices": [{"index": 0, "delta": {"content": "x"},
                     "finish_reason": None}]}, ensure_ascii=False) + "\n\n").encode("utf-8"))
frames_text.append(b"data: [DONE]\n\n")
engine_text = EnginePipe(frames_text, 5.0, 2)
wfile_text = SlowWfile(sleep_on_flush=20, sleep_seconds=6.0)
h_text = H(wfile_text, ConnStub())
h_text._stream_response(engine_text, "eng", 0, 0, "", "POST",
                        "/v1/chat/completions", is_streaming=True)
check(engine_text.timed_out is False,
      "Text-Relais: Engine lief in ihre 5-s-Schreibfrist")
check(wfile_text.slept, "Text-Relais: der Stockfall wurde nie erreicht")
check(h_text.wfile.getvalue() == b"".join(frames_text),
      "Text-Relais: Ausgabe ist nicht byte-gleich zu den Rahmen")

# h) der finish()-Schwanz des Detokenisers erreicht den Anrufer
#
# Ein Strom, der in einem unvollstaendigen UTF-8-Zeichen endet: mit dem
# strikten finish() waere das ein Abbruch. Die gepatchte Fassung gibt die
# gehaltenen Bytes als latin-1 aus -- der Text muss dann beim Anrufer ankommen.
import feyntalk_render.detok as ft_detok  # noqa: E402

_ft_finish_original = ft_detok.IncrementalDetokenizer.finish


def _ft_finish_latin1(self):
    if self._finished:
        return ""
    self._finished = True
    return bytes(self._decoder.getstate()[0]).decode("latin-1")


material_h = make_material()
events_h = [
    span_event("thinking", "start", 0, {"preopened": True}),
    token_event(0, 10, "reasoning"),
    span_event("thinking", "end", 1, {"closed": True}),
    token_event(1, 4, "text"),
]
try:
    ft_detok.IncrementalDetokenizer.finish = _ft_finish_latin1
    out_h = run_token(bench_token_stream.FakeResp(
        [bench_token_stream.sse(e) for e in events_h] + [b""]),
        token_request(material_h), io.BytesIO())
    content_h, _, _ = parse_chat(out_h)
    check("\u00c3" in content_h,
          f"finish()-Schwanz fehlt im content: {content_h!r}")
finally:
    ft_detok.IncrementalDetokenizer.finish = _ft_finish_original

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
