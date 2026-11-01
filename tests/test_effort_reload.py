#!/usr/bin/env python3
"""SIGHUP liest reasoning-effort-map.json neu ein.

Bis 1.0.8 las reload_backends() alle Korrekturdateien ausser dieser; EFFORT_OVERRIDES stand
einmal beim Start fest. Am 2026-10-21 bekam ds41-flash einen Eintrag (medium -> high), der erst
nach einem Neustart gewirkt haette - und ein Neustart schneidet jeden laufenden Strom ab.
ROT, wenn nach dem Neuladen ein neuer, ein geaenderter oder ein entfernter Eintrag nicht gilt,
oder die Protokollzeile die Datei nicht nennt. Gegenprobe vor dem ersten Neuladen: ohne Datei
uebersetzt der Proxy nichts (sonst bestuende auch ein fest eingebauter Eintrag).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402

lp = lade()
fehler = []
datei = os.path.join(lp.CONFIG_DIR, "reasoning-effort-map.json")
MODELL = "ds41-flash"


def schreibe(daten):
    with open(datei, "w") as f:
        json.dump(daten, f)


def erwarte(was, soll):
    ist = lp._map_reasoning_effort(MODELL, "medium")
    if ist != soll:
        fehler.append(f"{was}: _map_reasoning_effort({MODELL!r}, 'medium') = {ist}, erwartet {soll}")


erwarte("ohne Datei", (False, "medium"))
schreibe({MODELL: {"medium": "high"}})
erwarte("Datei geschrieben, noch kein SIGHUP", (False, "medium"))
lp.reload_backends(None, None)
erwarte("neuer Eintrag nach SIGHUP", (True, "high"))
if not any("reasoning-effort-map (1)" in m for m in lp._testlog):
    fehler.append(f"Protokollzeile nennt reasoning-effort-map nicht: {lp._testlog[-1:]}")
schreibe({MODELL: {"medium": None}})
lp.reload_backends(None, None)
erwarte("geaenderter Eintrag nach SIGHUP", (True, None))
os.remove(datei)
lp.reload_backends(None, None)
erwarte("Datei entfernt, nach SIGHUP", (False, "medium"))

for f in fehler:
    print("ROT:", f)
print("GRUEN" if not fehler else f"ROT ({len(fehler)})")
sys.exit(1 if fehler else 0)
