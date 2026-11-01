#!/usr/bin/env python3
"""Ein kaputter Katalogeintrag darf den Katalog-Thread nicht beenden.

Bis 1.0.1 stand in _run_catalog_check ein ungeschuetztes float(pricing.get("prompt")),
und _checker_loop hatte kein try: ein "prompt": null von OpenRouter beendete den
Thread fuer immer - kein Auffrischen mehr, /admin/recheck wirkungslos.
ROT, wenn der Thread an einem schlechten Eintrag stirbt oder die gueltigen
Eintraege derselben Liste nicht uebernimmt (Gegenprobe gegen "alles verwerfen").
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade

lp = lade()
zeilen = [
    {"id": "x-ai/grok-z", "name": "Grok Z", "context_length": 131072,
     "pricing": {"prompt": None}, "supported_parameters": None},
    {"name": "ohne id"},
    {"id": "qwen/zahl", "supported_parameters": 5, "name": 7},
    {"id": "qwen/kaputt", "pricing": None},
    {"id": "qwen/billig", "name": "Billig 70B", "context_length": 65536,
     "pricing": {"prompt": "0.0000001"}, "supported_parameters": ["reasoning"]},
]
lp._fetch_openrouter_models = lambda: (frozenset(z["id"] for z in zeilen if "id" in z), zeilen)
lp._probe_model = lambda k, m: None
lp._save_catalog = lambda: None
lp.CHECK_INTERVAL = 0.2
fehler = []

t = threading.Thread(target=lp._checker_loop, daemon=True)
t.start()
time.sleep(1.0)
if not t.is_alive():
    fehler.append("Katalog-Thread ist gestorben")
if "qwen/billig" not in lp._paid_catalog:
    fehler.append("gueltiger billiger Eintrag nicht uebernommen")
if (lp._model_metadata.get("qwen/billig") or {}).get("reasoning") is not True:
    fehler.append("Metadaten des gueltigen Eintrags fehlen")
if "prompt_price" in (lp._model_metadata.get("x-ai/grok-z") or {}):
    fehler.append("fehlender Preis wurde als Zahl eingetragen (fehlend ist nicht 0 und nicht 999)")
lp._checker_exit.set()
lp._checker_pending.set()

for f in fehler:
    print("ROT:", f)
print("GRUEN" if not fehler else f"ROT ({len(fehler)})")
sys.exit(1 if fehler else 0)
