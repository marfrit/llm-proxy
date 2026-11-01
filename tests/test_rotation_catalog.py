#!/usr/bin/env python3
"""Katalog: sagt /v1/models je lokalem Modell, ob das Backend es gerade liefert?

Die Rotation im pi-Plugin darf nur auf ein Modell hopsen, das READY ist. Diese Aussage
gab es im Katalog bereits: `reachable` ist der boolesche Wert, den catalog_entry() aus
dem Backend-Zustand setzt (False fuer -down-Eintraege). Deshalb wird hier KEIN zweiter
Name wie `ready` eingefuehrt - zwei Felder fuer denselben Fakt widersprechen sich frueher
oder spaeter. Geprueft wird das vorhandene Feld, und dass es keinen Doppelgaenger gibt.

ROT, wenn
  - ein Backend oben ist, aber sein Eintrag nicht reachable=true traegt;
  - ein -down-Eintrag reachable true traegt oder den Suffix verliert;
  - `context_length` (heute aus den Backend-Metadaten) verloren geht oder umbenannt wird;
  - ein zweites Bereitschaftsfeld (`ready`) neben `reachable` auftaucht.

Aufruf:  python3 tests/test_rotation_catalog.py [pfad-zu-llm-proxy.py]
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


class H(lp.Handler):
    def __init__(self, wfile, conn):
        self.wfile, self.connection = wfile, conn

    def send_response(self, *a, **k):
        pass

    def send_header(self, *a, **k):
        pass

    def end_headers(self):
        pass


# Synthetische Flotte: ein Backend oben (backend.example), eins unten (backend.example2).
lp.LOCAL_BACKENDS[:] = [("backend.example", "192.0.2.10", 8080),
                        ("backend.example2", "192.0.2.11", 8080)]
lp._build_sems({b[0]: 1 for b in lp.LOCAL_BACKENDS})
lp.discover = lambda b: ["modell-a"] if b[0] == "backend.example" else ["modell-b"]
lp.discover_cloud = lambda: None
lp._merge_catalogs = lambda: {}
lp._aktiver_modus = lambda: "max"
lp._local_reasoning = lambda mid, meta=None: None
lp._local_ctx = {}
lp._local_meta = {"modell-a": {"context_length": 98304, "n_ctx": 98304,
                               "supported_parameters": ["tools"]},
                  "modell-b": {"context_length": 32768, "n_ctx": 32768}}
lp.local_meta = lambda mid: dict(lp._local_meta.get(mid) or {})
lp.local_ctx = lambda mid: None
with lp._backends_up_lock:
    lp._backends_up.clear()
    lp._backends_up.add("backend.example")

a, b = socket.socketpair()
h = H(io.BytesIO(), a)
try:
    h._handle_models(0.0)
    rumpf = h.wfile.getvalue()
finally:
    a.close(); b.close()

try:
    eintraege = {e["id"]: e for e in json.loads(rumpf)["data"]}
except Exception as e:
    check(False, f"/v1/models kein JSON ({type(e).__name__}: {e})")
    eintraege = {}

oben = eintraege.get("[local] modell-a")
unten = eintraege.get("[local] modell-b-down")
check(oben is not None, f"Eintrag des oberen Backends fehlt: {sorted(eintraege)}")
check(unten is not None, f"-down-Eintrag fehlt/ohne Suffix: {sorted(eintraege)}")
if oben:
    check(oben.get("reachable") is True,
          f"oben: Bereitschaft {oben.get('reachable')!r} statt True")
    check(oben.get("context_length") == 98304,
          f"oben: context_length {oben.get('context_length')!r} statt 98304")
    check("down" not in oben["id"], f"oben: id traegt -down: {oben['id']!r}")
if unten:
    check(unten.get("reachable") is False,
          f"unten: Bereitschaft {unten.get('reachable')!r} statt False")
    check(unten.get("context_length") == 32768,
          f"unten: context_length {unten.get('context_length')!r} statt 32768")
    check(unten.get("owned_by", "").endswith("(temporarily unavailable)"),
          f"unten: owned_by {unten.get('owned_by')!r} ohne Hinweis")

# Kein Doppelgaenger: der Fakt steht genau einmal im Eintrag.
if oben:
    check("ready" not in oben,
          "zweites Bereitschaftsfeld 'ready' neben dem vorhandenen 'reachable' erfunden")

print("GRUEN" if not fail else f"ROT ({fail})")
sys.exit(1 if fail else 0)
