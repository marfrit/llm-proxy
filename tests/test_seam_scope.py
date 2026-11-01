#!/usr/bin/env python3
"""ReasoningSeam nur dort, wo er etwas reparieren kann.

Ein Wolkenstrom denkt nicht in einem </think>-Block des lokalen Parsers; der Seam
haelt dort nur den Antwortanfang zurueck und kostet sichtbare Zeit bis zum ersten
Token. Ein lokaler Denkstrom bekommt die Reparatur weiterhin. Und sobald ein
content-Delta ohne vorheriges reasoning kommt, kann keine Naht mehr folgen: der
Anfang geht sofort raus.

ROT gegen den Stand vor dem Fix: der Seam laeuft auf jedem Strom, das erste
content-Delta eines Wolkenstroms ist beim zweiten Lesevorgang noch nicht beim
Anrufer, und ein Strom ohne reasoning haelt seinen Anfang bis zur Schwelle.
"""
import io
import json
import os
import socket
import sys
import time

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
    return ("data: " + json.dumps({"id": "c", "object": "chat.completion.chunk", "model": "m",
                                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
            + "\n\n").encode()


class Antwort:
    """Liefert Rahmen stueckweise; nach dem zweiten Lesevorgang wird geprueft, ob
    das erste content-Delta schon beim Anrufer steht."""

    def __init__(self, rahmen, wfile):
        self.rahmen = list(rahmen)
        self.wfile = wfile
        self.i = 0
        self.status = 200
        self.erste_sichtbar = None

    def getheader(self, name, default=""):
        return "text/event-stream" if name == "Content-Type" else default

    def read1(self, n=-1):
        self.i += 1
        if self.i == 3:
            # Der Lesefaden darf dem Schreibpfad vorauslaufen; deshalb kurz
            # warten, ob das erste content-Delta ankommt, statt es am
            # Lesevorgang festzumachen. Haelt der Seam es zurueck, bleibt es aus.
            grenze = time.monotonic() + 0.5
            while b"Hallo" not in self.wfile.getvalue() and time.monotonic() < grenze:
                time.sleep(0.005)
            self.erste_sichtbar = b"Hallo" in self.wfile.getvalue()
        if self.i > len(self.rahmen):
            return b""
        return self.rahmen[self.i - 1]


class H(lp.Handler):
    def __init__(self, wfile, conn):
        self.wfile, self.connection = wfile, conn

    def send_response(self, *a, **k):
        pass

    def send_header(self, *a, **k):
        pass

    def end_headers(self):
        pass


def lauf(rahmen, seam_aktiv):
    a, b = socket.socketpair()
    h = H(io.BytesIO(), a)
    h._req_seam = seam_aktiv
    raus = Antwort(rahmen, h.wfile)
    try:
        h._stream_response(raus, "pruef", 0.0, 0.0, "", "POST",
                           "/v1/chat/completions", is_streaming=True)
        return raus, h.wfile.getvalue()
    finally:
        a.close()
        b.close()


# 1) Wolkenstrom: erstes content-Delta steht nach dem zweiten Lesevorgang beim Anrufer
rahmen = [frame({"role": "assistant"}), frame({"content": "Hallo"}),
          frame({"content": " Welt"}, "stop"), b"data: [DONE]\n\n"]
raus, out = lauf(rahmen, seam_aktiv=False)
check(raus.erste_sichtbar is True,
      "Wolkenstrom hielt das erste content-Delta zurueck (Seam lief unnoetig)")
check(b"Hallo" in out and b"Welt" in out, f"Wolkenstrom unvollstaendig: {out[:160]!r}")

# 1b) Wolkenstrom mit Denkblock: der lokale Seam darf ihn NICHT filtern
rahmen = [frame({"role": "assistant"}), frame({"reasoning": "hm"}),
          frame({"content": "denk.</think>Ja"}, "stop"), b"data: [DONE]\n\n"]
raus, out = lauf(rahmen, seam_aktiv=False)
check(b"denk." in out, f"Wolkenstrom wurde vom lokalen Seam beschnitten: {out!r}")

# 2) lokaler Denkstrom: die </think>-Naht wird weiterhin repariert
rahmen = [frame({"role": "assistant"}), frame({"reasoning": "hm"}),
          frame({"content": "denk.</think>Ja"}, "stop"), b"data: [DONE]\n\n"]
raus, out = lauf(rahmen, seam_aktiv=True)
check(b"denk." not in out and b"Ja" in out, f"lokale Naht nicht repariert: {out!r}")

# 3) ohne vorheriges reasoning kann keine Naht mehr folgen -> sofort raus
seam = lp.ReasoningSeam()
sofort = seam.feed(frame({"content": "Hallo"}))
check(b"Hallo" in sofort, f"Anfang ohne reasoning wurde weiter zurueckgehalten: {sofort!r}")

print("GRUEN" if not fail else f"ROT ({fail})")
sys.exit(1 if fail else 0)
