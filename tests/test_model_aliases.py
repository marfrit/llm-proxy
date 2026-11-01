#!/usr/bin/env python3
"""Modell-Aliase mit Laengengrenze (model-aliases.json).

ROT, wenn: ein Alias nicht auf sein Ziel umgeschrieben wird (Praefix bleibt); ein Prompt ueber der
Grenze durchgeht oder einer darunter abgewiesen wird; die Schaetzung ohne /tokenize WENIGER Token
annimmt als ein realistischer Text hat (sie muss im Zweifel abweisen); der Katalog den Alias nicht
mit seiner Grenze fuehrt; ein reiner Zweitname (ohne Grenze) etwas abweist; ein Name, den ein Backend
selbst fuehrt, umgeschrieben wird. Gegenprobe: der Zielname selbst bleibt unberuehrt.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade

lp = lade()
fehler = []
with open(os.path.join(os.environ["LLM_PROXY_CONFIG_DIR"], "model-aliases.json"), "w") as f:
    json.dump({"ds41-flash-light": {"model": "ds41-flash", "max_prompt_tokens": 1000},
               "DeepSeek-v4.1-Flash-EXL3": {"model": "ds41-flash"},
               "kaputt": "kein objekt"}, f)
lp.MODEL_ALIASES = lp._load_model_aliases()
served = {"ds41-flash"}
lp.find_backend_for_model = lambda m: ("backend-a", "127.0.0.1", 9) if m in served else None

if set(lp.MODEL_ALIASES) != {"ds41-flash-light", "DeepSeek-v4.1-Flash-EXL3"}:
    fehler.append(f"Laden: {sorted(lp.MODEL_ALIASES)}")
raw, model, a = lp._apply_model_alias("[local] ds41-flash-light", "ds41-flash-light")
if (raw, model) != ("[local] ds41-flash", "ds41-flash") or not a or a["max_prompt_tokens"] != 1000:
    fehler.append(f"Umschreiben: {raw!r} {model!r} {a}")
if lp._classify(raw, model) != "local":
    fehler.append("umgeschriebener Alias nicht als lokal eingestuft")
if lp._apply_model_alias("[local] ds41-flash", "ds41-flash")[2] is not None:
    fehler.append("Gegenprobe: der Zielname selbst wurde als Alias behandelt")
r2 = lp._apply_model_alias("[local] DeepSeek-v4.1-Flash-EXL3", "DeepSeek-v4.1-Flash-EXL3")
if r2[1] != "ds41-flash" or r2[2]["max_prompt_tokens"] is not None:
    fehler.append(f"Zweitname: {r2}")
served.add("DeepSeek-v4.1-Flash-EXL3")  # alter Name noch live (Umbenennung laeuft): nicht umschreiben
if lp._apply_model_alias("[local] DeepSeek-v4.1-Flash-EXL3", "DeepSeek-v4.1-Flash-EXL3")[2] is not None:
    fehler.append("ein vom Backend selbst gefuehrter Name wurde umgeschrieben")
served.discard("DeepSeek-v4.1-Flash-EXL3")

a = {"alias": "ds41-flash-light", "model": "ds41-flash", "max_prompt_tokens": 1000}
if lp._cap_violation(a, 1000, "tokenize") is not None:
    fehler.append("genau an der Grenze abgewiesen")
e = lp._cap_violation(a, 1001, "tokenize")
if not e or e["error"]["type"] != "context_length_exceeded" or "ds41-flash" not in e["error"]["message"]:
    fehler.append(f"ueber der Grenze nicht (klar) abgewiesen: {e}")
if lp._cap_violation({"alias": "x", "model": "y", "max_prompt_tokens": None}, 10 ** 7, "tokenize") is not None:
    fehler.append("Zweitname ohne Grenze weist ab")

# ohne erreichbares /tokenize: Schaetzung, und die muss vorsichtig sein (>= echte Tokenzahl).
text = ("def parse(line):\n    return [f.strip() for f in line.split(',')]\n" * 200
        + "Erklaere kurz, was die Funktion tut, und nenne zwei Schwachstellen. " * 100)
j = {"model": "ds41-flash", "messages": [{"role": "user", "content": text}]}
n, how = lp._count_prompt_tokens(("tot", "127.0.0.1", 9), j)
if how != "estimate":
    fehler.append(f"ohne Backend nicht geschaetzt: {how}")
if n < len(text) / 3.2:   # gemessen 3,1-3,2 Zeichen je Token: die Schaetzung darf nicht darunter liegen
    fehler.append(f"Schaetzung {n} kleiner als die erwartete Tokenzahl ~{len(text) / 3.2:.0f}")

# Katalog: Alias mit Grenze, Zweitname, toter Zustand vererbt
ent = [lp.catalog_entry("[local] ds41-flash", "local-backend-a", {"ctx": 1048576})]
ent[0]["properties"] = [{"property_id": "context_window", "value": 1048576}]
cat = {e["id"]: e for e in lp._alias_catalog_entries(ent)}
if cat.get("[local] ds41-flash-light", {}).get("ctx") != 1000:
    fehler.append(f"Katalog: Alias ohne Grenze/ctx: {cat.get('[local] ds41-flash-light')}")
if [p["value"] for p in cat["[local] ds41-flash-light"].get("properties", [])] != [1000]:
    fehler.append("Katalog: context_window des Alias nicht gesenkt")
if cat.get("[local] DeepSeek-v4.1-Flash-EXL3", {}).get("ctx") != 1048576:
    fehler.append("Katalog: Zweitname fehlt oder hat falsches ctx")
down = lp._alias_catalog_entries([lp.catalog_entry("[local] ds41-flash" + lp.DOWN_SUFFIX, "x", {"ctx": 5})])
if not any(e["id"] == "[local] ds41-flash-light" + lp.DOWN_SUFFIX for e in down):
    fehler.append("Katalog: toter Zustand nicht vererbt")
if ent[0].get("ctx") != 1048576:
    fehler.append("Katalog: Zieleintrag veraendert")

# Jedes Kontextfeld des Katalogs traegt die Grenze (manche Klienten lesen max_model_len, nicht ctx): ein
# Alias, der 1048576 als max_model_len weitermeldet, laesst eine Sitzung bis in den 400 wachsen.
voll = {"max_model_len": 1048576, "context_length": 1048576, "n_ctx": 1048576, "max_context_length": 1048576,
        "meta": {"n_ctx": 1048576, "n_ctx_train": 1048576}}
ent2 = [lp.catalog_entry("[local] ds41-flash", "local-backend-a", dict(voll, ctx=1048576))]
ent2[0].update(voll)
ent2[0]["properties"] = [{"property_id": "context_window", "value": 1048576}]
cat2 = {e["id"]: e for e in lp._alias_catalog_entries(ent2)}
light = cat2.get("[local] ds41-flash-light", {})
for k in ("max_model_len", "context_length", "n_ctx", "max_context_length", "ctx"):
    if light.get(k) != 1000:
        fehler.append(f"Katalog: Alias meldet {k}={light.get(k)} statt der Grenze 1000")
if (light.get("meta") or {}).get("n_ctx") != 1000 or (light.get("meta") or {}).get("n_ctx_train") != 1000:
    fehler.append(f"Katalog: Alias meldet meta {light.get('meta')}")
if lp._ctx_of(light) != 1000:
    fehler.append(f"Katalog: _ctx_of(Alias) = {lp._ctx_of(light)}")
old = cat2.get("[local] DeepSeek-v4.1-Flash-EXL3", {})
if old.get("max_model_len") != 1048576 or old.get("ctx") != 1048576:
    fehler.append(f"Katalog: Zweitname ohne Grenze veraendert: {old.get('max_model_len')}/{old.get('ctx')}")
if ent2[0].get("max_model_len") != 1048576 or ent2[0]["meta"]["n_ctx"] != 1048576:
    fehler.append("Katalog: Zieleintrag veraendert")

# Verdrahtung: Katalog und Anfrageweg rufen die Bausteine wirklich auf (sonst sind obige Tests
# gruen, waehrend der Dienst die Aliase nie anbietet oder nie prueft).
import inspect
src_models = inspect.getsource(lp.Handler._handle_models)
src_proxy = inspect.getsource(lp.Handler._proxy)
if "local_entries + _alias_catalog_entries(local_entries)" not in src_models:
    fehler.append("Verdrahtung: /v1/models fuehrt die Aliase nicht")
for needle in ("_apply_model_alias(raw_model, model)", "_count_prompt_tokens(", "_cap_violation(alias"):
    if needle not in src_proxy:
        fehler.append(f"Verdrahtung: _proxy ruft {needle} nicht auf")

for f in fehler:
    print("ROT:", f)
print("GRUEN" if not fehler else f"ROT ({len(fehler)})")
sys.exit(1 if fehler else 0)
