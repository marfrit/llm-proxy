#!/usr/bin/env python3
"""Preisgrenzen in $/MTok gegen OpenRouter-Preise in $/Token.

OpenRouter meldet pricing.prompt in Dollar PRO TOKEN (o1-pro: "0.00015" = 150 $/MTok).
Bis 1.0.1 verglich der Proxy diese Zahl direkt mit 0.50 "$/MTok": jede Zahl pro Token
liegt darunter. Folgen: die Auto-Entdeckung "billiger" Modelle nahm o1-pro und GPT-4
auf, und Modus 'cheap' liess JEDES Modell durch.
ROT, wenn 150 $/MTok unter cheap_max_prompt_price 0.5 durchgeht oder als billig
entdeckt wird; Gegenprobe: 0.10 $/MTok muss durchgehen und entdeckt werden.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade

lp = lade()
lp.find_backend_for_model = lambda m: None
with open(os.path.join(lp.CONFIG_DIR, "policy.json"), "w") as f:
    json.dump({"mode": "cheap", "cheap_max_prompt_price": 0.5}, f)
fehler = []

teuer, billig = "openai/o1-pro", "qwen/billig-70b"
lp._paid_catalog.update({teuer: "ok", billig: "ok"})
lp._model_metadata.update({teuer: {"prompt_price": 0.00015}, billig: {"prompt_price": 1e-7}})
if not lp._policy_blocked(teuer, teuer)[0]:
    fehler.append("cheap: o1-pro (150 $/MTok) geht unter Grenze 0.5 $/MTok durch")
if lp._policy_blocked(billig, billig)[0]:
    fehler.append("cheap: 0.10 $/MTok wird gesperrt")

# Auto-Entdeckung
zeilen = [
    {"id": teuer, "name": "o1-pro", "context_length": 200000, "pricing": {"prompt": "0.00015"}},
    {"id": billig, "name": "Billig 70B", "context_length": 65536, "pricing": {"prompt": "0.0000001"}},
]
lp._paid_catalog.clear()
lp._fetch_openrouter_models = lambda: (frozenset(z["id"] for z in zeilen), zeilen)
lp._probe_model = lambda k, m: None
lp._save_catalog = lambda: None
lp._run_catalog_check()
if teuer in lp._paid_catalog:
    fehler.append("Auto-Entdeckung nimmt o1-pro (150 $/MTok) als 'billig' auf")
if billig not in lp._paid_catalog:
    fehler.append("Auto-Entdeckung uebersieht 0.10 $/MTok")
if lp._model_metadata.get(teuer, {}).get("prompt_price") != 0.00015:
    fehler.append("prompt_price im Katalog nicht mehr OpenRouters $/Token (Schnittstelle nach aussen geaendert)")

for f in fehler:
    print("ROT:", f)
print("GRUEN" if not fehler else f"ROT ({len(fehler)})")
sys.exit(1 if fehler else 0)
