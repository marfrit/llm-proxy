#!/usr/bin/env python3
"""Bietet /v1/models in den Modi 'local' und 'free' nur an, was auch geht?

Prueft _katalog_zeigt(mode, kind, unavailable) als Tabelle. ROT gegen den Stand
vor der Aenderung (dort gibt es die Funktion nicht, /v1/models zeigte in jedem
Modus alles), GRUEN danach. Gegenprobe eingebaut: 'max' und 'cheap' muessen
weiterhin ALLES zeigen, auch -down/-na und bezahlte Modelle - sonst bestuende
die Pruefung auch bei einem "Fix", der einfach immer filtert.

Aufruf:  python3 tests/test_catalog_mode.py [pfad-zu-llm-proxy.py]
"""

import importlib.util
import os
import sys

pfad = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "llm-proxy.py")
spec = importlib.util.spec_from_file_location("llm_proxy", pfad)
lp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lp)

if not hasattr(lp, "_katalog_zeigt"):
    print("ROT: _katalog_zeigt fehlt - /v1/models filtert nach Modus gar nicht")
    sys.exit(1)

# (mode, kind, unavailable) -> erwartet
faelle = {
    ("local", "local", False): True,
    ("local", "local", True):  False,   # -down
    ("local", "free",  False): False,
    ("local", "paid",  False): False,
    ("free",  "local", False): True,
    ("free",  "local", True):  False,   # -down
    ("free",  "free",  False): True,
    ("free",  "free",  True):  False,   # -na
    ("free",  "paid",  False): False,
    # Gegenprobe: unveraendert alles
    ("cheap", "paid",  False): True,
    ("cheap", "local", True):  True,
    ("max",   "paid",  True):  True,
    ("max",   "free",  True):  True,
    ("max",   "local", True):  True,
}
fehler = [(k, v) for k, v in faelle.items() if lp._katalog_zeigt(*k) != v]
for k, v in fehler:
    print(f"ROT: _katalog_zeigt{k} = {not v}, erwartet {v}")
if fehler:
    sys.exit(1)
print(f"GRUEN: {len(faelle)} Faelle")
