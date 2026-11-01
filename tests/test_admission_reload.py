#!/usr/bin/env python3
"""SIGHUP darf die Slots eines Backends nicht verdoppeln.

Bis 1.0.1 baute _build_sems() bei jedem Neuladen frische Semaphoren; laufende
Anfragen hielten die alten, neue bekamen volle neue. Bei slots=1 liefen nach einem
SIGHUP (ein externes Werkzeug nach jedem Unit-Wechsel; 228 im Log) zwei Anfragen
gleichzeitig. ROT, wenn nach dem Neuladen eine zweite Anfrage sofort zugelassen
wird, solange die erste laeuft - oder wenn die erste nach ihrem Ende den Slot nicht
zurueckgibt (Gegenprobe gegen "einfach nie mehr zulassen").
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade

lp = lade()
with open(os.path.join(lp.CONFIG_DIR, "local-backends.json"), "w") as f:
    json.dump([{"name": "b1", "host": "127.0.0.1", "port": 1, "slots": 1}], f)
lp.LOCAL_BACKENDS = lp._load_local_backends()
lp.ADMIT_TIMEOUT = 0.5
fehler = []

a = lp._admit("b1")
if not a:
    fehler.append("erste Anfrage nicht zugelassen")
lp.reload_backends(None, None)
t = time.monotonic()
b = lp._admit("b1")
if b:
    fehler.append(f"zweite Anfrage nach SIGHUP sofort zugelassen ({time.monotonic()-t:.1f}s) - slots=1 verdoppelt")
    lp._release("b1", b) if lp._release.__code__.co_argcount > 1 else lp._release("b1")
lp._release("b1", a) if lp._release.__code__.co_argcount > 1 else lp._release("b1")
c = lp._admit("b1")
if not c:
    fehler.append("nach Ende der ersten Anfrage bleibt der Slot belegt")
else:
    lp._release("b1", c) if lp._release.__code__.co_argcount > 1 else lp._release("b1")

# Eine geaenderte Slot-Zahl muss nach dem Neuladen gelten
with open(os.path.join(lp.CONFIG_DIR, "local-backends.json"), "w") as f:
    json.dump([{"name": "b1", "host": "127.0.0.1", "port": 1, "slots": 2}], f)
lp.reload_backends(None, None)
x, y = lp._admit("b1"), lp._admit("b1")
if not (x and y):
    fehler.append("slots=2 nach dem Neuladen nicht wirksam")

for f in fehler:
    print("ROT:", f)
print("GRUEN" if not fehler else f"ROT ({len(fehler)})")
sys.exit(1 if fehler else 0)
