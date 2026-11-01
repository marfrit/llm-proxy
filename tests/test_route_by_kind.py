#!/usr/bin/env python3
"""Geroutet wird nach der gepruefte Einstufung - nie an ihr vorbei.

_classify sagte 'local' fuer das [local]-Praefix allein, der Kostenregler liess
'local' immer durch, das Routing aber fragte die Einstufung nie und schickte alles
mit Wolkenpraefix oder Katalog-/Metadateneintrag an OpenRouter. Unter Modus 'local'
(DSGVO) ging so "[local] openai/x" in die Wolke - und seit 4b539cf jede bei
OpenRouter bekannte id, auch ein lokales Modell, das wie eine OpenRouter-id heisst.
ROT, wenn eine als 'local' eingestufte Anfrage in die Wolke geroutet wird oder ein
[local]-Auftrag ohne lokales Backend als 'local' durchgeht. Gegenprobe: ein echtes
Wolkenmodell wird weiter in die Wolke geroutet, ein lokales lokal.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade

lp = lade()
fehler = []
if not hasattr(lp, "_ziel"):
    print("ROT: _ziel fehlt - die Routing-Entscheidung steckt unpruefbar in _proxy")
    sys.exit(1)

lp._model_metadata.update({"openai/x": {"prompt_price": 1e-6}, "minimax/m2": {"prompt_price": 1e-6},
                           "gleich/name": {"prompt_price": 1e-6}})
lp.find_backend_for_model = lambda m: ("lokal", "127.0.0.1", 1) if m == "gleich/name" else None

for raw in ("[local] openai/x", "[local] minimax/m2"):
    model = lp._anfrage_id(raw)
    kind = lp._classify(raw, model)
    if kind == "local":
        fehler.append(f"{raw!r}: ohne lokales Backend als 'local' eingestuft")
    if lp._ziel(kind, model) == "cloud" and kind == "local":
        fehler.append(f"{raw!r}: 'local' eingestuft, aber in die Wolke geroutet")

model = lp._anfrage_id("gleich/name")
kind = lp._classify("gleich/name", model)
if kind != "local" or lp._ziel(kind, model) != "local":
    fehler.append(f"lokal gefuehrtes 'gleich/name' (auch OpenRouter-id): kind={kind}, ziel={lp._ziel(kind, model)}")

if lp._ziel(lp._classify("openai/x", "openai/x"), "openai/x") != "cloud":
    fehler.append("Gegenprobe: echtes Wolkenmodell nicht mehr in die Wolke geroutet")
if lp._ziel("free", "opencode/big-pickle") != "zen":
    fehler.append("Gegenprobe: Zen-Modell nicht mehr an Zen geroutet")

for f in fehler:
    print("ROT:", f)
print("GRUEN" if not fehler else f"ROT ({len(fehler)})")
sys.exit(1 if fehler else 0)
