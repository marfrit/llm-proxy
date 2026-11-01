#!/usr/bin/env python3
"""Der Bezahlkatalog wird aus der Entdeckung abgeleitet, wie der Gratiskatalog.

Bis 1.0.2 hielt der Katalog-Check jeden einmal aufgenommenen Bezahl-Eintrag fuer
immer ("manuell gepflegte Eintraege nie entfernen") - es gibt aber keine
manuellen (2026-10-14). Wird ein Modell teurer oder verschwindet es bei
OpenRouter, blieb es trotzdem im Katalog. ROT, wenn ein inzwischen teures oder
verschwundenes Modell bleibt; Gegenprobe: ein weiterhin billiges bleibt, und ein
gescheiterter Abruf leert den Katalog NICHT.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade

lp = lade()
lp._probe_model = lambda k, m: None
lp._save_catalog = lambda: None
lp._paid_catalog.update({"qwen/bleibt": "ok", "openai/teurer": "ok", "mistralai/weg": "ok"})
zeilen = [
    {"id": "qwen/bleibt", "name": "B", "context_length": 65536, "pricing": {"prompt": "0.0000002"}},
    {"id": "openai/teurer", "name": "T", "context_length": 65536, "pricing": {"prompt": "0.00003"}},
]
lp._fetch_openrouter_models = lambda: (frozenset(z["id"] for z in zeilen), zeilen)
lp._run_catalog_check()
fehler = []
if "openai/teurer" in lp._paid_catalog:
    fehler.append("teurer gewordenes Modell (30 $/MTok) bleibt im Bezahlkatalog")
if "mistralai/weg" in lp._paid_catalog:
    fehler.append("bei OpenRouter verschwundenes Modell bleibt im Bezahlkatalog")
if "qwen/bleibt" not in lp._paid_catalog:
    fehler.append("weiterhin billiges Modell (0.20 $/MTok) entfernt")

vorher = dict(lp._paid_catalog)
lp._fetch_openrouter_models = lambda: None          # OpenRouter nicht erreichbar
lp._run_catalog_check()
if lp._paid_catalog != vorher:
    fehler.append("gescheiterter Abruf veraendert den Bezahlkatalog")

# OpenRouter antwortet 200 mit leerer Liste: das ist kein Katalog, sondern ein Ausfall.
lp._model_metadata["qwen/bleibt"] = {"prompt_price": 2e-7}
vorher = (dict(lp._paid_catalog), dict(lp._free_catalog), dict(lp._model_metadata))
lp._fetch_openrouter_models = lambda: (frozenset(), [])
lp._run_catalog_check()
if (dict(lp._paid_catalog), dict(lp._free_catalog), dict(lp._model_metadata)) != vorher:
    fehler.append("leere Liste von OpenRouter leert die Kataloge")

lp._fetch_openrouter_models = lambda: (frozenset(), [{"name": "ohne id"}, {"id": 7}])
lp._run_catalog_check()
if (dict(lp._paid_catalog), dict(lp._free_catalog), dict(lp._model_metadata)) != vorher:
    fehler.append("Liste nur aus kaputten Zeilen leert die Kataloge")

for f in fehler:
    print("ROT:", f)
print("GRUEN" if not fehler else f"ROT ({len(fehler)})")
sys.exit(1 if fehler else 0)
