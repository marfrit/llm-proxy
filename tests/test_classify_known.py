#!/usr/bin/env python3
"""Was OpenRouter kennt, ist 'paid' - nicht 'unknown' -, auch ausserhalb des Bezahlkatalogs.

Der Bezahlkatalog listet nur, was billig ist (Anzeige). _classify fragte bis 1.0.2
nur die beiden Kataloge: ein teures, aber existierendes Modell (o1-pro) war damit
'unknown' -> 404 model_not_found, auch im Modus 'max'. Verdeckt war das, solange die
Einheiten-Verwechslung fast jedes Modell in den Katalog spuelte.
ROT, wenn ein in den OpenRouter-Metadaten bekanntes Modell 'unknown' ist, unter 'max'
gesperrt wird oder unter 'free' als 'unbekannt' statt als Kostenverstoss abgewiesen wird;
Gegenprobe: ein wirklich unbekannter Name bleibt 'unknown'.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade

lp = lade()
lp.find_backend_for_model = lambda m: None
lp._model_metadata["openai/o1-pro"] = {"prompt_price": 0.00015}
fehler = []
k = lp._classify("openai/o1-pro", "openai/o1-pro")
if k != "paid":
    fehler.append(f"o1-pro (bei OpenRouter bekannt, nicht im Bezahlkatalog) -> {k!r}, erwartet 'paid'")
if lp._classify("openai/gibts-nicht", "openai/gibts-nicht") != "unknown":
    fehler.append("unbekannter Name wird nicht mehr als 'unknown' erkannt")
pf = os.path.join(lp.CONFIG_DIR, "policy.json")
for t, (mode, soll_zu) in enumerate([("max", False), ("cheap", True), ("free", True)], start=1):
    with open(pf, "w") as f:
        json.dump({"mode": mode, "cheap_max_prompt_price": 0.5}, f)
    os.utime(pf, (t * 10, t * 10))
    zu, warum = lp._policy_blocked("openai/o1-pro", "openai/o1-pro")
    if zu != soll_zu:
        fehler.append(f"Modus {mode}: gesperrt={zu}, erwartet {soll_zu} ({warum})")
    if zu and "unbekannt" in warum:
        fehler.append(f"Modus {mode}: als 'unbekannt' abgewiesen statt nach Kosten")

for f in fehler:
    print("ROT:", f)
print("GRUEN" if not fehler else f"ROT ({len(fehler)})")
sys.exit(1 if fehler else 0)
