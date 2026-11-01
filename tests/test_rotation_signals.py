#!/usr/bin/env python3
"""Rotationssignale durch den Proxy: finish_reason und loop_guard bleiben beim Klienten.

Die pi-Erweiterung entscheidet ueber eine Rotation an `finish_reason` (arcint: "loop",
"length" ohne Antwort) und am `loop_guard`-Objekt {detections, nudges, slaps, aborts,
self_closes, door_masked}. Der Proxy sitzt dazwischen und baut auf dem Strompfad Frames
neu (ReasoningSeam trennt einen Endframe, ChatMLGuard beendet den Zug, ToolTextSalvage
schreibt den Schwanz um). Jeder dieser Umbauten muss die Randschluessel des Backends
mitnehmen - sonst feuert der Ausloeser nie (stiller falscher Negativ).

ROT, wenn
  - `finish_reason: "loop"` nicht beim Klienten ankommt (Seam, ToolTextSalvage, Nicht-Strom);
  - `loop_guard` (und andere Randschluessel wie usage/timings) auf dem Frame mit
    `finish_reason` fehlen, den ein Filter neu gebaut hat;
  - ein unbekannter finish_reason ("weird_reason") umgeschrieben wird;
  - ChatMLGuard beim fruehen Schnitt die gesehenen Randschluessel verliert oder den
    Override nicht protokolliert;
  - die Schlusszeile des Stroms den finish_reason/Zaehler nicht nennt oder Prompttext traegt.

Aufruf:  python3 tests/test_rotation_signals.py [pfad-zu-llm-proxy.py]
"""
import io
import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402

lp = lade()
fail = 0


def check(cond, msg):
    global fail
    if not cond:
        print("ROT:", msg)
        fail += 1


def frame(delta, finish=None, zusatz=None):
    j = {"id": "rot-1", "object": "chat.completion.chunk", "created": 1,
         "model": "backend.example/modell-test",
         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if zusatz:
        j.update(zusatz)
    return ("data: " + json.dumps(j, ensure_ascii=False) + "\n\n").encode()


DONE = b"data: [DONE]\n\n"
LOOP_GUARD = {"detections": 3, "nudges": 2, "slaps": 1, "aborts": 1,
              "self_closes": 0, "door_masked": 0}
ZUSATZ = {"loop_guard": LOOP_GUARD, "timings": {"predicted_ms": 249000},
          "usage": {"prompt_tokens": 11, "completion_tokens": 22}}


def daten(out):
    """Alle JSON-Frames der Ausgabe, in Sendereihenfolge."""
    frames = []
    for zeile in out.decode().split("\n"):
        z = zeile.strip()
        if not z.startswith("data: ") or z[6:] == "[DONE]":
            continue
        try:
            frames.append(json.loads(z[6:]))
        except Exception:
            pass
    return frames


def endframe(out):
    """Der Frame, der den Zug beendet (traegt finish_reason)."""
    for j in daten(out):
        if j.get("choices") and j["choices"][0].get("finish_reason") is not None:
            return j
    return None


def durch_seam(raw, size=7):
    seam = lp.ReasoningSeam()
    out = b""
    for i in range(0, len(raw), size):
        out += seam.feed(raw[i:i + size])
    return out + seam.flush()


def durch_salvage(raw, size=7, tools=None):
    sal = lp.ToolTextSalvage(tools)
    out = b""
    for i in range(0, len(raw), size):
        out += sal.feed(raw[i:i + size])
    return out + sal.flush()


# 1. lokaler Denkstrom, der mit finish_reason "loop" und loop_guard endet, durch die Naht
raw = (frame({"role": "assistant", "content": ""})
       + frame({"reasoning": "ich pruefe"})
       + frame({"content": "denk.</think>Antwort"}, "loop", ZUSATZ) + DONE)
out = durch_seam(raw)
end = endframe(out)
check(end is not None and end["choices"][0]["finish_reason"] == "loop",
      f"Seam: finish_reason {None if end is None else end['choices'][0]['finish_reason']!r} statt 'loop'")
check(end is not None and end.get("loop_guard") == LOOP_GUARD,
      f"Seam: loop_guard fehlt/veraendert: {None if end is None else end.get('loop_guard')!r}")
check(end is not None and end.get("timings") == ZUSATZ["timings"] and end.get("usage") == ZUSATZ["usage"],
      f"Seam: Randschluessel fehlen: {None if end is None else sorted(end)}")

# 2. derselbe Strom durch ToolTextSalvage (Frame mit Inhalt und finish_reason zugleich,
#    das ist der Umbau-Pfad: der Filter baut Inhalt- und Endframe neu)
raw = (frame({"content": "x"}, "loop", ZUSATZ)
       + DONE)
out = durch_salvage(raw)
end = endframe(out)
check(end is not None and end["choices"][0]["finish_reason"] == "loop",
      f"Salvage: finish_reason {None if end is None else end['choices'][0]['finish_reason']!r} statt 'loop'")
check(end is not None and end.get("loop_guard") == LOOP_GUARD,
      f"Salvage: loop_guard fehlt/veraendert: {None if end is None else end.get('loop_guard')!r}")
check(end is not None and end.get("usage") == ZUSATZ["usage"],
      f"Salvage: usage fehlt: {None if end is None else sorted(end)}")

# 3. Nicht-Strom: der Rumpf geht byte-gleich raus (finish_reason + loop_guard unveraendert)
nichtstrom = json.dumps({"id": "rot-1", "object": "chat.completion",
                         "choices": [{"index": 0,
                                      "message": {"role": "assistant", "content": ""},
                                      "finish_reason": "loop"}],
                         "loop_guard": LOOP_GUARD,
                         "usage": {"prompt_tokens": 11}}).encode()


class Antwort:
    def __init__(self, chunks, ctype):
        self.chunks, self.i, self.status = list(chunks), 0, 200
        self.ctype = ctype

    def getheader(self, name, default=""):
        return self.ctype if name == "Content-Type" else default

    def read1(self, n=-1):
        if self.i >= len(self.chunks):
            return b""
        self.i += 1
        return self.chunks[self.i - 1]


class H(lp.Handler):
    def __init__(self, wfile, conn):
        self.wfile, self.connection = wfile, conn

    def send_response(self, *a, **k):
        pass

    def send_header(self, *a, **k):
        pass

    def end_headers(self):
        pass


def lauf(antwort, is_streaming, hint="", chatml=False):
    a, b = socket.socketpair()
    h = H(io.BytesIO(), a)
    try:
        h._stream_response(antwort, "backend.example", 0.0, 0.0, hint, "POST",
                           "/v1/chat/completions", is_streaming=is_streaming,
                           chatml_guard=chatml)
        return h.wfile.getvalue()
    finally:
        a.close(); b.close()


out = lauf(Antwort([nichtstrom], "application/json"), False)
check(out == nichtstrom, "Nicht-Strom: Rumpf nicht byte-gleich")
try:
    j = json.loads(out)
    check(j.get("choices")[0].get("finish_reason") == "loop" and j.get("loop_guard") == LOOP_GUARD,
          "Nicht-Strom: finish_reason/loop_guard veraendert")
except Exception as e:
    check(False, f"Nicht-Strom: kein JSON ({type(e).__name__}: {e})")

# 4. unbekannter finish_reason bleibt unveraendert (auch im Umbau-Pfad)
out = durch_seam(frame({"content": "ok"}, "weird_reason") + DONE)
end = endframe(out)
check(end is not None and end["choices"][0]["finish_reason"] == "weird_reason",
      f"Seam: weird_reason -> {None if end is None else end['choices'][0]['finish_reason']!r}")
out = durch_salvage(frame({"content": "ok"}, "weird_reason") + DONE)
end = endframe(out)
check(end is not None and end["choices"][0]["finish_reason"] == "weird_reason",
      f"Salvage: weird_reason -> {None if end is None else end['choices'][0]['finish_reason']!r}")

# 5. ein Frame, den die Naht trennt, traegt seine Randschluessel auf dem Endframe
raw = (frame({"role": "assistant", "content": ""})
       + frame({"reasoning": "hm"})
       + frame({"content": "denk.</think>Ja"}, "loop", {"loop_guard": LOOP_GUARD})
       + DONE)
out = durch_seam(raw)
end = endframe(out)
check(end is not None and end.get("loop_guard") == LOOP_GUARD,
      f"Naht-Trennung: Randschluessel auf dem Endframe fehlen: {None if end is None else sorted(end)}")

# 6. ChatMLGuard beendet frueh: gesehene Randschluessel bleiben, Override wird protokolliert
lp._testlog.clear()
raw = (frame({"role": "assistant", "content": ""})
       + frame({"content": "Antwort<|im_end|>Rest"}, "loop", {"loop_guard": LOOP_GUARD})
       + DONE)
g = lp.ChatMLGuard()
out = g.feed(raw) + g.flush()
end = endframe(out)
check(g.cut, "ChatMLGuard: Marker im content beendete den Zug nicht")
check(end is not None and end["choices"][0]["finish_reason"] == "stop",
      f"ChatMLGuard: Ende traegt {None if end is None else end['choices'][0]['finish_reason']!r} statt 'stop'")
check(end is not None and end.get("loop_guard") == LOOP_GUARD,
      f"ChatMLGuard: loop_guard ging beim Schnitt verloren: {None if end is None else sorted(end)}")
check(any("finish_reason" in z and "stop" in z and ("loop" in z or "override" in z.lower())
          for z in lp._testlog),
      f"ChatMLGuard: Override nicht protokolliert: {lp._testlog[-3:]}")

# 7. Schlusszeile des Stroms: finish_reason und loop_guard-Zaehler, kein Prompttext
lp._testlog.clear()
GEHEIM = "GEHEIMER-PROMPT-TEXT"
raw = (frame({"role": "assistant", "content": ""})
       + frame({"content": "Antwort"}, "loop", ZUSATZ) + DONE)
lauf(Antwort([raw], "text/event-stream"), True, hint=" (test)")
zeilen = [z for z in lp._testlog if "stream=True" in z]
check(len(zeilen) >= 1, f"keine Schlusszeile protokolliert: {lp._testlog}")
if zeilen:
    z = zeilen[-1]
    check("finish=loop" in z, f"Schlusszeile nennt finish_reason nicht: {z!r}")
    check("aborts=1" in z and "slaps=1" in z, f"Schlusszeile nennt loop_guard-Zaehler nicht: {z!r}")
    check(GEHEIM not in z, f"Schlusszeile traegt Prompttext: {z!r}")

# 8. Der Zaehler-Frame des Backends kommt NACH dem finish (OpenAI-Form: letzter
#    usage-Chunk mit leerem delta oder leeren choices) - er muss als Frame ankommen,
#    mit loop_guard und usage, und die Schlusszeile muss die Zaehler sehen.
def zusatz_frame(zusatz, choices=None):
    j = {"id": "rot-1", "object": "chat.completion.chunk", "created": 1,
         "model": "backend.example/modell-test"}
    j["choices"] = choices if choices is not None else [
        {"index": 0, "delta": {}, "finish_reason": None}]
    j.update(zusatz)
    return ("data: " + json.dumps(j, ensure_ascii=False) + "\n\n").encode()


TOOLS = lp._tool_schemas([{"type": "function", "function": {"name": "bash", "parameters": {
    "type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}], "auto")
DENK = (frame({"role": "assistant", "content": ""})
        + frame({"reasoning": "ich pruefe"})
        + frame({"content": "denk.</think>Antwort"}, "loop"))


def pruefe_zaehler_frame(name, raus, logzeilen):
    zframe = None
    for j in daten(raus):
        if j.get("choices") == [] or (j.get("choices") and j["choices"][0].get("delta") == {}):
            if j.get("loop_guard") == LOOP_GUARD and j.get("usage") == ZUSATZ["usage"]:
                zframe = j
    check(zframe is not None, f"{name}: Zaehler-Frame nach finish fehlt/ohne loop_guard+usage")
    z = [q for q in logzeilen if "stream=True" in q]
    check(bool(z), f"{name}: keine Schlusszeile: {logzeilen}")
    if z:
        check("loop_guard(detections=3" in z[-1] and "aborts=1" in z[-1],
              f"{name}: Schlusszeile nennt loop_guard-Zaehler nicht: {z[-1]!r}")


for form, choices in (("leeres delta", None), ("leere choices", [])):
    lp._testlog.clear()
    zf = zusatz_frame({"loop_guard": LOOP_GUARD, "usage": ZUSATZ["usage"]}, choices)
    raus = lauf(Antwort([DENK + zf + DONE], "text/event-stream"), True)
    pruefe_zaehler_frame(f"Seam, {form}", raus, lp._testlog)
    lp._testlog.clear()
    # mit ToolTextSalvage dahinter (Tools in der Anfrage)
    a, b = socket.socketpair()
    h = H(io.BytesIO(), a)
    try:
        h._stream_response(Antwort([DENK + zf + DONE], "text/event-stream"), "backend.example",
                           0.0, 0.0, "", "POST", "/v1/chat/completions", is_streaming=True,
                           salvage_tools=TOOLS)
        raus = h.wfile.getvalue()
    finally:
        a.close(); b.close()
    pruefe_zaehler_frame(f"Seam+Salvage, {form}", raus, lp._testlog)

# 9. Gegenprobe zum Drop-Zweig der Naht: ein Frame, dessen content ganz
#    zurueckgehalten wird, traegt danach nur noch Randschluessel (delta leer). Er darf
#    nicht verschwinden - sonst fiele `and not _frame_zusatz(j)` auf `and True` still
#    durch, und der Klient saehe die Randschluessel erst spaeter an einem anderen Frame.
#    Seit 1.1.0 haelt die Naht nur noch, wenn vorher ein reasoning-Delta kam; ohne
#    Denkblock gibt sie den Anfang sofort frei und der Drop-Zweig laeuft nie. Deshalb
#    steht hier ein reasoning-Frame vor dem Text.
raw = (frame({"reasoning": "ich pruefe"}) + frame({"content": "A"})
       + frame({"content": "B"}, zusatz={"timings": {"predicted_ms": 249000}})
       + frame({}, "stop") + DONE)
out = durch_seam(raw)
leer_mit_zusatz = [j for j in daten(out)
                   if j["choices"][0].get("delta") == {} and j.get("timings") == {"predicted_ms": 249000}]
check(len(leer_mit_zusatz) == 1,
      f"Naht: Frame mit nur Randschluesseln verschwand ({len(leer_mit_zusatz)} statt 1): "
      f"{[sorted(j) for j in daten(out)]}")

print("GRUEN" if not fail else f"ROT ({fail})")
sys.exit(1 if fail else 0)
