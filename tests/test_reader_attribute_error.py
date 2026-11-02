#!/usr/bin/env python3
"""Ein AttributeError des Lesefadens wird nur als Client-Abbruch getarnt, wenn er es ist.

Der bekannte Wettlauf (Wachhund kappt den Socket, fp wird None) ist ein
Stromabriss. Jeder andere AttributeError -- ein echter Programmfehler -- muss
Typ und Meldung behalten, sonst verschwindet die Ursache hinter
"Backend-Verbindung wurde unter dem Lesefaden geschlossen".

ROT auf bb7c71f: beide Pfade (Token-Chat, Responses-Passthrough) meldeten
jeden AttributeError als Client-Abbruch, ohne gone-Beweis.
"""
import io
import os
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402
from _feyntalk_fixture import make_material  # noqa: E402

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


class Abriss:
    """Erstes Byte liefert die Hauptfaden-Abfrage, danach wirft der Lesefaden."""

    status = 200

    def __init__(self, fehler):
        self.fehler, self.aufrufe = fehler, 0

    def getheader(self, name, default=""):
        return "text/event-stream" if name == "Content-Type" else default

    def read1(self, n=-1):
        self.aufrufe += 1
        if self.aufrufe == 1:
            return b""
        raise AttributeError(self.fehler)


def lauf(funktion, material, gone):
    ca, cb = socket.socketpair()
    h = H(io.BytesIO(), ca)
    request = {"material": material, "tools": None,
               "model": material["model"]["served_id"], "stream": True}
    try:
        funktion(h, Abriss("kaputt"), request, gone)
        return None
    except BaseException as e:   # noqa: BLE001 - genau die Frage
        return e
    finally:
        for s in (ca, cb):
            try:
                s.close()
            except OSError:
                pass


def chat(h, antwort, request, gone):
    kw = {"gone_evt": gone} if "gone_evt" in h._stream_chat_from_tokens.__code__.co_varnames else {}
    h._stream_chat_from_tokens(antwort, "eng", 0.0, 0.0, " (test)", "POST",
                               "/v1/chat/completions", request, **kw)


def responses(h, antwort, request, gone):
    kw = {"gone_evt": gone} if "gone_evt" in h._stream_responses_from_tokens.__code__.co_varnames else {}
    h._stream_responses_from_tokens(antwort, "eng", 0.0, 0.0, " (test)", "POST",
                                    "/v1/responses", request, **kw)


material = make_material()
for name, funktion in (("Token-Chat", chat), ("Responses-Passthrough", responses)):
    # a) kein gone: echter Programmfehler behaelt Typ und Meldung.
    e = lauf(funktion, material, threading.Event())
    check(isinstance(e, lp.StromAbgeschnitten),
          f"{name}: {type(e).__name__} statt StromAbgeschnitten")
    text = str(e)
    check("AttributeError" in text and "kaputt" in text,
          f"{name}: Programmfehler verlor Typ/Meldung: {text!r}")
    check("unter dem Lesefaden geschlossen" not in text,
          f"{name}: Programmfehler wurde als Client-Abbruch gemeldet")

    # b) gone gesetzt: der bekannte Wettlauf ist ein Client-Abbruch.
    gone = threading.Event()
    gone.set()
    e = lauf(funktion, material, gone)
    check(isinstance(e, lp.StromAbgeschnitten),
          f"{name}: {type(e).__name__} statt StromAbgeschnitten")
    text = str(e)
    check("unter dem Lesefaden geschlossen" in text,
          f"{name}: Client-Abbruch nicht erkannt: {text!r}")
    check("AttributeError" not in text,
          f"{name}: Client-Abbruch nennt den fremden Attributnamen: {text!r}")

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
