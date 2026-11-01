#!/usr/bin/env python3
r"""Was /v1/models mit -na anzeigt, muss als Anfrage wieder auf die echte id fuehren.

Bis 1.0.1 stand in _proxy re.sub(r"\-na(?=:|\$)", ...) - `\$` ist ein woertliches
Dollarzeichen, kein Zeilenende. Ein Gratis-Modell wird als "[free] base-na"
angezeigt (das ":free" faellt in der Anzeige weg) und kam deshalb nie zurueck auf
"base:free". ROT, wenn die angezeigte Form nicht auf die Katalog-id fuehrt;
Gegenprobe: ein Modell, das wirklich auf "-na" endet und im Katalog steht, bleibt.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade

lp = lade()
fehler = []
if not hasattr(lp, "_anfrage_id"):
    print("ROT: _anfrage_id fehlt - die Rueckuebersetzung steckt unpruefbar in _proxy")
    sys.exit(1)

lp._free_catalog["meta-llama/llama-4-maverick:free"] = "na"
lp._paid_catalog["vendor/echt-na"] = "ok"
faelle = {
    "[free] meta-llama/llama-4-maverick-na": "meta-llama/llama-4-maverick:free",
    "[free] meta-llama/llama-4-maverick": "meta-llama/llama-4-maverick:free",
    "meta-llama/llama-4-maverick-na:free": "meta-llama/llama-4-maverick:free",
    "[$] vendor/echt-na": "vendor/echt-na",
    "[local] qwen3.6-coding": "qwen3.6-coding",
}
# Gleichnamig lokal und als :free in der Wolke: '[local] x' bleibt lokal (DSGVO-Fall),
# '[$] y' bleibt das bezahlte Modell, nicht stillschweigend die Gratisvariante.
lp._free_catalog["vendor/doppelt:free"] = "ok"
lp._free_catalog["vendor/bezahlt:free"] = "ok"
lp._paid_catalog["vendor/bezahlt"] = "ok"
lp.find_backend_for_model = lambda m: ("lokal", "127.0.0.1", 1) if m == "vendor/doppelt" else None
faelle["[local] vendor/doppelt"] = "vendor/doppelt"
faelle["vendor/doppelt"] = "vendor/doppelt"
faelle["[$] vendor/bezahlt"] = "vendor/bezahlt"
# Eine nur in den Metadaten bekannte id, die wirklich auf -na endet, bleibt.
lp._model_metadata["vendor/meta-na"] = {"prompt_price": 1e-6}
faelle["vendor/meta-na"] = "vendor/meta-na"
faelle["[local] qwen3.6-coding-down"] = "qwen3.6-coding"   # Anzeige eines toten Backends
for anzeige, soll in faelle.items():
    ist = lp._anfrage_id(anzeige)
    if ist != soll:
        fehler.append(f"{anzeige!r} -> {ist!r}, erwartet {soll!r}")

for f in fehler:
    print("ROT:", f)
print("GRUEN" if not fehler else f"ROT ({len(fehler)})")
sys.exit(1 if fehler else 0)
