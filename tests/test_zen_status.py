#!/usr/bin/env python3
"""Zen-Fehler: den Status durchreichen, keinen Anfragetext ins Log.

Ein 429 des Anbieters ist eine Aussage ueber die Verfuegbarkeit, kein generischer
Proxy-Fehler: der Anrufer sieht 429 (mit Retry-After, wenn geliefert), 4xx bleibt
4xx, nur Netz- und 5xx-Fehler werden 502. Im Log steht Groesse, Modell und ein
kurzer Hash -- nicht der Prompt.

ROT gegen den Stand vor dem Fix: der Anrufer sieht 502 statt 429 und die Logzeile
enthaelt die ersten 500 Zeichen des Anfragetexts.
"""
import io
import json
import os
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


class Antwort:
    def __init__(self, status, body, kopf=None):
        self.status, self.body, self.kopf = status, body, kopf or {}

    def read(self):
        return self.body

    def getheader(self, name, default=""):
        return self.kopf.get(name, default)


class Verbindung:
    status = 429
    rumpf = b'{"error":"rate limited"}'
    kopf = {"Retry-After": "7"}
    gesehen = []

    def __init__(self, host, port, timeout=None, context=None):
        pass

    def request(self, method, path, body=None, headers=None):
        Verbindung.gesehen.append(body)

    def getresponse(self):
        return Antwort(Verbindung.status, Verbindung.rumpf, Verbindung.kopf)

    def close(self):
        pass


lp.http.client.HTTPSConnection = Verbindung


class H(lp.Handler):
    def __init__(self):
        self.wfile = io.BytesIO()
        self.status = None
        self.kopf = {}

    def send_response(self, status, *a, **k):
        self.status = status

    def send_header(self, key, value):
        self.kopf[key] = value

    def end_headers(self):
        pass


koerper = json.dumps({"model": "opencode/test-modell", "stream": False,
                      "messages": [{"role": "user", "content": "GEHEIMER-PROMPT"}]}).encode()
h = H()
h._proxy_zen("POST", "/zen/v1/chat/completions", koerper, "", 0.0, is_streaming=False)

check(h.status == 429, f"Anrufer sah {h.status}, erwartet 429 (Status des Anbieters)")
check(h.kopf.get("Retry-After") == "7", f"Retry-After fehlt: {h.kopf}")
check(h.wfile.getvalue() == Verbindung.rumpf, "Fehlerrumpf des Anbieters wurde veraendert")

zeilen = [z for z in lp._testlog if "zen" in z.lower() or "opencode" in z.lower()]
check(zeilen, "keine Zen-Logzeile")
check(not any("GEHEIMER-PROMPT" in z for z in zeilen),
      f"Anfragetext steht im Log: {zeilen}")
check(any("bytes=" in z and "hash=" in z for z in zeilen),
      f"Logzeile nennt nicht Groesse/Hash: {zeilen}")

# Gegenprobe: 5xx des Anbieters wird weiterhin 502 (kein 5xx an den Anrufer)
Verbindung.status = 503
Verbindung.rumpf = b'{"error":"upstream down"}'
Verbindung.kopf = {}
h2 = H()
h2._proxy_zen("POST", "/zen/v1/chat/completions", koerper, "", 0.0, is_streaming=False)
check(h2.status == 502, f"5xx des Anbieters: {h2.status}, erwartet 502")

print("GRUEN" if not fail else f"ROT ({fail})")
sys.exit(1 if fail else 0)
