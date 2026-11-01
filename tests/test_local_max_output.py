#!/usr/bin/env python3
"""local-max-output.json: max Output-Token fuer lokale Modelle im Katalog.

Ohne das Feld nehmen Klienten ihren Vorgabewert (16384) an - zu klein fuer das
hauptlokale Denkmodell. Korrektur, nicht Quelle: ein vom Backend gemeldeter Wert
gewinnt immer, die Datei greift nur bei Schweigen.

ROT, wenn: der Lader True, Strings, Null oder Negative nicht verwirft (bool ist in
Python ein int und darf nicht 1 werden) oder eine fehlende Datei nicht {} liefert;
ein Katalogeintrag eines gelisteten Modells max_output nicht traegt; ein nicht
gelistetes Modell das Feld bekommt; ein vom Backend gemeldetes positives int
ueberschrieben wird; ein (capped) Alias das max_output seines Ziels nicht erbt;
SIGHUP die Datei nicht neu liest oder die Protokollzeile sie nicht nennt.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade

lp = lade()
fehler = []
datei = os.path.join(lp.CONFIG_DIR, "local-max-output.json")


def schreibe(daten):
    with open(datei, "w") as f:
        json.dump(daten, f)


# --- Lader ---------------------------------------------------------------
if lp.LOCAL_MAX_OUTPUT != {}:
    fehler.append(f"fehlende Datei liefert {lp.LOCAL_MAX_OUTPUT}, erwartet {{}}")
schreibe({"a": 32768, "b": 4096.0, "c": True, "d": "32768", "e": 0, "f": -5})
ist = lp._load_local_max_output()
if ist != {"a": 32768, "b": 4096}:
    fehler.append("Lader: %s, erwartet {'a': 32768, 'b': 4096}" % ist)
schreibe([1, 2])
if lp._load_local_max_output() != {}:
    fehler.append("Lader: nicht-dict JSON liefert nicht {}")
os.remove(datei)
if lp._load_local_max_output() != {}:
    fehler.append("Lader: fehlende Datei liefert nicht {}")

# --- Katalog -------------------------------------------------------------
# Discovery ohne Live-Backend: discover/local_meta/local_ctx/Cloud alles gestubt,
# _handle_models liefert dann die JSON-Antwort an ein Fake-self.
class _Wfile:
    def __init__(self, owner):
        self.owner = owner
    def write(self, b):
        self.owner.body = b

class _FakeSelf:
    def __init__(self):
        self.body = None
        self.wfile = _Wfile(self)
    def send_response(self, code): pass
    def send_header(self, k, v): pass
    def end_headers(self): pass

IDS = ["ds41-flash", "melder", "sag", "anderes"]

def katalog(meta_map, aliases=None):
    if aliases is not None:
        with open(os.path.join(lp.CONFIG_DIR, "model-aliases.json"), "w") as f:
            json.dump(aliases, f)
        lp.MODEL_ALIASES = lp._load_model_aliases()
    lp.LOCAL_BACKENDS = [("b1", "127.0.0.1", 1)]
    lp.discover = lambda b: list(IDS)
    with lp._backends_up_lock:
        lp._backends_up.add("b1")
    lp.local_meta = lambda mid: meta_map.get(mid)
    lp.local_ctx = lambda mid: None
    lp._aktiver_modus = lambda: "max"
    lp.discover_cloud = lambda: None
    lp._merge_catalogs = lambda: {}
    fake = _FakeSelf()
    lp.Handler._handle_models(fake, 0.0)
    return {e["id"]: e for e in json.loads(fake.body)["data"]}

schreibe({"ds41-flash": 32768, "melder": 16384, "sag": 4096})
lp.LOCAL_MAX_OUTPUT = lp._load_local_max_output()
cat = katalog({"ds41-flash": {}, "melder": {"max_output": 8192},
               "sag": {"max_output": "8192"}, "anderes": None})
e = cat.get("[local] ds41-flash", {})
if e.get("max_output") != 32768:
    fehler.append(f"Katalog: gelistetes Modell ohne max_output: {e}")
if cat.get("[local] melder", {}).get("max_output") != 8192:
    fehler.append(f"Katalog: Backendwert ueberschrieben: {cat.get('[local] melder')}")
if cat.get("[local] sag", {}).get("max_output") != 4096:
    fehler.append(f"Katalog: nicht-int Backendwert zaehlt nicht als Meldung: {cat.get('[local] sag')}")
if "max_output" in cat.get("[local] anderes", {}):
    fehler.append(f"Katalog: nicht gelistetes Modell bekommt max_output: {cat.get('[local] anderes')}")

# Alias (capped) erbt max_output vom Ziel - deepcopy, kein extra Code.
cat = katalog({"ds41-flash": {}, "melder": None, "sag": None, "anderes": None},
              aliases={"ds41-flash-light": {"model": "ds41-flash", "max_prompt_tokens": 1000}})
a = cat.get("[local] ds41-flash-light", {})
if a.get("max_output") != 32768:
    fehler.append(f"Katalog: Alias erbt max_output nicht: {a}")
if a.get("ctx") != 1000:
    fehler.append(f"Katalog: Alias ctx nicht die Grenze: {a}")

# --- SIGHUP ----------------------------------------------------------------
schreibe({"ds41-flash": 65536})
lp.reload_backends(None, None)
if lp.LOCAL_MAX_OUTPUT != {"ds41-flash": 65536}:
    fehler.append("SIGHUP: %s, erwartet {'ds41-flash': 65536}" % (lp.LOCAL_MAX_OUTPUT,))
if not any("local-max-output (1)" in m for m in lp._testlog):
    fehler.append(f"Protokollzeile nennt local-max-output nicht: {lp._testlog[-1:]}")
os.remove(datei)
lp.reload_backends(None, None)
if lp.LOCAL_MAX_OUTPUT != {}:
    fehler.append(f"SIGHUP: Datei entfernt, {lp.LOCAL_MAX_OUTPUT}, erwartet {{}}")
if not any("local-max-output (0)" in m for m in lp._testlog):
    fehler.append(f"Protokollzeile nach SIGHUP nennt local-max-output nicht: {lp._testlog[-1:]}")
schreibe({"ds41-flash": 65536})
lp.reload_backends(None, None)
cat = katalog({"ds41-flash": {}, "melder": None, "sag": None, "anderes": None})
if cat.get("[local] ds41-flash", {}).get("max_output") != 65536:
    fehler.append(f"Katalog nach SIGHUP: {cat.get('[local] ds41-flash')}")

for f in fehler:
    print("ROT:", f)
print("GRUEN" if not fehler else f"ROT ({len(fehler)})")
sys.exit(1 if fehler else 0)
