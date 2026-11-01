#!/usr/bin/env python3
"""Der Strom reisst mitten im Lesen ab: was meldet _stream_response?

Nachgestellt wird der Wettlauf mit _client_watchdog. Der Wachhund schliesst die
Backend-Verbindung, http.client setzt sein fp auf None, und der naechste
resp.read1() wirft `AttributeError: 'NoneType' object has no attribute 'read1'`.

Diese Pruefung wird ROT gegen den Stand vor dem Fix (dort faellt der nackte
AttributeError durch) und GRUEN, sobald _stream_response ihn in
StromAbgeschnitten uebersetzt. Gegenprobe eingebaut: ein Strom, der sauber
endet, darf NICHT werfen -- sonst bestuende die Pruefung auch bei einem "Fix",
der einfach immer wirft.

Aufruf:  python3 tests/test_stream_cut.py [pfad-zu-llm-proxy.py]
"""

import importlib.util
import io
import os
import socket
import sys

PFAD = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "llm-proxy.py")


def laden(pfad):
    spec = importlib.util.spec_from_file_location("lp_unter_test", pfad)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    # Kein Testlaerm im Betriebsprotokoll: log() schreibt sonst nach
    # /var/log/llm-proxy.log und verwirrt die naechste Fehlersuche.
    m.log = lambda *a, **k: None
    return m


RAHMEN = b'data: {"choices":[{"index":0,"delta":{"content":"x"}}]}\n\n'
SCHLUSS = b'data: [DONE]\n\n'


class FalscheAntwort:
    """SSE-Rahmen ausgeben und danach entweder enden oder abreissen."""

    def __init__(self, rahmen, abriss_nach=None):
        self.rahmen = list(rahmen)
        self.abriss_nach = abriss_nach
        self.i = 0
        self.status = 200

    def getheader(self, name, default=""):
        return "text/event-stream" if name == "Content-Type" else default

    def read1(self, n=-1):
        if self.abriss_nach is not None and self.i >= self.abriss_nach:
            # Wortgleich das, was http.client wirft, wenn fp unter ihm wegfaellt.
            raise AttributeError("'NoneType' object has no attribute 'read1'")
        if self.i >= len(self.rahmen):
            return b""
        self.i += 1
        return self.rahmen[self.i - 1]


def handler_bauen(m):
    class Pruefhandler(m.Handler):
        def __init__(self, wfile, conn):
            self.wfile = wfile
            self.connection = conn

        def send_response(self, *a, **k):
            pass

        def send_header(self, *a, **k):
            pass

        def end_headers(self):
            pass

    a, b = socket.socketpair()
    h = Pruefhandler(io.BytesIO(), a)
    return h, (a, b)


def lauf(m, antwort):
    h, socken = handler_bauen(m)
    try:
        h._stream_response(antwort, "pruef-backend", 0.0, 0.0, " (test)",
                           "POST", "/v1/chat/completions", is_streaming=True)
        return None, h.wfile.getvalue()
    except BaseException as e:      # genau das ist die Frage
        return e, h.wfile.getvalue()
    finally:
        for s in socken:
            s.close()


def main():
    m = laden(PFAD)
    fehler = []

    # 1) Abriss mitten im Strom -> StromAbgeschnitten, nicht AttributeError.
    e, raus = lauf(m, FalscheAntwort([RAHMEN] * 40, abriss_nach=20))
    erwartet = getattr(m, "StromAbgeschnitten", None)
    if erwartet is None:
        fehler.append("StromAbgeschnitten gibt es nicht - Fix fehlt")
    elif not isinstance(e, erwartet):
        fehler.append(f"Abriss meldete {type(e).__name__}: {e!r} "
                      f"(erwartet StromAbgeschnitten)")
    elif "Bytes" not in str(e):
        fehler.append(f"Meldung nennt die Byte-Zahl nicht: {e!r}")
    elif not raus:
        fehler.append("nichts an den Anrufer weitergereicht, obwohl 20 Rahmen kamen")

    # 2) Gegenprobe: sauberer Schluss darf nicht werfen.
    e2, raus2 = lauf(m, FalscheAntwort([RAHMEN] * 12 + [SCHLUSS]))
    if e2 is not None:
        fehler.append(f"sauberer Strom warf {type(e2).__name__}: {e2!r}")
    elif SCHLUSS not in raus2:
        fehler.append("sauberer Strom kam ohne [DONE] beim Anrufer an")

    if fehler:
        for f in fehler:
            print("ROT: " + f)
        return 1
    print("GRUEN: Abriss -> StromAbgeschnitten (%s), sauberer Strom laeuft durch" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
